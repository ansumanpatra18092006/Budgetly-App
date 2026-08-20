# ============================================================
# FILE: routes/ai_insights.py  [UNIFIED FINANCIAL INTELLIGENCE]
# ============================================================

from flask import Blueprint, jsonify, session, request
from utils.db import get_db
from utils.decorators import login_required
from datetime import datetime, date, timedelta
import math
import re

ai_insights_bp = Blueprint("ai_insights", __name__)


def _safe_close(conn):
    try:
        conn.close()
    except Exception:
        pass


def _get_month_bounds():
    today = datetime.today()
    cur_start = today.strftime("%Y-%m-01")
    if today.month == 1:
        prev_start = f"{today.year-1}-12-01"
        prev_end   = f"{today.year-1}-12-31"
    else:
        first_of_cur = datetime(today.year, today.month, 1)
        last_of_prev = first_of_cur - timedelta(days=1)
        prev_start   = last_of_prev.strftime("%Y-%m-01")
        prev_end     = last_of_prev.strftime("%Y-%m-%d")
    return cur_start, prev_start, prev_end


def _fetch_full_metrics(conn, user_id):
    """
    Unified metrics layer — single source of truth for all intelligence
    modules: insights, risk, roadmap, transaction preview, recommendations.

    Added in this version:
      - goal_pressure  : urgency index (0–100). High = goals falling behind.
      - goal_details   : per-goal breakdown with monthly_required, months_left.
      - combined_risk  : blended risk from expense ratio + goal pressure.
    """
    cur_start, prev_start, prev_end = _get_month_bounds()
    today = datetime.today()

    # ── Current month income / expense ──────────────────────────
    cur = conn.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN type='income'  THEN amount ELSE 0 END),0) AS income,
            COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END),0) AS expense
        FROM transactions WHERE user_id=%s AND date>=%s
    """, (user_id, cur_start)).fetchone()

    # ── Previous month ───────────────────────────────────────────
    prev = conn.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN type='income'  THEN amount ELSE 0 END),0) AS income,
            COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END),0) AS expense
        FROM transactions WHERE user_id=%s AND date>=%s AND date<=%s
    """, (user_id, prev_start, prev_end)).fetchone()

    # ── Budget ───────────────────────────────────────────────────
    budget_row = conn.execute(
        "SELECT COALESCE(amount,0) AS amount FROM budgets WHERE user_id=%s",
        (user_id,)
    ).fetchone()

    # ── Top spending category ────────────────────────────────────
    top_cat = conn.execute("""
        SELECT COALESCE(category,'Misc') AS category, SUM(amount) AS total
        FROM transactions WHERE user_id=%s AND type='expense' AND date>=%s
        GROUP BY category ORDER BY total DESC LIMIT 1
    """, (user_id, cur_start)).fetchone()

    # ── Goals with full detail ───────────────────────────────────
    goal_rows = conn.execute(
        """SELECT id, name, target_amount, saved_amount, category, target_date
           FROM goals WHERE user_id=%s ORDER BY id ASC""",
        (user_id,)
    ).fetchall()

    # ── Monthly cash flow (last 3 months for forecasting) ────────
    hist_rows = conn.execute("""
        SELECT to_char(date,'YYYY-MM') AS month,
               SUM(CASE WHEN type='income'  THEN amount ELSE 0 END) AS inc,
               SUM(CASE WHEN type='expense' THEN amount ELSE 0 END) AS exp
        FROM transactions WHERE user_id=%s
        GROUP BY month ORDER BY month DESC LIMIT 3
    """, (user_id,)).fetchall()

    # ── Anomaly count (last 30 days) ─────────────────────────────
    recent_amounts_row = conn.execute("""
        SELECT amount FROM transactions
        WHERE user_id=%s AND type='expense' ORDER BY date ASC
    """, (user_id,)).fetchall()

    # ═══════════════════════════════════════════════════════════
    # COMPUTE BASE METRICS
    # ═══════════════════════════════════════════════════════════
    # Safely convert PostgreSQL Decimals to float before calculations
    income    = float(cur["income"] or 0)
    expense   = float(cur["expense"] or 0)
    surplus   = income - expense
    budget    = float(budget_row["amount"] or 0) if budget_row else 0.0
    p_expense = float(prev["expense"] or 0)
    p_income  = float(prev["income"] or 0)

    savings_rate    = round(surplus / income * 100, 1)          if income  > 0 else 0.0
    budget_used_pct = round(expense / budget * 100, 1)          if budget  > 0 else 0.0
    expense_change  = round((expense - p_expense) / p_expense * 100, 1) if p_expense > 0 else 0.0

    days_passed  = max(today.day, 1)
    days_left    = max(30 - days_passed, 0)
    daily_burn   = expense / days_passed

    top_cat_name  = top_cat["category"] if top_cat else "N/A"
    top_cat_total = float(top_cat["total"] or 0) if top_cat else 0.0
    top_cat_pct   = round(top_cat_total / expense * 100, 1) if top_cat and expense > 0 else 0.0

    # ── Average monthly cash flow (3-month history) ──────────────
    if hist_rows:
        avg_income_hist  = sum(float(r["inc"] or 0) for r in hist_rows) / len(hist_rows)
        avg_expense_hist = sum(float(r["exp"] or 0) for r in hist_rows) / len(hist_rows)
        avg_monthly_surplus = max(0.0, avg_income_hist - avg_expense_hist)
    else:
        avg_monthly_surplus = max(0.0, surplus)

    # ═══════════════════════════════════════════════════════════
    # GOAL PRESSURE CALCULATION
    # Formula:
    #   goal_pressure = ((total_target - total_saved) / total_target) * 100
    #
    # Elevated further when:
    #   - A goal has a target_date that is approaching (<3 months)
    #   - Monthly surplus is insufficient to cover required savings
    # ═══════════════════════════════════════════════════════════
    total_target = 0.0
    total_saved  = 0.0
    goal_details = []

    for row in goal_rows:
        g_id     = row["id"]
        g_name   = row["name"]
        target   = float(row["target_amount"] or 0)
        saved    = float(row["saved_amount"]  or 0)
        remaining = max(0.0, target - saved)
        progress_pct = round(saved / target * 100, 1) if target > 0 else 0.0

        total_target += target
        total_saved  += saved

        # Per-goal monthly requirement
        monthly_required = None
        months_left_goal = None
        goal_risk        = "low"
        target_date_str  = row["target_date"] if "target_date" in row.keys() else None

        if target_date_str:
            try:
                td = datetime.strptime(target_date_str, "%Y-%m-%d")
                ml = max(1, (td.year - today.year) * 12 + (td.month - today.month))
                months_left_goal = ml
                monthly_required = round(remaining / ml, 2) if ml > 0 else remaining

                if monthly_required > avg_monthly_surplus:
                    goal_risk = "high" if monthly_required > avg_monthly_surplus * 1.5 else "medium"
                elif ml <= 2:
                    goal_risk = "medium"
            except (ValueError, TypeError):
                pass
        elif avg_monthly_surplus > 0 and remaining > 0:
            months_left_goal = round(remaining / avg_monthly_surplus, 1)
            monthly_required = round(avg_monthly_surplus, 2)

        goal_details.append({
            "id":               g_id,
            "name":             g_name,
            "target_amount":    target,
            "saved_amount":     saved,
            "remaining":        round(remaining, 2),
            "progress_percent": progress_pct,
            "monthly_required": monthly_required,
            "months_left":      months_left_goal,
            "target_date":      target_date_str,
            "goal_risk":        goal_risk,
            "category":         row["category"],
        })

    # Base goal pressure
    if total_target > 0:
        base_pressure = ((total_target - total_saved) / total_target) * 100
    else:
        base_pressure = 0.0

    # Urgency bonus — amplify if surplus can't cover goals
    total_monthly_required = sum(
        float(g["monthly_required"] or 0) for g in goal_details if g["monthly_required"]
    )
    
    if avg_monthly_surplus > 0 and total_monthly_required > 0:
        coverage_ratio = avg_monthly_surplus / total_monthly_required
        if coverage_ratio < 0.5:
            base_pressure = min(100.0, base_pressure * 1.3)
        elif coverage_ratio < 1.0:
            base_pressure = min(100.0, base_pressure * 1.1)

    goal_pressure = round(min(base_pressure, 100.0), 1)

    # ═══════════════════════════════════════════════════════════
    # COMBINED RISK SCORE
    # Blends: expense ratio (50%) + goal pressure (30%) + budget (20%)
    # ═══════════════════════════════════════════════════════════
    expense_ratio = (expense / income * 100) if income > 0 else 100.0
    goal_pressure_weight = 0.30
    expense_ratio_weight = 0.50
    budget_weight        = 0.20

    combined_risk_score = (
        (expense_ratio        * expense_ratio_weight) +
        (goal_pressure        * goal_pressure_weight) +
        (budget_used_pct      * budget_weight)
    )

    if   combined_risk_score >= 80: combined_risk = "high"
    elif combined_risk_score >= 55: combined_risk = "medium"
    else:                           combined_risk = "low"

    return dict(
        # ── Core financials ──────────────────────────────────
        income=income, expense=expense, surplus=surplus, budget=budget,
        savings_rate=savings_rate, budget_used_pct=budget_used_pct,
        expense_change=expense_change, p_income=p_income, p_expense=p_expense,
        days_left=days_left, days_passed=days_passed, daily_burn=daily_burn,
        today_day=today.day,
        # ── Category intelligence ────────────────────────────
        top_cat_name=top_cat_name, top_cat_pct=top_cat_pct,
        # ── Goal intelligence (NEW) ──────────────────────────
        goal_pressure=goal_pressure,
        goal_details=goal_details,
        total_target=total_target,
        total_saved=total_saved,
        avg_monthly_surplus=avg_monthly_surplus,
        total_monthly_required=total_monthly_required,
        # ── Combined risk (NEW) ──────────────────────────────
        combined_risk=combined_risk,
        combined_risk_score=round(combined_risk_score, 1),
        # ── Legacy compat ────────────────────────────────────
        goals=[{
            "name": g["name"],
            "target_amount": g["target_amount"],
            "saved_amount": g["saved_amount"],
        } for g in goal_details],
    )


