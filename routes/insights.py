from flask import Blueprint, jsonify, session
from utils.db import get_db
from utils.decorators import login_required
from datetime import datetime, timedelta

# ML imports
from ml.forecast_model import predict_next_month
from ml.anomaly_model import detect_category_anomalies
from ml.risk_model import predict_risk

from ml.recommender import get_recommendations

insights_bp = Blueprint("insights", __name__)


def get_month_start():
    """Return the first day of the current month as a date string (YYYY-MM-01)."""
    return datetime.today().strftime("%Y-%m-01")


def _current_month_key():
    """Return the actual current calendar month as 'YYYY-MM' (matches the
    to_char(date, 'YYYY-MM') format already used in the month-over-month
    query below)."""
    return datetime.today().strftime("%Y-%m")


def _previous_month_key(month_key):
    """Given a 'YYYY-MM' string, return the calendar month immediately
    before it — NOT the previous month that happens to have transactions.
    This is what lets us tell 'no data yet' apart from 'nothing changed'."""
    year, month = (int(part) for part in month_key.split("-"))
    first_of_month = datetime(year, month, 1)
    last_day_of_prev_month = first_of_month - timedelta(days=1)
    return last_day_of_prev_month.strftime("%Y-%m")


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
        WHERE user_id = %s
          AND date   >= %s
        """,
        (user_id, month_start),
    ).fetchone()

    budget_row = conn.execute(
        "SELECT amount FROM budgets WHERE user_id = %s",
        (user_id,),
    ).fetchone()

    # PostgreSQL NUMERIC columns come back as Decimal; convert to float
    # at the boundary so downstream arithmetic/JSON never sees a Decimal.
    income  = float(row["income"]  or 0)
    expense = float(row["expense"] or 0)
    budget  = float(budget_row["amount"]) if budget_row else 0.0

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
                to_char(date, 'YYYY-MM') AS month,
                SUM(amount)             AS total
            FROM transactions
            WHERE user_id = %s
              AND type    = 'expense'
            GROUP BY month
            ORDER BY month ASC
            """,
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    # SUM(amount) comes back as Decimal from PostgreSQL; convert to float
    # before it reaches the ML model.
    expenses   = [float(r["total"] or 0) for r in rows]
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
            WHERE user_id=%s AND date>=%s
        """, (user_id, month_start)).fetchone()

        # Decimal -> float at the boundary
        income = float(row["income"] or 0)
        expense = float(row["expense"] or 0)

        # No current-month income AND no current-month expense means there is
        # simply no evidence to evaluate — not a "LOW risk" verdict. Return an
        # explicit insufficient-data state instead of letting income=0 fall
        # through and reach the divide-by-income guards below with fabricated
        # numbers (0% probability, 999-day runway) that look like real
        # assessments. Note: income > 0 with expense == 0 (Case E) is NOT
        # short-circuited here — that's real evidence and flows through the
        # existing calculation unchanged, per spec.
        if income == 0 and expense == 0:
            return jsonify({
                "status":            "insufficient_data",
                "risk":              "INSUFFICIENT DATA",
                "probability":       None,
                "projected_expense": None,
                "days_left":         None,
                "message":           "No current-month financial activity is available to estimate risk.",
            })

        budget_row = conn.execute(
            "SELECT amount FROM budgets WHERE user_id=%s",
            (user_id,)
        ).fetchone()

        budget = float(budget_row["amount"]) if budget_row else 0.0

        # -------- Stabilized burn rate --------
        today = datetime.today()
        days_passed = max(today.day, 7)   # prevents early-month spikes
        daily_burn = expense / days_passed if days_passed > 0 else 0

        current_projection = daily_burn * 30

        # -------- Historical average (last 6 months) --------
        hist_rows = conn.execute("""
            SELECT to_char(date, 'YYYY-MM') AS month,
                   SUM(amount) AS total
            FROM transactions
            WHERE user_id=%s AND type='expense'
            GROUP BY month
            ORDER BY month DESC
            LIMIT 6
        """, (user_id,)).fetchall()

        # Historical totals are Decimal from PostgreSQL; convert to float.
        hist_values = [float(r["total"]) for r in hist_rows if r["total"]]

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
            SELECT id, amount, category, description
            FROM transactions
            WHERE user_id = %s
              AND type    = 'expense'
            ORDER BY date ASC
            """,
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    # amount is Decimal from PostgreSQL; convert to float before it
    # reaches the anomaly-detection model.
    transactions = [
        {
            "id":          r["id"],
            "amount":      float(r["amount"] or 0),
            "category":    r["category"],
            "description": r["description"],
        }
        for r in rows
    ]

    raw_anomalies = detect_category_anomalies(transactions)

    # "id" is kept alongside "transaction_id" for frontend compatibility.
    anomalies = [
        {
            "id":               a["transaction_id"],
            "transaction_id":   a["transaction_id"],
            "amount":           a["amount"],
            "category":         a["category"],
            "expected_amount":  a["expected_amount"],
            "deviation":        a["deviation"],
            "severity":         a["severity"],
            "confidence":       a["confidence"],
            "reason":           a["reason"],
        }
        for a in raw_anomalies
    ]

    return jsonify({"anomalies": anomalies})


