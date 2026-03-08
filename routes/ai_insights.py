# ============================================================
# FILE: routes/ai_insights.py
# Register in app.py:
#   from routes.ai_insights import ai_insights_bp
#   app.register_blueprint(ai_insights_bp)
# ============================================================

from flask import Blueprint, jsonify, session
from utils.db import get_db
from utils.decorators import login_required
from datetime import datetime, date, timedelta

ai_insights_bp = Blueprint("ai_insights", __name__)


def _safe_close(conn):
    try: conn.close()
    except: pass


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
    cur_start, prev_start, prev_end = _get_month_bounds()
    today = datetime.today()

    cur = conn.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN type='income'  THEN amount ELSE 0 END),0) AS income,
            COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END),0) AS expense
        FROM transactions WHERE user_id=? AND date>=?
    """, (user_id, cur_start)).fetchone()

    prev = conn.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN type='income'  THEN amount ELSE 0 END),0) AS income,
            COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END),0) AS expense
        FROM transactions WHERE user_id=? AND date>=? AND date<=?
    """, (user_id, prev_start, prev_end)).fetchone()

    budget_row = conn.execute(
        "SELECT COALESCE(amount,0) AS amount FROM budgets WHERE user_id=?", (user_id,)
    ).fetchone()

    top_cat = conn.execute("""
        SELECT COALESCE(category,'Misc') AS category, SUM(amount) AS total
        FROM transactions WHERE user_id=? AND type='expense' AND date>=?
        GROUP BY category ORDER BY total DESC LIMIT 1
    """, (user_id, cur_start)).fetchone()

    goals = conn.execute(
        "SELECT name, target_amount, saved_amount FROM goals WHERE user_id=?", (user_id,)
    ).fetchall()

    income    = float(cur["income"])
    expense   = float(cur["expense"])
    surplus   = income - expense
    budget    = float(budget_row["amount"]) if budget_row else 0.0
    p_expense = float(prev["expense"])
    p_income  = float(prev["income"])

    savings_rate    = round(surplus/income*100, 1)       if income  > 0 else 0.0
    budget_used_pct = round(expense/budget*100, 1)       if budget  > 0 else 0.0
    expense_change  = round((expense-p_expense)/p_expense*100, 1) if p_expense > 0 else 0.0
    days_passed     = max(today.day, 1)
    days_left       = max(30 - days_passed, 0)
    daily_burn      = expense / days_passed
    top_cat_name    = top_cat["category"] if top_cat else "N/A"
    top_cat_pct     = round(top_cat["total"]/expense*100,1) if top_cat and expense>0 else 0.0

    return dict(
        income=income, expense=expense, surplus=surplus, budget=budget,
        savings_rate=savings_rate, budget_used_pct=budget_used_pct,
        expense_change=expense_change, p_income=p_income, p_expense=p_expense,
        days_left=days_left, days_passed=days_passed, daily_burn=daily_burn,
        today_day=today.day, top_cat_name=top_cat_name, top_cat_pct=top_cat_pct,
        goals=[dict(g) for g in goals],
    )