# ── 1. PROACTIVE AI INSIGHTS (goal-aware) ────────────────────────────
@ai_insights_bp.route("/ai-insights")
@login_required
def ai_insights():
    conn = get_db()
    try:
        m = _fetch_full_metrics(conn, session["user_id"])
    finally:
        _safe_close(conn)

    insights = []

    # Budget pressure
    if m["budget"] > 0 and m["budget_used_pct"] >= 85:
        cut = int((m["expense"] - m["budget"]) / max(m["days_left"], 1))
        insights.append({
            "message": f"Budget at {m['budget_used_pct']}% — ₹{int(m['expense'])} of ₹{int(m['budget'])} used. Cut ₹{cut}/day to avoid overspend.",
            "level": "high", "type": "budget"
        })
    elif m["budget"] > 0 and m["budget_used_pct"] >= 65:
        safe = int((m["budget"] - m["expense"]) / max(m["days_left"], 1))
        insights.append({
            "message": f"Budget {m['budget_used_pct']}% used. ₹{int(m['budget'] - m['expense'])} left for {m['days_left']} days — pace at ₹{safe}/day.",
            "level": "medium", "type": "budget"
        })

    # Savings rate
    if m["savings_rate"] < 5 and m["income"] > 0:
        save = int(m["expense"] * m["top_cat_pct"] / 100 * 0.15)
        insights.append({
            "message": f"Savings rate only {m['savings_rate']}%. Cutting {m['top_cat_name']} by 15% would free ₹{save} this month.",
            "level": "high", "type": "trend"
        })
    elif 5 <= m["savings_rate"] < 15 and m["income"] > 0:
        insights.append({
            "message": f"Savings at {m['savings_rate']}%. Trimming ₹{int(m['expense'] * 0.08)} from {m['top_cat_name']} could push you past 15%.",
            "level": "medium", "type": "trend"
        })

    # Expense spike
    if m["expense_change"] > 30:
        insights.append({
            "message": f"Expenses up {m['expense_change']}% vs last month (₹{int(m['p_expense'])} → ₹{int(m['expense'])}). {m['top_cat_name']} is {m['top_cat_pct']}% of spend.",
            "level": "high", "type": "category"
        })

    # ── GOAL-BASED INSIGHTS (NEW) ────────────────────────────────
    for g in m["goal_details"]:
        if g["target_amount"] <= 0:
            continue

        pct = g["progress_percent"]
        mr  = float(g["monthly_required"] or 0)

        # Goal falling behind
        if g["goal_risk"] == "high":
            insights.append({
                "message": f"Goal '{g['name']}' needs ₹{int(mr)}/mo but your surplus is only ₹{int(m['avg_monthly_surplus'])}. It may be delayed.",
                "level": "high", "type": "goal"
            })
        elif g["goal_risk"] == "medium":
            ml = g.get("months_left", "?")
            insights.append({
                "message": f"Goal '{g['name']}' is {pct}% funded with {ml} months left. Save ₹{int(mr)}/mo to stay on track.",
                "level": "medium", "type": "goal"
            })
        elif pct < 20 and m["surplus"] > 0 and mr > 0:
            months = round(g["remaining"] / m["avg_monthly_surplus"]) if m["avg_monthly_surplus"] > 0 else "?"
            insights.append({
                "message": f"Goal '{g['name']}' is {pct}% funded. At ₹{int(m['avg_monthly_surplus'])}/mo surplus, ~{months} months to go.",
                "level": "medium", "type": "goal"
            })

    # ── GOAL PRESSURE INSIGHT (NEW) ──────────────────────────────
    if m["goal_pressure"] > 70 and m["total_monthly_required"] > 0:
        insights.append({
            "message": f"Goal pressure is high ({m['goal_pressure']:.0f}/100). You need ₹{int(m['total_monthly_required'])}/mo for all goals but surplus is ₹{int(m['avg_monthly_surplus'])}.",
            "level": "high", "type": "goal"
        })

    order = {"high": 0, "medium": 1, "low": 2}
    insights = sorted(insights, key=lambda x: order.get(x["level"], 3))[:4]

    if not insights:
        insights.append({
            "message": f"Finances look healthy. Savings {m['savings_rate']}%, surplus ₹{int(m['surplus'])}. Keep it up!",
            "level": "low", "type": "trend"
        })

    return jsonify({"insights": insights})


