from flask import Blueprint, jsonify, session
from utils.db import get_db
from utils.decorators import login_required
from datetime import datetime

# ML imports
from ml.forecast_model import predict_next_month
from ml.anomaly_model import detect_anomalies
from ml.risk_model import predict_risk

from ml.recommender import get_recommendations

insights_bp = Blueprint("insights", __name__)


def get_month_start():
    """Return the first day of the current month as a date string (YYYY-MM-01)."""
    return datetime.today().strftime("%Y-%m-01")


def _fetch_current_month_totals(conn, user_id, month_start):
    """
    Single query that returns current-month income, expense, and the user's budget.
    Avoids issuing multiple round-trips for the same data.
    """
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN type = 'income'  THEN amount ELSE 0 END), 0) AS income,
            COALESCE(SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END), 0) AS expense
        FROM transactions
        WHERE user_id = ?
          AND date   >= ?
        """,
        (user_id, month_start),
    ).fetchone()

    budget_row = conn.execute(
        "SELECT amount FROM budgets WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    income  = row["income"]  or 0
    expense = row["expense"] or 0
    budget  = budget_row["amount"] if budget_row else 0

    return income, expense, budget


# ================= PREDICT NEXT MONTH EXPENSE (ALL HISTORY) =================
@insights_bp.route("/predict-expense")
@login_required
def predict_expense():
    user_id = session["user_id"]
    conn    = get_db()

    try:
        rows = conn.execute(
            """
            SELECT
                strftime('%Y-%m', date) AS month,
                SUM(amount)             AS total
            FROM transactions
            WHERE user_id = ?
              AND type    = 'expense'
            GROUP BY month
            ORDER BY month ASC
            """,
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    expenses   = [r["total"] for r in rows]
    prediction = predict_next_month(expenses)

    return jsonify({"predicted_expense": prediction})


# ================= HEALTH METRICS (CURRENT MONTH) =================
@insights_bp.route("/health-metrics")
@login_required
def health_metrics():
    user_id     = session["user_id"]
    month_start = get_month_start()
    conn        = get_db()

    try:
        income, expense, budget = _fetch_current_month_totals(conn, user_id, month_start)
    finally:
        conn.close()

    savings_rate     = ((income - expense) / income * 100) if income > 0 else 0
    budget_adherence = max(0.0, 100 - (expense / budget * 100)) if budget > 0 else 0

    health_score = int(0.5 * savings_rate + 0.5 * budget_adherence)
    # Clamp to [0, 100]
    health_score = max(0, min(100, health_score))

    return jsonify({
        "health_score":      round(health_score),
        "savings_rate":      round(savings_rate),
        "budget_adherence":  round(budget_adherence),
        "income_stability":  100,
    })


# ================= RECOMMENDATIONS (CURRENT MONTH) =================
@insights_bp.route("/recommendations")
@login_required
def recommendations():
    user_id = session["user_id"]
    recs = get_recommendations(user_id)
    return jsonify({"recommendations": recs})


# ================= RISK ANALYSIS (CURRENT MONTH) =================
@insights_bp.route("/risk-analysis")
@login_required
def risk_analysis():
    user_id = session["user_id"]
    month_start = get_month_start()
    conn = get_db()

    try:
        # Current month totals
        row = conn.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN type='income' THEN amount ELSE 0 END),0) AS income,
                COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END),0) AS expense
            FROM transactions
            WHERE user_id=? AND date>=?
        """, (user_id, month_start)).fetchone()

        income = row["income"]
        expense = row["expense"]

        budget_row = conn.execute(
            "SELECT amount FROM budgets WHERE user_id=?",
            (user_id,)
        ).fetchone()

        budget = budget_row["amount"] if budget_row else 0

        # -------- Stabilized burn rate --------
        today = datetime.today()
        days_passed = max(today.day, 7)   # prevents early-month spikes
        daily_burn = expense / days_passed if days_passed > 0 else 0

        current_projection = daily_burn * 30

        # -------- Historical average (last 6 months) --------
        hist_rows = conn.execute("""
            SELECT strftime('%Y-%m', date) AS month,
                   SUM(amount) AS total
            FROM transactions
            WHERE user_id=? AND type='expense'
            GROUP BY month
            ORDER BY month DESC
            LIMIT 6
        """, (user_id,)).fetchall()

        hist_values = [r["total"] for r in hist_rows if r["total"]]

        historical_avg = sum(hist_values) / len(hist_values) if hist_values else current_projection

    finally:
        conn.close()

    # -------- Final projected expense (smoothed) --------
    projected_expense = (0.6 * current_projection) + (0.4 * historical_avg)

    # -------- Breach probability --------
    if budget > 0:
        breach_ratio = projected_expense / budget
        probability = min(100, int(breach_ratio * 100))
    else:
        probability = 0

    # -------- Risk level --------
    if probability > 110:
        risk = "HIGH"
    elif probability > 90:
        risk = "MEDIUM"
    else:
        risk = "LOW"

    # -------- Runway --------
    balance = income - expense
    days_left = int(balance / daily_burn) if daily_burn > 0 else 999
    days_left = max(days_left, 0)

    return jsonify({
        "risk": risk,
        "probability": probability,
        "projected_expense": round(projected_expense),
        "days_left": days_left,
        "balance": balance
    })