# ── 1. PROACTIVE AI INSIGHTS ──────────────────────────────────────
@ai_insights_bp.route("/ai-insights")
@login_required
def ai_insights():
    conn = get_db()
    try:    m = _fetch_full_metrics(conn, session["user_id"])
    finally: _safe_close(conn)

    insights = []

    if m["budget"] > 0 and m["budget_used_pct"] >= 85:
        cut = int((m["expense"]-m["budget"]) / max(m["days_left"],1))
        insights.append({"message": f"Budget at {m['budget_used_pct']}% — Rs.{int(m['expense'])} of Rs.{int(m['budget'])} used. Cut Rs.{cut}/day to avoid overspend.", "level":"high","type":"budget"})
    elif m["budget"] > 0 and m["budget_used_pct"] >= 65:
        safe = int((m["budget"]-m["expense"]) / max(m["days_left"],1))
        insights.append({"message": f"Budget {m['budget_used_pct']}% used. Rs.{int(m['budget']-m['expense'])} left for {m['days_left']} days — pace at Rs.{safe}/day.", "level":"medium","type":"budget"})

    if m["savings_rate"] < 5 and m["income"] > 0:
        save = int(m["expense"] * m["top_cat_pct"]/100 * 0.15)
        insights.append({"message": f"Savings rate only {m['savings_rate']}%. Cutting {m['top_cat_name']} by 15% would free Rs.{save} this month.", "level":"high","type":"trend"})
    elif 5 <= m["savings_rate"] < 15 and m["income"] > 0:
        insights.append({"message": f"Savings at {m['savings_rate']}%. Trimming Rs.{int(m['expense']*0.08)} from {m['top_cat_name']} could push you past 15%.", "level":"medium","type":"trend"})

    if m["expense_change"] > 30:
        insights.append({"message": f"Expenses up {m['expense_change']}% vs last month (Rs.{int(m['p_expense'])} to Rs.{int(m['expense'])}). {m['top_cat_name']} is {m['top_cat_pct']}% of spend.", "level":"high","type":"category"})
    elif 15 < m["expense_change"] <= 30:
        insights.append({"message": f"Spending rose {m['expense_change']}% vs last month. Monitor {m['top_cat_name']} — {m['top_cat_pct']}% of expenses.", "level":"medium","type":"category"})

    for g in m["goals"]:
        tgt  = float(g.get("target_amount",0))
        saved = float(g.get("saved_amount",0))
        if tgt > 0:
            pct = round(saved/tgt*100,1)
            if pct < 20 and m["surplus"] > 0:
                months = round((tgt-saved)/m["surplus"])
                insights.append({"message": f"Goal '{g['name']}' is {pct}% funded. At Rs.{int(m['surplus'])}/mo surplus, ~{months} months to go.", "level":"medium","type":"goal"})

    order = {"high":0,"medium":1,"low":2}
    insights = sorted(insights, key=lambda x: order.get(x["level"],3))[:3]
    if not insights:
        insights.append({"message": f"Finances look healthy. Savings {m['savings_rate']}%, surplus Rs.{int(m['surplus'])}. Keep it up!", "level":"low","type":"trend"})

    return jsonify({"insights": insights})


# ── 2+3. RISK SCORE ───────────────────────────────────────────────
@ai_insights_bp.route("/risk-score")
@login_required
def risk_score():
    conn = get_db()
    try:    m = _fetch_full_metrics(conn, session["user_id"])
    finally: _safe_close(conn)

    score = 100
    if   m["savings_rate"] < 5:     score -= 30
    elif m["savings_rate"] < 15:    score -= 15
    elif m["savings_rate"] < 25:    score -= 5
    if   m["budget_used_pct"] > 90: score -= 25
    elif m["budget_used_pct"] > 75: score -= 12
    elif m["budget_used_pct"] > 50: score -= 5
    if   m["expense_change"] > 40:  score -= 20
    elif m["expense_change"] > 20:  score -= 10
    for g in m["goals"]:
        if float(g.get("target_amount",0)) > 0 and float(g.get("saved_amount",0))/float(g["target_amount"]) < 0.1:
            score -= 5
    score = max(0, min(100, score))

    if   score >= 70: risk, tip = "low",    f"Stable. Savings {m['savings_rate']}%, budget {m['budget_used_pct']}% used."
    elif score >= 40: risk, tip = "medium", f"Moderate risk. Budget {m['budget_used_pct']}% used, savings {m['savings_rate']}%."
    else:             risk, tip = "high",   f"High risk! Budget {m['budget_used_pct']}% used, savings only {m['savings_rate']}%."

    return jsonify({"health_score":score,"risk_level":risk,"tooltip":tip,"savings_rate":m["savings_rate"],"budget_used":m["budget_used_pct"]})


# ── BADGE COUNT ───────────────────────────────────────────────────
@ai_insights_bp.route("/insight-badge")
@login_required
def insight_badge():
    conn = get_db()
    try:    m = _fetch_full_metrics(conn, session["user_id"])
    finally: _safe_close(conn)
    high = medium = 0
    if m["budget"]>0:
        if   m["budget_used_pct"]>=85: high+=1
        elif m["budget_used_pct"]>=65: medium+=1
    if m["income"]>0:
        if   m["savings_rate"]<5:  high+=1
        elif m["savings_rate"]<15: medium+=1
    if   m["expense_change"]>30: high+=1
    elif m["expense_change"]>15: medium+=1
    color = "red" if high>0 else ("yellow" if medium>0 else "green")
    return jsonify({"count":high+medium,"color":color,"high":high,"medium":medium})