# ── 2. RISK SCORE (goal-pressure aware) ─────────────────────────────
@ai_insights_bp.route("/risk-score")
@login_required
def risk_score():
    conn = get_db()
    try:
        m = _fetch_full_metrics(conn, session["user_id"])
    finally:
        _safe_close(conn)

    score = 100

    # Savings rate deductions
    if   m["savings_rate"] < 5:   score -= 30
    elif m["savings_rate"] < 15:  score -= 15
    elif m["savings_rate"] < 25:  score -= 5

    # Budget deductions
    if   m["budget_used_pct"] > 90: score -= 25
    elif m["budget_used_pct"] > 75: score -= 12
    elif m["budget_used_pct"] > 50: score -= 5

    # Expense growth deductions
    if   m["expense_change"] > 40: score -= 20
    elif m["expense_change"] > 20: score -= 10

    # Goal-pressure deductions (NEW)
    if   m["goal_pressure"] > 80: score -= 20
    elif m["goal_pressure"] > 60: score -= 12
    elif m["goal_pressure"] > 40: score -= 5

    # Per-goal funding check (NEW)
    for g in m["goal_details"]:
        if g["target_amount"] > 0:
            if g["progress_percent"] < 10:
                score -= 5
            if g["goal_risk"] == "high":
                score -= 5

    score = max(0, min(100, score))

    if   score >= 70: risk, tip = "low",    f"Stable. Savings {m['savings_rate']}%, budget {m['budget_used_pct']}% used. Goal pressure: {m['goal_pressure']:.0f}/100."
    elif score >= 40: risk, tip = "medium", f"Moderate risk. Budget {m['budget_used_pct']}% used, goal pressure {m['goal_pressure']:.0f}/100."
    else:             risk, tip = "high",   f"High risk! Budget {m['budget_used_pct']}% used, savings {m['savings_rate']}%, goal pressure {m['goal_pressure']:.0f}/100."

    return jsonify({
        "health_score":      score,
        "risk_level":        risk,
        "tooltip":           tip,
        "savings_rate":      m["savings_rate"],
        "budget_used":       m["budget_used_pct"],
        "goal_pressure":     m["goal_pressure"],
        "combined_risk":     m["combined_risk"],
        "combined_risk_score": m["combined_risk_score"],
    })


