from flask import Blueprint, jsonify, session, request
from utils.db import get_db
from utils.decorators import login_required
from services.budget_service import set_budget
from datetime import datetime
from services.recurring_service import get_recurring_suggestions

dashboard_bp = Blueprint("dashboard", __name__)


def get_month_start():
    """Return the first day of the current month as a date string (YYYY-MM-01)."""
    return datetime.today().strftime("%Y-%m-01")


# ================= DASHBOARD SUMMARY (CURRENT MONTH) =================
@dashboard_bp.route("/dashboard-summary")
@login_required
def dashboard_summary():
    user_id = session["user_id"]
    month_start = get_month_start()
    conn = get_db()

    try:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN type = 'income'  THEN amount ELSE 0 END), 0) AS income,
                COALESCE(SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END), 0) AS expense
            FROM transactions
            WHERE user_id = %s
              AND date >= %s
              AND status <> 'failed'
            """,
            (user_id, month_start),
        ).fetchone()

        income  = row["income"]  or 0
        expense = row["expense"] or 0
    finally:
        conn.close()

    return jsonify({
        "income":  income,
        "expense": expense,
        "balance": income - expense,
    })


# ================= GET BUDGET =================
@dashboard_bp.route("/get-budget")
@login_required
def get_budget():
    user_id = session["user_id"]
    conn = get_db()

    try:
        row = conn.execute(
            "SELECT amount FROM budgets WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()

    return jsonify({"budget": row["amount"] if row else 0})


# ================= SET BUDGET =================
@dashboard_bp.route("/set-budget", methods=["POST"])
@login_required
def set_budget_route():
    user_id = session["user_id"]
    data    = request.get_json(silent=True) or {}
    amount  = data.get("amount", 0)

    set_budget(user_id, amount)
    return jsonify({"success": True})


# ================= CATEGORY DATA (CURRENT MONTH) =================
@dashboard_bp.route("/category-data")
@login_required
def category_data():
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
              AND status <> 'failed'
            GROUP BY category
            HAVING SUM(amount) > 0
            ORDER BY total DESC
            """,
            (user_id, month_start),
        ).fetchall()
    finally:
        conn.close()

    return jsonify({
        "labels": [r["category"]      for r in rows],
        "data":   [float(r["total"])   for r in rows],
    })


# ================= MONTHLY TREND (ALL HISTORY) =================
@dashboard_bp.route("/monthly-trend")
@login_required
def monthly_trend():
    user_id = session["user_id"]
    conn    = get_db()

    try:
        rows = conn.execute(
            """
            SELECT
                to_char(date, 'YYYY-MM') AS month,
                COALESCE(SUM(CASE WHEN type = 'income'  THEN amount ELSE 0 END), 0) AS income,
                COALESCE(SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END), 0) AS expense
            FROM transactions
            WHERE user_id = %s
              AND status <> 'failed'
            GROUP BY month
            ORDER BY month ASC
            """,
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    return jsonify({
        "months":  [r["month"]   for r in rows],
        "income":  [r["income"]  for r in rows],
        "expense": [r["expense"] for r in rows],
    })


# ================= BALANCE / INCOME / EXPENSE TREND (LAST 2 MONTHS) =================
def _pct_change(current, previous):
    """Percent change from previous -> current, or None if not meaningful."""
    if previous == 0:
        return None
    return round(((current - previous) / abs(previous)) * 100)


@dashboard_bp.route("/balance-trend")
@login_required
def balance_trend():
    user_id = session["user_id"]
    conn    = get_db()

    try:
        rows = conn.execute(
            """
            SELECT
                to_char(date, 'YYYY-MM') AS month,
                COALESCE(SUM(CASE WHEN type = 'income'  THEN amount ELSE 0 END), 0) AS income,
                COALESCE(SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END), 0) AS expense
            FROM transactions
            WHERE user_id = %s
              AND status <> 'failed'
            GROUP BY month
            ORDER BY month DESC
            LIMIT 2
            """,
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    current_month_str = datetime.today().strftime("%Y-%m")
    by_month = {r["month"]: r for r in rows}

    current_row = by_month.get(current_month_str)
    current_income  = float(current_row["income"])  if current_row else 0.0
    current_expense = float(current_row["expense"]) if current_row else 0.0
    current_balance = current_income - current_expense
    has_current_data = current_income > 0 or current_expense > 0

    # Previous = the most recent row that ISN'T the current month
    previous_row = next((r for r in rows if r["month"] != current_month_str), None)
    previous_income  = float(previous_row["income"])  if previous_row else 0.0
    previous_expense = float(previous_row["expense"]) if previous_row else 0.0
    previous_balance = previous_income - previous_expense
    has_previous_data = previous_row is not None and (previous_income > 0 or previous_expense > 0)

    # ---- Balance trend ----
    if has_current_data and has_previous_data:
        balance_change = _pct_change(current_balance, previous_balance)
        balance_status = "ok" if balance_change is not None else "insufficient_data"
    else:
        balance_change = None
        balance_status = "insufficient_data"

    # ---- Income trend ----
    if current_income > 0 and previous_income > 0:
        income_change = _pct_change(current_income, previous_income)
        income_status = "ok" if income_change is not None else "insufficient_data"
    else:
        income_change = None
        income_status = "insufficient_data"

    # ---- Expense trend ----
    if current_expense > 0 and previous_expense > 0:
        expense_change = _pct_change(current_expense, previous_expense)
        expense_status = "ok" if expense_change is not None else "insufficient_data"
    else:
        expense_change = None
        expense_status = "insufficient_data"

    return jsonify({
        "balance": {
            "status": balance_status,
            "change": balance_change,
            "current_balance": current_balance,
            "previous_balance": previous_balance,
            "has_current_data": has_current_data,
        },
        "income": {
            "status": income_status,
            "change": income_change,
            "current_income": current_income,
            "previous_income": previous_income,
        },
        "expense": {
            "status": expense_status,
            "change": expense_change,
            "current_expense": current_expense,
            "previous_expense": previous_expense,
        },
    })


# ================= TOP CATEGORIES (CURRENT MONTH) =================
@dashboard_bp.route("/top-categories")
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
              AND status <> 'failed'
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
            "amount":   float(r["total"]),
            "percent":  round((r["total"] / grand_total) * 100),
        }
        for r in rows
    ]

    return jsonify(result)

@dashboard_bp.route("/recurring-suggestions")
@login_required
def recurring_suggestions():
    user_id = session["user_id"]
    data = get_recurring_suggestions(user_id)
    return jsonify(data)