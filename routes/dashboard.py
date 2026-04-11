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
            WHERE user_id = ?
              AND date >= ?
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
            "SELECT amount FROM budgets WHERE user_id = ?",
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
            WHERE user_id = ?
              AND type    = 'expense'
              AND date   >= ?
            GROUP BY category
            HAVING total > 0
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
                strftime('%Y-%m', date) AS month,
                COALESCE(SUM(CASE WHEN type = 'income'  THEN amount ELSE 0 END), 0) AS income,
                COALESCE(SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END), 0) AS expense
            FROM transactions
            WHERE user_id = ?
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


# ================= BALANCE TREND (LAST 2 MONTHS) =================
@dashboard_bp.route("/balance-trend")
@login_required
def balance_trend():
    user_id = session["user_id"]
    conn    = get_db()

    try:
        rows = conn.execute(
            """
            SELECT
                strftime('%Y-%m', date) AS month,
                COALESCE(SUM(CASE WHEN type = 'income'  THEN amount ELSE 0 END), 0) -
                COALESCE(SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END), 0) AS balance
            FROM transactions
            WHERE user_id = ?
            GROUP BY month
            ORDER BY month DESC
            LIMIT 2
            """,
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    if len(rows) < 2:
        return jsonify({"change": 0})

    current  = rows[0]["balance"] or 0
    previous = rows[1]["balance"] or 0

    if previous == 0:
        return jsonify({"change": 0})

    percent = ((current - previous) / abs(previous)) * 100
    return jsonify({"change": round(percent)})


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