# ── 3. BADGE COUNT ───────────────────────────────────────────────────
@ai_insights_bp.route("/insight-badge")
@login_required
def insight_badge():
    conn = get_db()
    try:
        m = _fetch_full_metrics(conn, session["user_id"])
    finally:
        _safe_close(conn)

    high = medium = 0

    if m["budget"] > 0:
        if   m["budget_used_pct"] >= 85: high   += 1
        elif m["budget_used_pct"] >= 65: medium += 1

    if m["income"] > 0:
        if   m["savings_rate"] < 5:  high   += 1
        elif m["savings_rate"] < 15: medium += 1

    if   m["expense_change"] > 30: high   += 1
    elif m["expense_change"] > 15: medium += 1

    # Goal pressure badge (NEW)
    if   m["goal_pressure"] > 70: high   += 1
    elif m["goal_pressure"] > 40: medium += 1

    color = "red" if high > 0 else ("yellow" if medium > 0 else "green")
    return jsonify({"count": high + medium, "color": color, "high": high, "medium": medium})


# ── 4. SMART NUDGE ───────────────────────────────────────────────────
@ai_insights_bp.route("/smart-nudge")
@login_required
def smart_nudge():
    conn = get_db()
    try:
        m = _fetch_full_metrics(conn, session["user_id"])
    finally:
        _safe_close(conn)

    # Show nudge if: month-end OR goal pressure is high
    show_for_goals = m["goal_pressure"] > 60 and m["total_monthly_required"] > m["avg_monthly_surplus"]
    show_for_budget = m["today_day"] > 20 and (m["budget_used_pct"] >= 75 or m["savings_rate"] < 10)

    if not show_for_goals and not show_for_budget:
        return jsonify({"nudge": None})

    days_left  = max(m["days_left"], 1)
    safe_daily = (m["budget"] - m["expense"]) / days_left if m["budget"] > 0 else 0
    reduction  = max(0, round(m["daily_burn"] - safe_daily))

    if show_for_goals:
        shortfall = max(0, int(m["total_monthly_required"] - m["avg_monthly_surplus"]))
        msg = (
            f"Goal pressure is high — you need ₹{int(m['total_monthly_required'])}/mo for your goals "
            f"but surplus is ₹{int(m['avg_monthly_surplus'])}. "
            f"Cutting {m['top_cat_name']} (₹{shortfall} gap) would help significantly."
        )
    else:
        msg = (
            f"You are at {m['budget_used_pct']}% of your budget with {days_left} days left. "
            f"Cutting ₹{reduction}/day — especially in {m['top_cat_name']} ({m['top_cat_pct']}%) — will keep you on track."
        )

    return jsonify({"nudge": {
        "message":   msg,
        "days_left": days_left,
        "reduction": reduction,
        "goal_pressure": m["goal_pressure"],
    }})


