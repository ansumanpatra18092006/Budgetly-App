# ============================================================
# FILE: routes/ai_insights.py  [UNIFIED FINANCIAL INTELLIGENCE]
# ============================================================

from flask import Blueprint, jsonify, session
from utils.db import get_db
from utils.decorators import login_required
from datetime import datetime, date, timedelta

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
        FROM transactions WHERE user_id=? AND date>=?
    """, (user_id, cur_start)).fetchone()

    # ── Previous month ───────────────────────────────────────────
    prev = conn.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN type='income'  THEN amount ELSE 0 END),0) AS income,
            COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END),0) AS expense
        FROM transactions WHERE user_id=? AND date>=? AND date<=?
    """, (user_id, prev_start, prev_end)).fetchone()

    # ── Budget ───────────────────────────────────────────────────
    budget_row = conn.execute(
        "SELECT COALESCE(amount,0) AS amount FROM budgets WHERE user_id=?",
        (user_id,)
    ).fetchone()

    # ── Top spending category ────────────────────────────────────
    top_cat = conn.execute("""
        SELECT COALESCE(category,'Misc') AS category, SUM(amount) AS total
        FROM transactions WHERE user_id=? AND type='expense' AND date>=?
        GROUP BY category ORDER BY total DESC LIMIT 1
    """, (user_id, cur_start)).fetchone()

    # ── Goals with full detail ───────────────────────────────────
    goal_rows = conn.execute(
        """SELECT id, name, target_amount, saved_amount, category, target_date
           FROM goals WHERE user_id=? ORDER BY id ASC""",
        (user_id,)
    ).fetchall()

    # ── Monthly cash flow (last 3 months for forecasting) ────────
    hist_rows = conn.execute("""
        SELECT strftime('%Y-%m',date) AS month,
               SUM(CASE WHEN type='income'  THEN amount ELSE 0 END) AS inc,
               SUM(CASE WHEN type='expense' THEN amount ELSE 0 END) AS exp
        FROM transactions WHERE user_id=?
        GROUP BY month ORDER BY month DESC LIMIT 3
    """, (user_id,)).fetchall()

    # ── Anomaly count (last 30 days) ─────────────────────────────
    recent_amounts_row = conn.execute("""
        SELECT amount FROM transactions
        WHERE user_id=? AND type='expense' ORDER BY date ASC
    """, (user_id,)).fetchall()

    # ═══════════════════════════════════════════════════════════
    # COMPUTE BASE METRICS
    # ═══════════════════════════════════════════════════════════
    income    = float(cur["income"])
    expense   = float(cur["expense"])
    surplus   = income - expense
    budget    = float(budget_row["amount"]) if budget_row else 0.0
    p_expense = float(prev["expense"])
    p_income  = float(prev["income"])

    savings_rate    = round(surplus / income * 100, 1)          if income  > 0 else 0.0
    budget_used_pct = round(expense / budget * 100, 1)          if budget  > 0 else 0.0
    expense_change  = round((expense - p_expense) / p_expense * 100, 1) if p_expense > 0 else 0.0

    days_passed  = max(today.day, 1)
    days_left    = max(30 - days_passed, 0)
    daily_burn   = expense / days_passed

    top_cat_name = top_cat["category"] if top_cat else "N/A"
    top_cat_pct  = round(top_cat["total"] / expense * 100, 1) if top_cat and expense > 0 else 0.0

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
        g["monthly_required"] or 0 for g in goal_details if g["monthly_required"]
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
        mr  = g["monthly_required"]

        # Goal falling behind
        if g["goal_risk"] == "high":
            insights.append({
                "message": f"Goal '{g['name']}' needs ₹{int(mr or 0)}/mo but your surplus is only ₹{int(m['avg_monthly_surplus'])}. It may be delayed.",
                "level": "high", "type": "goal"
            })
        elif g["goal_risk"] == "medium":
            ml = g.get("months_left", "?")
            insights.append({
                "message": f"Goal '{g['name']}' is {pct}% funded with {ml} months left. Save ₹{int(mr or 0)}/mo to stay on track.",
                "level": "medium", "type": "goal"
            })
        elif pct < 20 and m["surplus"] > 0 and mr:
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

        mr  = g["monthly_required"]
        ml  = g["months_left"]
        pct = g["progress_percent"]
        rem = g["remaining"]

        if g["goal_risk"] == "high":
            shortfall = max(0, (mr or 0) - m["avg_monthly_surplus"])
            insight_msg = (
                f"Needs ₹{int(mr or 0)}/mo but surplus is ₹{int(m['avg_monthly_surplus'])}. "
                f"Reduce {m['top_cat_name']} by ₹{int(shortfall)} to close the gap."
            )
            savings_tip = f"Cut {m['top_cat_name']} spending by 15% to free ₹{int(m['expense'] * m['top_cat_pct'] / 100 * 0.15)}."
        elif g["goal_risk"] == "medium":
            insight_msg = f"{pct:.0f}% funded. Save ₹{int(mr or 0)}/mo for {ml} more months."
            savings_tip = f"Automate ₹{int((mr or 0) * 0.5)} bi-weekly transfers for discipline."
        else:
            if pct >= 90:
                insight_msg = f"Almost there! Just ₹{int(rem)} remaining."
                savings_tip = "One extra contribution now closes this goal early."
            elif pct >= 50:
                insight_msg = f"Good progress at {pct:.0f}%. {ml} months remaining at current pace."
                savings_tip = "You're on track. Keep contributions consistent."
            else:
                insight_msg = f"Early stage — {pct:.0f}% funded. Consistency is key."
                savings_tip = f"Set up auto-transfer of ₹{int(mr or 0)} on payday."

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
            SELECT strftime('%Y-%m',date) AS month,
                   COALESCE(category,'Misc') AS category,
                   SUM(amount) AS total
            FROM transactions WHERE user_id=? AND type='expense'
            GROUP BY month, category ORDER BY month DESC
        """, (user_id,)).fetchall()
        rate_rows = conn.execute("""
            SELECT strftime('%Y-%m',date) AS month,
                   COALESCE(SUM(CASE WHEN type='income' THEN amount ELSE 0 END),0) AS income,
                   COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END),0) AS expense
            FROM transactions WHERE user_id=? GROUP BY month ORDER BY month DESC LIMIT 4
        """, (user_id,)).fetchall()
    finally:
        _safe_close(conn)

    patterns = []
    if not rows:
        return jsonify({"patterns": patterns})

    monthly = {}
    for r in rows:
        monthly.setdefault(r["month"], {})[r["category"]] = float(r["total"])
    months = sorted([m for m in monthly.keys() if m], reverse=True)
    if len(months) < 2:
        return jsonify({"patterns": patterns})

    cur_d = monthly[months[0]]
    prv_d = monthly[months[1]]

    for cat, ct in cur_d.items():
        pt = prv_d.get(cat, 0)
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
            round((float(r["income"]) - float(r["expense"])) / float(r["income"]) * 100, 1)
            if float(r["income"]) > 0 else 0
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


# ── 7. RECURRING DETECTION V2 ────────────────────────────────────────
@ai_insights_bp.route("/recurring-suggestions-v2")
@login_required
def recurring_suggestions_v2():
    user_id = session["user_id"]
    conn    = get_db()
    today   = date.today()
    this_month = today.strftime("%Y-%m")

    try:
        rows = conn.execute("""
            SELECT description, COALESCE(category,'Misc') AS category,
                   ROUND(AVG(amount),2) AS avg_amount,
                   COUNT(DISTINCT strftime('%Y-%m',date)) AS month_count,
                   AVG(CAST(strftime('%d',date) AS INTEGER)) AS avg_day
            FROM transactions WHERE user_id=? AND type='expense'
            GROUP BY LOWER(TRIM(description)) HAVING month_count>=2 ORDER BY avg_amount DESC
        """, (user_id,)).fetchall()

        added = set(
            r["description"].lower().strip()
            for r in conn.execute("""
                SELECT description FROM transactions
                WHERE user_id=? AND type='expense' AND strftime('%Y-%m',date)=?
            """, (user_id, this_month)).fetchall()
        )
    finally:
        _safe_close(conn)

    out = []
    for r in rows:
        avg_day = int(r["avg_day"] or 15)
        if abs(today.day - avg_day) > 5:
            continue
        if r["description"].lower().strip() in added:
            continue
        out.append({
            "description": r["description"],
            "amount":      float(r["avg_amount"]),
            "category":    r["category"],
            "avg_day":     avg_day,
            "month_count": int(r["month_count"]),
        })

    return jsonify(out[:5])