# ── 4. SMART NUDGE ───────────────────────────────────────────────
@ai_insights_bp.route("/smart-nudge")
@login_required
def smart_nudge():
    conn = get_db()
    try:    m = _fetch_full_metrics(conn, session["user_id"])
    finally: _safe_close(conn)
    if m["today_day"]<=20 or (m["budget_used_pct"]<75 and m["savings_rate"]>=10):
        return jsonify({"nudge":None})
    days_left  = max(m["days_left"],1)
    safe_daily = (m["budget"]-m["expense"])/days_left if m["budget"]>0 else 0
    reduction  = max(0, round(m["daily_burn"]-safe_daily))
    if m["budget_used_pct"]>=75:
        msg = f"You are at {m['budget_used_pct']}% of your budget with {days_left} days left. Cutting Rs.{reduction}/day — especially in {m['top_cat_name']} ({m['top_cat_pct']}%) — will keep you on track."
    else:
        gap = max(0, int(m["income"]*0.2 - m["surplus"]))
        msg = f"Savings rate is {m['savings_rate']}% this month. With {days_left} days left, saving Rs.{gap} more is achievable."
    return jsonify({"nudge":{"message":msg,"days_left":days_left,"reduction":reduction}})


# ── 5. BEHAVIORAL PATTERNS ───────────────────────────────────────
@ai_insights_bp.route("/behavioral-patterns")
@login_required
def behavioral_patterns():
    user_id = session["user_id"]
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT strftime('%Y-%m',date) AS month, COALESCE(category,'Misc') AS category, SUM(amount) AS total
            FROM transactions WHERE user_id=? AND type='expense'
            GROUP BY month,category ORDER BY month DESC
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
    if not rows: return jsonify({"patterns":patterns})

    monthly = {}
    for r in rows: monthly.setdefault(r["month"],{})[r["category"]] = float(r["total"])
    months = sorted([m for m in monthly.keys() if m], reverse=True)
    if len(months) < 2: return jsonify({"patterns":patterns})

    cur_d = monthly[months[0]]; prv_d = monthly[months[1]]

    for cat, ct in cur_d.items():
        pt = prv_d.get(cat,0)
        if pt > 0:
            chg = (ct-pt)/pt*100
            if chg > 30:
                patterns.append({"title":f"Spending Spike: {cat}","description":f"{cat} up {round(chg)}% vs last month (Rs.{int(pt)} to Rs.{int(ct)}).","severity":"high" if chg>60 else "medium"})

    if cur_d and prv_d:
        ct = max(cur_d, key=cur_d.get); pt = max(prv_d, key=prv_d.get)
        if ct != pt:
            patterns.append({"title":f"New Top Category: {ct}","description":f"{ct} overtook {pt} as your biggest spend this month.","severity":"medium"})

    if len(months) >= 3:
        trd = monthly[months[2]]
        for cat in cur_d:
            if cat in prv_d and cat in trd:
                if cur_d[cat]==max(cur_d.values()) and prv_d[cat]==max(prv_d.values()) and trd[cat]==max(trd.values()):
                    patterns.append({"title":f"Consistent Top Spend: {cat}","description":f"{cat} has been your #1 expense for 3 straight months. Consider a sub-budget.","severity":"high"})

    if len(rate_rows) >= 3:
        rates = [round((float(r["income"])-float(r["expense"]))/float(r["income"])*100,1) if float(r["income"])>0 else 0 for r in rate_rows]
        if rates[0] < rates[1] < rates[2]:
            patterns.append({"title":"Savings Rate Declining","description":f"Savings dropped 3 months in a row: {round(rates[2])}% -> {round(rates[1])}% -> {round(rates[0])}%.","severity":"high"})

    seen, dedup = set(), []
    for p in patterns:
        if p["title"] not in seen: seen.add(p["title"]); dedup.append(p)
    dedup.sort(key=lambda x:{"high":0,"medium":1,"low":2}.get(x["severity"],3))
    return jsonify({"patterns":dedup[:5]})


# ── 6. RECURRING DETECTION V2 ────────────────────────────────────
@ai_insights_bp.route("/recurring-suggestions-v2")
@login_required
def recurring_suggestions_v2():
    user_id = session["user_id"]; conn = get_db(); today = date.today()
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
        added = set(r["description"].lower().strip() for r in conn.execute("""
            SELECT description FROM transactions WHERE user_id=? AND type='expense' AND strftime('%Y-%m',date)=?
        """, (user_id, this_month)).fetchall())
    finally:
        _safe_close(conn)

    out = []
    for r in rows:
        avg_day = int(r["avg_day"] or 15)
        if abs(today.day - avg_day) > 5: continue
        if r["description"].lower().strip() in added: continue
        out.append({"description":r["description"],"amount":float(r["avg_amount"]),"category":r["category"],"avg_day":avg_day,"month_count":int(r["month_count"])})
    return jsonify(out[:5])