# ── 5. GOAL INTELLIGENCE ENDPOINT (NEW) ─────────────────────────────
@ai_insights_bp.route("/goal-intelligence")
@login_required
def goal_intelligence():
    """
    Returns per-goal AI insights: savings suggestions, risk level,
    monthly targets, and delay warnings. Consumed by GoalsScreen and
    the unified financial provider.
    """
    conn = get_db()
    try:
        m = _fetch_full_metrics(conn, session["user_id"])
    finally:
        _safe_close(conn)

    results = []
    for g in m["goal_details"]:
        if g["target_amount"] <= 0:
            continue

        insight_msg = None
        savings_tip = None

        mr  = float(g["monthly_required"] or 0)
        ml  = g["months_left"]
        pct = g["progress_percent"]
        rem = float(g["remaining"] or 0)

        if g["goal_risk"] == "high":
            shortfall = max(0, mr - m["avg_monthly_surplus"])
            insight_msg = (
                f"Needs ₹{int(mr)}/mo but surplus is ₹{int(m['avg_monthly_surplus'])}. "
                f"Reduce {m['top_cat_name']} by ₹{int(shortfall)} to close the gap."
            )
            savings_tip = f"Cut {m['top_cat_name']} spending by 15% to free ₹{int(m['expense'] * m['top_cat_pct'] / 100 * 0.15)}."
        elif g["goal_risk"] == "medium":
            insight_msg = f"{pct:.0f}% funded. Save ₹{int(mr)}/mo for {ml} more months."
            savings_tip = f"Automate ₹{int(mr * 0.5)} bi-weekly transfers for discipline."
        else:
            if pct >= 90:
                insight_msg = f"Almost there! Just ₹{int(rem)} remaining."
                savings_tip = "One extra contribution now closes this goal early."
            elif pct >= 50:
                insight_msg = f"Good progress at {pct:.0f}%. {ml} months remaining at current pace."
                savings_tip = "You're on track. Keep contributions consistent."
            else:
                insight_msg = f"Early stage — {pct:.0f}% funded. Consistency is key."
                savings_tip = f"Set up auto-transfer of ₹{int(mr)} on payday."

        results.append({
            **g,
            "insight":     insight_msg,
            "savings_tip": savings_tip,
        })

    return jsonify({
        "goal_intelligence": results,
        "goal_pressure":     m["goal_pressure"],
        "combined_risk":     m["combined_risk"],
        "avg_monthly_surplus": m["avg_monthly_surplus"],
        "total_monthly_required": m["total_monthly_required"],
    })