# ================= BUDGET RISK / PROJECTION (CURRENT MONTH) =================
@insights_bp.route("/budget-risk")
@login_required
def budget_risk():
    user_id     = session["user_id"]
    month_start = get_month_start()
    conn        = get_db()

    try:
        expense_row = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total
            FROM transactions
            WHERE user_id = %s
              AND type    = 'expense'
              AND date   >= %s
            """,
            (user_id, month_start),
        ).fetchone()

        budget_row = conn.execute(
            "SELECT amount FROM budgets WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()

    # Decimal -> float before calculations
    expense   = float(expense_row["total"] or 0)
    budget    = float(budget_row["amount"]) if budget_row else 0.0
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
            WHERE user_id = %s
              AND type    = 'expense'
              AND date   >= %s
            GROUP BY category
            ORDER BY total DESC
            LIMIT 5
            """,
            (user_id, month_start),
        ).fetchall()
    finally:
        conn.close()

    # SUM(amount) is Decimal from PostgreSQL; convert to float before
    # using it in percentage math or the JSON response.
    totals = [float(r["total"] or 0) for r in rows]
    grand_total = sum(totals) or 1

    result = [
        {
            "category": r["category"],
            "amount":   total,
            "percent":  round((total / grand_total) * 100),
        }
        for r, total in zip(rows, totals)
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
                to_char(date, 'YYYY-MM') AS month,
                COALESCE(category, 'Uncategorized') AS category,
                SUM(amount) AS total
            FROM transactions
            WHERE user_id = %s
              AND type    = 'expense'
            GROUP BY month, category
            ORDER BY month DESC
            """,
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    # Use the actual current calendar month/previous calendar month, not
    # "the two most recent months that happen to have rows". Otherwise, once
    # the current month has zero transactions, this silently falls back to
    # comparing two historical months and reports a change (e.g. "Food
    # spending reduced by 94%") that has nothing to do with "this month".
    current_month  = _current_month_key()
    previous_month = _previous_month_key(current_month)

    curr_data, prev_data = {}, {}

    # SUM(amount) is Decimal from PostgreSQL; convert to float before
    # storing so subsequent subtraction/division stays in float land.
    for r in rows:
        total = float(r["total"] or 0)
        if r["month"] == current_month:
            curr_data[r["category"]] = total
        elif r["month"] == previous_month:
            prev_data[r["category"]] = total

    if not curr_data:
        return jsonify({
            "status":  "insufficient_data",
            "message": "No current-month spending data yet.",
        })

    if not prev_data:
        return jsonify({
            "status":  "insufficient_data",
            "message": "Not enough previous-month data for a comparison.",
        })

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
            SELECT description, MAX(category) AS category, AVG(amount) as avg_amount
            FROM transactions
            WHERE user_id=%s AND type='expense'
            GROUP BY description
            HAVING COUNT(*) >= 3
            ORDER BY avg_amount DESC
        """, (user_id,)).fetchall()
    finally:
        conn.close()

    # AVG(amount) is Decimal from PostgreSQL; convert to float before
    # round() and JSON serialization (json.dumps can't handle Decimal).
    subs = [
        {"name": r["description"], "amount": round(float(r["avg_amount"] or 0))}
        for r in rows
    ]

    return jsonify({"subscriptions": subs})