# ================= ANOMALY DETECTION (ALL HISTORY) =================
@insights_bp.route("/anomaly-transactions")
@login_required
def anomaly_transactions():
    user_id = session["user_id"]
    conn    = get_db()

    try:
        rows = conn.execute(
            """
            SELECT id, amount
            FROM transactions
            WHERE user_id = ?
              AND type    = 'expense'
            ORDER BY date ASC
            """,
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    amounts   = [r["amount"] for r in rows]
    indices   = detect_anomalies(amounts)
    anomalies = [dict(rows[i]) for i in indices if 0 <= i < len(rows)]

    return jsonify({"anomalies": anomalies})


# ================= BUDGET RISK / PROJECTION (CURRENT MONTH) =================
@insights_bp.route("/budget-risk")
@login_required
def budget_risk():
    user_id     = session["user_id"]
    month_start = get_month_start()
    conn        = get_db()

    try:
        expense = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM transactions
            WHERE user_id = ?
              AND type    = 'expense'
              AND date   >= ?
            """,
            (user_id, month_start),
        ).fetchone()[0] or 0

        budget_row = conn.execute(
            "SELECT amount FROM budgets WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()

    budget    = budget_row["amount"] if budget_row else 0
    day       = datetime.today().day or 1
    projected = round((expense / day) * 30)

    return jsonify({
        "projected_expense": projected,
        "will_exceed":       (projected > budget) if budget > 0 else False,
    })


# ================= TOP CATEGORIES (CURRENT MONTH) =================
@insights_bp.route("/top-categories")
@login_required
def top_categories():
    user_id     = session["user_id"]
    month_start = get_month_start()
    conn        = get_db()

    try:
        rows = conn.execute(
            """
            SELECT
                COALESCE(category, 'Uncategorized') AS category,
                SUM(amount) AS total
            FROM transactions
            WHERE user_id = ?
              AND type    = 'expense'
              AND date   >= ?
            GROUP BY category
            ORDER BY total DESC
            LIMIT 5
            """,
            (user_id, month_start),
        ).fetchall()
    finally:
        conn.close()

    grand_total = sum(r["total"] for r in rows) or 1

    result = [
        {
            "category": r["category"],
            "amount":   r["total"],
            "percent":  round((r["total"] / grand_total) * 100),
        }
        for r in rows
    ]

    return jsonify(result)


# ================= SPENDING INSIGHTS (MONTH-OVER-MONTH COMPARISON) =================
@insights_bp.route("/spending-insights")
@login_required
def spending_insights():
    user_id = session["user_id"]
    conn    = get_db()

    try:
        rows = conn.execute(
            """
            SELECT
                strftime('%Y-%m', date) AS month,
                COALESCE(category, 'Uncategorized') AS category,
                SUM(amount) AS total
            FROM transactions
            WHERE user_id = ?
              AND type    = 'expense'
            GROUP BY month, category
            ORDER BY month DESC
            """,
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return jsonify([])

    months = sorted({r["month"] for r in rows if r["month"]}, reverse=True)
    if len(months) < 2:
        return jsonify([])

    current_month, previous_month = months[0], months[1]
    curr_data, prev_data = {}, {}

    for r in rows:
        if r["month"] == current_month:
            curr_data[r["category"]] = r["total"]
        elif r["month"] == previous_month:
            prev_data[r["category"]] = r["total"]

    insights = []
    for cat, curr_total in curr_data.items():
        if cat not in prev_data:
            continue

        prev_total = prev_data[cat] or 0
        if prev_total == 0:
            continue

        change  = curr_total - prev_total
        percent = (change / prev_total) * 100

        if percent > 20:
            insights.append({
                "type":    "warning",
                "message": f"{cat} spending increased by {percent:.0f}%",
            })
        elif percent < -15:
            insights.append({
                "type":    "positive",
                "message": f"{cat} spending reduced by {abs(percent):.0f}%",
            })

    return jsonify(insights)

@insights_bp.route("/subscriptions")
@login_required
def subscriptions():
    user_id = session["user_id"]
    conn = get_db()

    try:
        rows = conn.execute("""
            SELECT description, category, AVG(amount) as avg_amount
            FROM transactions
            WHERE user_id=? AND type='expense'
            GROUP BY description
            HAVING COUNT(*) >= 3
            ORDER BY avg_amount DESC
        """, (user_id,)).fetchall()
    finally:
        conn.close()

    subs = [
        {"name": r["description"], "amount": round(r["avg_amount"])}
        for r in rows
    ]

    return jsonify({"subscriptions": subs})