# ── 6. BEHAVIORAL PATTERNS ───────────────────────────────────────────
@ai_insights_bp.route("/behavioral-patterns")
@login_required
def behavioral_patterns():
    user_id = session["user_id"]
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT to_char(date,'YYYY-MM') AS month,
                   COALESCE(category,'Misc') AS category,
                   SUM(amount) AS total
            FROM transactions WHERE user_id=%s AND type='expense'
            GROUP BY month, category ORDER BY month DESC
        """, (user_id,)).fetchall()
        rate_rows = conn.execute("""
            SELECT to_char(date,'YYYY-MM') AS month,
                   COALESCE(SUM(CASE WHEN type='income' THEN amount ELSE 0 END),0) AS income,
                   COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END),0) AS expense
            FROM transactions WHERE user_id=%s GROUP BY month ORDER BY month DESC LIMIT 4
        """, (user_id,)).fetchall()
    finally:
        _safe_close(conn)

    patterns = []
    if not rows:
        return jsonify({"patterns": patterns})

    monthly = {}
    for r in rows:
        monthly.setdefault(r["month"], {})[r["category"]] = float(r["total"] or 0)
    months = sorted([m for m in monthly.keys() if m], reverse=True)
    if len(months) < 2:
        return jsonify({"patterns": patterns})

    cur_d = monthly[months[0]]
    prv_d = monthly[months[1]]

    for cat, ct in cur_d.items():
        pt = prv_d.get(cat, 0.0)
        if pt > 0:
            chg = (ct - pt) / pt * 100
            if chg > 30:
                patterns.append({
                    "title": f"Spending Spike: {cat}",
                    "description": f"{cat} up {round(chg)}% vs last month (₹{int(pt)} → ₹{int(ct)}).",
                    "severity": "high" if chg > 60 else "medium"
                })

    if cur_d and prv_d:
        ct = max(cur_d, key=cur_d.get)
        pt = max(prv_d, key=prv_d.get)
        if ct != pt:
            patterns.append({
                "title": f"New Top Category: {ct}",
                "description": f"{ct} overtook {pt} as your biggest spend this month.",
                "severity": "medium"
            })

    if len(months) >= 3:
        trd = monthly[months[2]]
        for cat in cur_d:
            if cat in prv_d and cat in trd:
                if (cur_d[cat] == max(cur_d.values()) and
                        prv_d[cat] == max(prv_d.values()) and
                        trd[cat] == max(trd.values())):
                    patterns.append({
                        "title": f"Consistent Top Spend: {cat}",
                        "description": f"{cat} has been your #1 expense for 3 straight months. Consider a sub-budget.",
                        "severity": "high"
                    })

    if len(rate_rows) >= 3:
        rates = [
            round((float(r["income"] or 0) - float(r["expense"] or 0)) / float(r["income"] or 0) * 100, 1)
            if float(r["income"] or 0) > 0 else 0.0
            for r in rate_rows
        ]
        if rates[0] < rates[1] < rates[2]:
            patterns.append({
                "title": "Savings Rate Declining",
                "description": f"Savings dropped 3 months in a row: {round(rates[2])}% → {round(rates[1])}% → {round(rates[0])}%.",
                "severity": "high"
            })

    seen, dedup = set(), []
    for p in patterns:
        if p["title"] not in seen:
            seen.add(p["title"])
            dedup.append(p)

    dedup.sort(key=lambda x: {"high": 0, "medium": 1, "low": 2}.get(x["severity"], 3))
    return jsonify({"patterns": dedup[:5]})


# ── 7. RECURRING DETECTION V2 (confidence-scored) ───────────────────
#
# Groups a user's expense transactions by normalized merchant/description
# and scores each group on several independent signals of "genuinely
# recurring financial obligation" rather than merely "seen >=2 times":
#
#   - occurrence_count / month_count  : enough history to trust the pattern
#   - amount stability (CV)           : recurring bills charge ~the same amount
#   - day-of-month stability          : recurring bills land on ~the same day
#   - interval regularity             : gaps between charges are ~monthly
#   - category/merchant suitability   : subscriptions/utilities/insurance/
#                                        loans/rent/telecom/memberships are
#                                        favored; groceries, restaurants,
#                                        shopping, P2P transfers, and misc
#                                        spend are excluded unless the
#                                        description itself names a bill
#                                        (e.g. "Rent" sent via P2P transfer)
#
# Hard evidence gates reject weak candidates outright; everything that
# passes the gates gets a 0-1 confidence score from a weighted blend of
# the signals above, and only candidates at/above CONFIDENCE_THRESHOLD
# are returned. Handled-state filtering (dismissed/added) is applied
# AFTER candidate generation, per design.

CONFIDENCE_THRESHOLD = 0.60

MIN_OCCURRENCES  = 3      # at least 3 charges
MIN_MONTHS       = 3      # ideally across 3 distinct months
MAX_AMOUNT_CV    = 0.35   # amount coefficient of variation ceiling
MAX_DAY_STDEV    = 10.0   # day-of-month standard deviation ceiling (days)
INTERVAL_MIN     = 20     # avg gap between charges must look ~monthly
INTERVAL_MAX     = 40

SUBSCRIPTION_KEYWORDS = {
    "netflix", "spotify", "prime video", "amazon prime", "hotstar",
    "disney", "youtube premium", "youtube music", "apple music",
    "gym", "membership", "subscription", "icloud", "google one",
}
BILL_KEYWORDS = {
    "electricity", "water bill", "gas bill", "insurance", "loan", "emi",
    "rent", "broadband", "wifi", "internet", "telecom", "postpaid",
    "mobile bill", "dth", "jio", "airtel", "vodafone", "vi ", "phone bill",
}
RECURRING_CATEGORIES = {
    "subscription", "subscriptions", "utilities", "utility", "telecom",
    "insurance", "loan", "loans", "emi", "membership", "memberships", "rent",
}
EXCLUDED_CATEGORIES = {
    "groceries", "grocery", "restaurant", "dining", "food", "shopping",
    "p2p", "transfer", "personal", "misc", "miscellaneous", "other",
    "stationery",
}


def _mean(vals):
    return sum(vals) / len(vals) if vals else 0.0


def _stdev(vals):
    if len(vals) < 2:
        return 0.0
    m = _mean(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))


def _classify(description, category):
    desc_l = (description or "").lower()
    cat_l  = (category or "").lower().strip()
    is_subscription_kw = any(kw in desc_l for kw in SUBSCRIPTION_KEYWORDS)
    is_bill_kw          = any(kw in desc_l for kw in BILL_KEYWORDS)

    if cat_l in {"subscription", "subscriptions"} or is_subscription_kw:
        return "subscription", True
    if cat_l in RECURRING_CATEGORIES or is_bill_kw:
        return "bill", True
    if cat_l in {"transfer", "p2p"} and (is_subscription_kw or is_bill_kw):
        return "recurring_transfer", True
    return "recurring_payment", False  # generic; whitelisted-ness handled by caller


def _score_candidate(description, category, dates, amounts, today):
    """
    dates: list of datetime.date, sorted ascending
    amounts: list of float, same order as dates
    Returns (confidence, avg_day, due_window, month_count, recurrence_type)
    or None if it fails a hard gate.
    """
    occurrence_count = len(dates)
    months = {d.strftime("%Y-%m") for d in dates}
    month_count = len(months)

    if occurrence_count < MIN_OCCURRENCES or month_count < MIN_MONTHS:
        return None

    avg_amount = _mean(amounts)
    amount_cv  = (_stdev(amounts) / avg_amount) if avg_amount > 0 else 1.0
    if amount_cv > MAX_AMOUNT_CV:
        return None

    days = [d.day for d in dates]
    avg_day    = _mean(days)
    day_stdev  = _stdev(days)
    if day_stdev > MAX_DAY_STDEV:
        return None

    intervals = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
    avg_interval = _mean(intervals) if intervals else 30
    if not (INTERVAL_MIN <= avg_interval <= INTERVAL_MAX):
        return None

    cat_l = (category or "").lower().strip()
    desc_l = (description or "").lower()
    keyword_hit = any(kw in desc_l for kw in SUBSCRIPTION_KEYWORDS | BILL_KEYWORDS)

    # Hard category gate: excluded categories (groceries, restaurants,
    # shopping, P2P, misc, ...) are rejected unless the description itself
    # names a bill/subscription (e.g. a P2P "Rent" transfer).
    if cat_l in EXCLUDED_CATEGORIES and not keyword_hit:
        return None

    recurrence_type, whitelisted = _classify(description, category)
    if whitelisted:
        category_score = 1.0
    elif cat_l in EXCLUDED_CATEGORIES:
        category_score = 0.0  # unreachable due to gate above, kept for clarity
    else:
        category_score = 0.5  # neutral/unlabeled category, not excluded either

    occurrence_score = min(1.0, occurrence_count / 6.0)
    month_score       = min(1.0, month_count / 6.0)
    amount_score       = max(0.0, 1.0 - (amount_cv / MAX_AMOUNT_CV))
    day_score          = max(0.0, 1.0 - (day_stdev / MAX_DAY_STDEV))
    interval_score     = max(0.0, 1.0 - abs(avg_interval - 30) / 15.0)

    confidence = (
        occurrence_score * 0.15 +
        month_score       * 0.15 +
        amount_score       * 0.20 +
        day_score           * 0.15 +
        interval_score       * 0.15 +
        category_score       * 0.20
    )

    if confidence < CONFIDENCE_THRESHOLD:
        return None

    # Preserve the ±5-day "due soon" window only when the historical
    # evidence is strong (tight day-of-month clustering); widen slightly
    # for looser-but-still-passing patterns, capped at 10 days.
    due_window = 5 if day_stdev <= 3 else min(10, int(round(day_stdev)) + 5)

    return round(confidence, 2), int(round(avg_day)), due_window, month_count, recurrence_type


def _build_recurring_candidates(conn, user_id):
    """Fetch raw expense transactions and score each merchant group."""
    rows = conn.execute("""
        SELECT description, amount, COALESCE(category,'Misc') AS category, date::date AS date
        FROM transactions
        WHERE user_id=%s AND type='expense'
        ORDER BY date ASC
    """, (user_id,)).fetchall()

    groups = {}
    for r in rows:
        key = re.sub(r"\s+", " ", (r["description"] or "").strip().lower())
        if not key:
            continue
        g = groups.setdefault(key, {"description": r["description"], "category": r["category"],
                                     "dates": [], "amounts": []})
        g["dates"].append(r["date"])
        g["amounts"].append(float(r["amount"] or 0))
        g["category"] = r["category"] or g["category"]  # most recent category wins

    candidates = []
    for key, g in groups.items():
        scored = _score_candidate(g["description"], g["category"], g["dates"], g["amounts"], date.today())
        if not scored:
            continue
        confidence, avg_day, due_window, month_count, recurrence_type = scored
        candidates.append({
            "key":              key,
            "description":      g["description"],
            "category":         g["category"],
            "amount":           round(_mean(g["amounts"]), 2),
            "avg_day":          avg_day,
            "due_window":       due_window,
            "month_count":      month_count,
            "confidence":       confidence,
            "recurrence_type":  recurrence_type,
        })

    candidates.sort(key=lambda c: c["confidence"], reverse=True)
    return candidates


@ai_insights_bp.route("/recurring-suggestions-v2")
@login_required
def recurring_suggestions_v2():
    user_id = session["user_id"]
    conn    = get_db()
    today   = date.today()
    this_month = today.strftime("%Y-%m")

    try:
        candidates = _build_recurring_candidates(conn, user_id)

        added = set(
            r["description"].lower().strip()
            for r in conn.execute("""
                SELECT description FROM transactions
                WHERE user_id=%s AND type='expense' AND to_char(date,'YYYY-MM')=%s
            """, (user_id, this_month)).fetchall()
        )

        # Suggestions the user has already dismissed or added for this
        # occurrence period (persisted so a page refresh / dashboard
        # revisit does not re-prompt for the same occurrence).
        handled = set(
            r["suggestion_key"]
            for r in conn.execute("""
                SELECT suggestion_key FROM recurring_suggestion_state
                WHERE user_id=%s AND occurrence_period=%s
                AND status IN ('dismissed','added')
            """, (user_id, this_month)).fetchall()
        )
    finally:
        _safe_close(conn)

    out = []
    for c in candidates:
        # Due-soon eligibility window: only surface bills that are
        # actually due around now, using the confidence-adjusted window.
        if abs(today.day - c["avg_day"]) > c["due_window"]:
            continue
        # Handled-state filtering happens AFTER candidate generation.
        if c["key"] in added or c["key"] in handled:
            continue
        out.append({
            "description":       c["description"],
            "amount":            c["amount"],
            "category":          c["category"],
            "avg_day":           c["avg_day"],
            "month_count":       c["month_count"],
            "confidence":        c["confidence"],
            "recurrence_type":   c["recurrence_type"],
            "occurrence_period": this_month,
        })

    return jsonify(out[:5])


# ── 8. RECURRING SUGGESTION STATE (persist handled/dismissed) ───────
@ai_insights_bp.route("/recurring-suggestions-v2/mark", methods=["POST"])
@login_required
def mark_recurring_suggestion():
    """
    Persists that a recurring-suggestion occurrence has been handled
    (added or dismissed) so it is not shown again for that occurrence
    period, across page reloads and dashboard revisits. The underlying
    recurring detection is untouched — once a new occurrence period
    arrives (e.g. next month), the suggestion becomes eligible again
    because this row only applies to the specific occurrence_period.
    """
    data = request.get_json(silent=True) or {}
    description       = (data.get("description") or "").strip()
    occurrence_period = (data.get("occurrence_period") or "").strip()
    status             = (data.get("status") or "").strip()

    if not description or not occurrence_period or status not in ("dismissed", "added"):
        return jsonify({"error": "description, occurrence_period and a valid status are required"}), 400

    suggestion_key = description.lower().strip()
    user_id = session["user_id"]

    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO recurring_suggestion_state
                (user_id, suggestion_key, occurrence_period, status, handled_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (user_id, suggestion_key, occurrence_period)
            DO UPDATE SET status = EXCLUDED.status, handled_at = NOW()
        """, (user_id, suggestion_key, occurrence_period, status))
        conn.commit()
    finally:
        _safe_close(conn)

    return jsonify({"ok": True})