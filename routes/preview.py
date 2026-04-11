"""
routes/preview.py
Transaction preview, UPI confirm, and ledger verify endpoints.

These are additive — no existing route is modified.
"""

from flask import Blueprint, jsonify, request, session
from utils.db import get_db
from utils.decorators import login_required
from services.ledger_service import append_ledger, verify_chain
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
           FROM transactions WHERE user_id=? AND date>=? AND status!='failed'""",
        (user_id, cur_start)
    ).fetchone()

    budget_row = conn.execute(
        "SELECT COALESCE(amount,0) AS amount FROM budgets WHERE user_id=?",
        (user_id,)
    ).fetchone()

    goals = conn.execute(
        "SELECT name, target_amount, saved_amount FROM goals WHERE user_id=?",
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
          wallet_balance : float,
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

        # Wallet balance
        wallet_row = conn.execute(
            "SELECT balance FROM wallets WHERE user_id=?", (user_id,)
        ).fetchone()
        wallet_balance = float(wallet_row["balance"]) if wallet_row else 0.0

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

        # Wallet sufficiency note
        if wallet_balance < amount:
            wallet_note = (
                f"Wallet balance (₹{wallet_balance:,.0f}) is insufficient — "
                "UPI or top-up required."
            )
            goal_impact = wallet_note if not goal_impact else f"{goal_impact} {wallet_note}"

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
        "wallet_balance":      wallet_balance,
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
    Save the transaction as 'completed' and append to ledger.

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
               VALUES (?, ?, ?, 'expense', ?, ?, 'completed')""",
            (user_id, note, amount, category, date)
        )
        tx_id = cur.lastrowid
        conn.commit()

        append_ledger(conn, user_id, tx_id)
        conn.commit()
    finally:
        conn.close()

    return jsonify({"success": True, "transaction_id": tx_id})


# ─────────────────────────────────────────────────────────────────
# POST /wallet-pay-transaction
# ─────────────────────────────────────────────────────────────────

@preview_bp.route("/wallet-pay-transaction", methods=["POST"])
@login_required
def wallet_pay_transaction():
    """
    Pay for a transaction using the internal wallet.
    Deducts balance and saves as 'completed'.

    Body: { description, amount, category, date? }
    """
    data        = request.get_json(silent=True) or {}
    description = (data.get("description") or "Wallet Payment")[:200]
    amount      = float(data.get("amount", 0))
    category    = (data.get("category") or "").strip()
    date        = data.get("date") or datetime.today().strftime("%Y-%m-%d")

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
        # Check balance
        wallet_row = conn.execute(
            "SELECT balance FROM wallets WHERE user_id=?", (user_id,)
        ).fetchone()
        balance = float(wallet_row["balance"]) if wallet_row else 0.0

        if balance < amount:
            return jsonify({
                "success": False,
                "message": f"Insufficient wallet balance (₹{balance:,.2f} available)"
            }), 400

        # Deduct
        conn.execute(
            "UPDATE wallets SET balance = balance - ? WHERE user_id=?",
            (amount, user_id)
        )

        # Record wallet_transaction
        conn.execute(
            """INSERT INTO wallet_transactions
               (sender_id, receiver_id, amount, note, status, created_at)
               VALUES (?, NULL, ?, ?, 'completed', ?)""",
            (user_id, amount, description, datetime.utcnow().isoformat())
        )

        # Save in transactions
        cur = conn.execute(
            """INSERT INTO transactions
               (user_id, description, amount, type, category, date, status)
               VALUES (?, ?, ?, 'expense', ?, ?, 'completed')""",
            (user_id, f"{description} [Wallet]", amount, category, date)
        )
        tx_id = cur.lastrowid
        conn.commit()

        append_ledger(conn, user_id, tx_id)
        conn.commit()

    finally:
        conn.close()

    return jsonify({"success": True, "transaction_id": tx_id})


# ─────────────────────────────────────────────────────────────────
# GET /ledger/verify
# ─────────────────────────────────────────────────────────────────

@preview_bp.route("/ledger/verify")
@login_required
def ledger_verify():
    """Verify the integrity of the caller's ledger chain."""
    conn = get_db()
    try:
        result = verify_chain(conn, session["user_id"])
    finally:
        conn.close()
    return jsonify(result)


# ─────────────────────────────────────────────────────────────────
# GET /ledger/history
# ─────────────────────────────────────────────────────────────────

@preview_bp.route("/ledger/history")
@login_required
def ledger_history():
    """Return last 30 ledger entries for the caller."""
    user_id = session["user_id"]
    conn    = get_db()
    try:
        rows = conn.execute(
            """SELECT l.id, l.transaction_id, l.hash, l.prev_hash, l.timestamp,
                      t.description, t.amount, t.type
               FROM ledger l
               JOIN transactions t ON t.id = l.transaction_id
               WHERE l.user_id = ?
               ORDER BY l.id DESC LIMIT 30""",
            (user_id,)
        ).fetchall()
    finally:
        conn.close()
    return jsonify({"ledger": [dict(r) for r in rows]})