"""
routes/preview.py
Transaction preview and UPI confirm endpoints.

FinTrust does not hold user money. Payment is always external
(the user's own UPI app); FinTrust only records the outcome and
its financial impact.
"""

from flask import Blueprint, current_app, jsonify, request, session
from utils.db import get_db
from utils.decorators import login_required
from datetime import datetime, timedelta

preview_bp = Blueprint("preview", __name__)


# ─────────────────────────────────────────────────────────────────
# POST /preview-transaction
# ─────────────────────────────────────────────────────────────────

@preview_bp.route("/preview-transaction", methods=["POST"])
@login_required
def preview_transaction():
    """Evaluate a proposed transaction without writing it to the database."""
    data = request.get_json(silent=True) or {}

    try:
        amount = float(data.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Invalid amount"}), 400

    if amount <= 0:
        return jsonify({"success": False, "message": "Amount must be > 0"}), 400

    tx_type = str(data.get("type", "expense") or "expense").lower()
    category = str(data.get("category", "Misc") or "Misc")

    try:
        from services.transaction_impact_service import evaluate_transaction_impact

        result = evaluate_transaction_impact(
            session["user_id"],
            amount=amount,
            tx_type=tx_type,
            category=category,
        )
    except (TypeError, ValueError) as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    except Exception:
        # Do not leak database/model internals to the mobile client.
        current_app.logger.exception("transaction preview failed")
        return jsonify({
            "success": False,
            "message": "Transaction impact is temporarily unavailable.",
        }), 503

    return jsonify(result)


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
    transaction_timestamp = None
    raw_timestamp = data.get("transaction_timestamp") or data.get("timestamp")
    if raw_timestamp:
        try:
            transaction_timestamp = datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
            if transaction_timestamp.tzinfo is not None:
                transaction_timestamp = transaction_timestamp.replace(tzinfo=None)
        except ValueError:
            transaction_timestamp = None
    if transaction_timestamp is None and date == datetime.today().strftime("%Y-%m-%d"):
        transaction_timestamp = datetime.now().replace(microsecond=0)

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
            existing = conn.execute(
                "SELECT id FROM transactions WHERE user_id=%s AND reference_id=%s LIMIT 1",
                (user_id, upi_ref)
            ).fetchone()
            if existing:
                conn.commit()
                return jsonify({"success": True, "transaction_id": existing["id"], "duplicate": True})

        cur = conn.execute(
            """INSERT INTO transactions
               (user_id, description, amount, type, category, date, status,
                transaction_timestamp, reference_id, source)
               VALUES (%s, %s, %s, 'expense', %s, %s, 'completed', %s, %s, %s)
               RETURNING id""",
            (user_id, note, amount, category, date, transaction_timestamp,
             upi_ref or None, "UPI")
        )
        tx_id = cur.fetchone()["id"]
        conn.commit()
    finally:
        conn.close()

    return jsonify({"success": True, "transaction_id": tx_id})