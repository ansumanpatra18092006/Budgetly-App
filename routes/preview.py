"""
routes/preview.py
Transaction preview and UPI confirm endpoints.

Budgetly does not hold user money. Payment is always external
(the user's own UPI app); Budgetly only records the outcome and
its financial impact.
"""

from flask import Blueprint, jsonify, request, session
from utils.db import get_db
from utils.decorators import login_required
from datetime import datetime, timedelta

preview_bp = Blueprint("preview", __name__)


# ─────────────────────────────────────────────────────────────────
# Shared metric helper (mirrors ai_insights._fetch_full_metrics)
# ─────────────────────────────────────────────────────────────────

def _current_metrics(conn, user_id: int) -> dict:
    today     = datetime.today()
    cur_start = today.strftime("%Y-%m-01")

    cur = conn.execute(
        """SELECT
               COALESCE(SUM(CASE WHEN type='income'  THEN amount ELSE 0 END),0) AS income,
               COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END),0) AS expense
           FROM transactions WHERE user_id=%s AND date>=%s AND status!='failed'""",
        (user_id, cur_start)
    ).fetchone()

    budget_row = conn.execute(
        "SELECT COALESCE(amount,0) AS amount FROM budgets WHERE user_id=%s",
        (user_id,)
    ).fetchone()

    goals = conn.execute(
        "SELECT name, target_amount, saved_amount FROM goals WHERE user_id=%s",
        (user_id,)
    ).fetchall()

    income  = float(cur["income"])
    expense = float(cur["expense"])
    budget  = float(budget_row["amount"]) if budget_row else 0.0
    surplus = income - expense

    return dict(
        income=income,
        expense=expense,
        surplus=surplus,
        budget=budget,
        goals=[dict(g) for g in goals],
    )


# ─────────────────────────────────────────────────────────────────
# POST /preview-transaction
# ─────────────────────────────────────────────────────────────────

@preview_bp.route("/preview-transaction", methods=["POST"])
@login_required
def preview_transaction():
    """
    Simulate the financial impact of a pending expense BEFORE saving it.

    Body:
        { description, amount, category, type }

    Returns:
        {
          warning        : str | null,
          level          : "low" | "medium" | "high",
          new_surplus    : float,
          budget_after   : float,        # % of budget used after this tx
          savings_rate_after : float,    # % savings rate after
          goal_impact    : str | null,   # human-readable goal note
        }
    """
    data    = request.get_json(silent=True) or {}
    amount  = float(data.get("amount", 0))
    tx_type = data.get("type", "expense")

    if amount <= 0:
        return jsonify({"success": False, "message": "Amount must be > 0"}), 400

    user_id = session["user_id"]
    conn    = get_db()

    try:
        m = _current_metrics(conn, user_id)
    finally:
        conn.close()

    # ── Simulate ─────────────────────────────────────────────────
    if tx_type == "expense":
        new_expense     = m["expense"] + amount
        new_surplus     = m["income"] - new_expense
        budget_after    = round(new_expense / m["budget"] * 100, 1) if m["budget"] > 0 else 0.0
        savings_rate_after = round(new_surplus / m["income"] * 100, 1) if m["income"] > 0 else 0.0
    else:
        # income transaction — no risk
        new_surplus        = m["surplus"] + amount
        budget_after       = round(m["expense"] / m["budget"] * 100, 1) if m["budget"] > 0 else 0.0
        savings_rate_after = round(new_surplus / (m["income"] + amount) * 100, 1) if m["income"] > 0 else 0.0

    # ── Risk scoring ──────────────────────────────────────────────
    warning     = None
    level       = "low"
    goal_impact = None

    if tx_type == "expense":
        # Budget breach
        if m["budget"] > 0:
            if budget_after > 100:
                warning = (
                    f"This transaction will exceed your monthly budget by "
                    f"₹{abs(m['budget'] - m['expense'] - amount):,.0f}."
                )
                level = "high"
            elif budget_after > 85:
                warning = (
                    f"You'll have used {budget_after}% of your budget after this. "
                    f"Only ₹{m['budget'] - m['expense'] - amount:,.0f} left."
                )
                level = "medium"
            elif budget_after > 70:
                warning = f"Budget usage will reach {budget_after}% after this transaction."
                level   = "low"

        # Savings rate collapse
        if savings_rate_after < 5 and m["income"] > 0:
            msg = (
                f"Your savings rate will drop to {savings_rate_after}% — "
                "well below the recommended 20%."
            )
            if level == "low":
                warning = msg
            elif level == "medium":
                warning += f" Also, {msg}"
            level = max(level, "high") if level != "high" else "high"

        elif savings_rate_after < 15 and m["income"] > 0:
            msg = f"Savings rate will fall to {savings_rate_after}%."
            if not warning:
                warning = msg
                level   = max(level, "medium") if level != "high" else "high"

        # Goal impact
        if m["goals"]:
            total_left = sum(
                max(0, float(g["target_amount"]) - float(g["saved_amount"]))
                for g in m["goals"]
            )
            if total_left > 0 and new_surplus < 0:
                goal_impact = (
                    f"This transaction leaves you with a deficit of "
                    f"₹{abs(new_surplus):,.0f}, which may delay your savings goals."
                )
            elif total_left > 0 and new_surplus < amount * 2:
                goal_impact = (
                    f"After this expense your remaining surplus (₹{new_surplus:,.0f}) "
                    f"covers only {round(new_surplus/total_left*100)}% of your total goal gap."
                )

    # Level guard
    level_map = {"low": 0, "medium": 1, "high": 2}
    if level not in level_map:
        level = "low"

    return jsonify({
        "warning":             warning,
        "level":               level,
        "new_surplus":         round(new_surplus, 2),
        "budget_after":        budget_after,
        "savings_rate_after":  savings_rate_after,
        "goal_impact":         goal_impact,
        "current_expense":     round(m["expense"], 2),
        "current_surplus":     round(m["surplus"], 2),
        "budget":              round(m["budget"], 2),
    })


# ─────────────────────────────────────────────────────────────────
# POST /confirm-upi-transaction
# ─────────────────────────────────────────────────────────────────

@preview_bp.route("/confirm-upi-transaction", methods=["POST"])
@login_required
def confirm_upi_transaction():
    """
    User confirmed payment was completed in their UPI app.
    Save the transaction as 'completed'.

    Body: { description, amount, category, date?, upi_ref? }
    """
    data        = request.get_json(silent=True) or {}
    description = (data.get("description") or "UPI Payment")[:200]
    amount      = float(data.get("amount", 0))
    category    = (data.get("category") or "").strip()
    date        = data.get("date") or datetime.today().strftime("%Y-%m-%d")
    upi_ref     = (data.get("upi_ref") or "")[:100]

    # Auto-detect category if blank or "auto-detect"
    if not category or category.lower() == "auto-detect":
        from routes.transactions import get_smart_category
        try:
            category = get_smart_category(session["user_id"], description)
        except Exception:
            category = "Misc"

    if amount <= 0:
        return jsonify({"success": False, "message": "Invalid amount"}), 400

    user_id = session["user_id"]
    conn    = get_db()

    try:
        note = description
        if upi_ref:
            note = f"{description} [UPI:{upi_ref}]"

        cur = conn.execute(
            """INSERT INTO transactions
               (user_id, description, amount, type, category, date, status)
               VALUES (%s, %s, %s, 'expense', %s, %s, 'completed')
               RETURNING id""",
            (user_id, note, amount, category, date)
        )
        tx_id = cur.fetchone()["id"]
        conn.commit()
    finally:
        conn.close()

    return jsonify({"success": True, "transaction_id": tx_id})