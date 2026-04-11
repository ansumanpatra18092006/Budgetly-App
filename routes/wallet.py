"""
routes/wallet.py
Wallet system: balance queries, peer sends, and top-ups.
Every completed wallet transaction is recorded in the ledger.
"""

from flask import Blueprint, jsonify, request, session
from utils.db import get_db
from utils.decorators import login_required
from services.ledger_service import append_ledger
from datetime import datetime

wallet_bp = Blueprint("wallet", __name__, url_prefix="/wallet")


# ── helpers ──────────────────────────────────────────────────────

def _get_or_create_wallet(conn, user_id: int) -> float:
    """Return current balance, creating a zero-balance wallet if needed."""
    row = conn.execute(
        "SELECT balance FROM wallets WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO wallets (user_id, balance) VALUES (?, 0)", (user_id,)
        )
        conn.commit()
        return 0.0
    return float(row["balance"])


# ── GET /wallet/balance ───────────────────────────────────────────

@wallet_bp.route("/balance")
@login_required
def get_balance():
    conn = get_db()
    try:
        balance = _get_or_create_wallet(conn, session["user_id"])
    finally:
        conn.close()
    return jsonify({"balance": balance})


# ── POST /wallet/topup ────────────────────────────────────────────

@wallet_bp.route("/topup", methods=["POST"])
@login_required
def topup():
    """Add funds to the caller's wallet (simulated – no real payment gateway)."""
    data   = request.get_json(silent=True) or {}
    amount = float(data.get("amount", 0))

    if amount <= 0:
        return jsonify({"success": False, "message": "Amount must be positive"}), 400
    if amount > 100_000:
        return jsonify({"success": False, "message": "Single top-up limit is ₹1,00,000"}), 400

    user_id = session["user_id"]
    conn    = get_db()
    try:
        _get_or_create_wallet(conn, user_id)
        conn.execute(
            "UPDATE wallets SET balance = balance + ? WHERE user_id = ?",
            (amount, user_id)
        )

        # Record in wallet_transactions
        cur = conn.execute(
            """INSERT INTO wallet_transactions
               (sender_id, receiver_id, amount, note, status, created_at)
               VALUES (NULL, ?, ?, 'Top-up', 'completed', ?)""",
            (user_id, amount, datetime.utcnow().isoformat())
        )
        wtx_id = cur.lastrowid

        # Mirror as an income transaction so it appears in history
        tx_cur = conn.execute(
            """INSERT INTO transactions
               (user_id, description, amount, type, category, date, status)
               VALUES (?, 'Wallet Top-up', ?, 'income', 'Finance', ?, 'completed')""",
            (user_id, amount, datetime.utcnow().strftime("%Y-%m-%d"))
        )
        conn.commit()

        append_ledger(conn, user_id, tx_cur.lastrowid)
        conn.commit()
    finally:
        conn.close()

    return jsonify({"success": True, "message": f"₹{amount:,.0f} added to wallet"})


# ── POST /wallet/send ─────────────────────────────────────────────

@wallet_bp.route("/send", methods=["POST"])
@login_required
def send():
    """
    Transfer from caller's wallet to another user's wallet.
    Body: { receiver_email, amount, note }
    """
    data     = request.get_json(silent=True) or {}
    receiver_email = (data.get("receiver_email") or "").strip().lower()
    amount   = float(data.get("amount", 0))
    note     = (data.get("note") or "Wallet Transfer")[:120]

    if amount <= 0:
        return jsonify({"success": False, "message": "Amount must be positive"}), 400

    sender_id = session["user_id"]
    conn      = get_db()

    try:
        # ── resolve receiver ──────────────────────────────────────
        receiver = conn.execute(
            "SELECT id FROM users WHERE LOWER(email) = ?", (receiver_email,)
        ).fetchone()
        if not receiver:
            return jsonify({"success": False, "message": "Receiver not found"}), 404

        receiver_id = receiver["id"]
        if receiver_id == sender_id:
            return jsonify({"success": False, "message": "Cannot send to yourself"}), 400

        # ── check sender balance ──────────────────────────────────
        sender_balance = _get_or_create_wallet(conn, sender_id)
        if sender_balance < amount:
            return jsonify({
                "success": False,
                "message": f"Insufficient wallet balance (₹{sender_balance:,.2f} available)"
            }), 400

        _get_or_create_wallet(conn, receiver_id)

        # ── atomic debit / credit ─────────────────────────────────
        conn.execute(
            "UPDATE wallets SET balance = balance - ? WHERE user_id = ?",
            (amount, sender_id)
        )
        conn.execute(
            "UPDATE wallets SET balance = balance + ? WHERE user_id = ?",
            (amount, receiver_id)
        )

        now = datetime.utcnow()
        cur = conn.execute(
            """INSERT INTO wallet_transactions
               (sender_id, receiver_id, amount, note, status, created_at)
               VALUES (?, ?, ?, ?, 'completed', ?)""",
            (sender_id, receiver_id, amount, note, now.isoformat())
        )
        wtx_id = cur.lastrowid

        # Debit transaction for sender
        tx_cur = conn.execute(
            """INSERT INTO transactions
               (user_id, description, amount, type, category, date, status)
               VALUES (?, ?, ?, 'expense', 'Finance', ?, 'completed')""",
            (sender_id, note, amount, now.strftime("%Y-%m-%d"))
        )
        conn.commit()
        append_ledger(conn, sender_id, tx_cur.lastrowid)

        # Credit transaction for receiver
        rx_cur = conn.execute(
            """INSERT INTO transactions
               (user_id, description, amount, type, category, date, status)
               VALUES (?, ?, ?, 'income', 'Finance', ?, 'completed')""",
            (receiver_id, f"Received: {note}", amount, now.strftime("%Y-%m-%d"))
        )
        conn.commit()
        append_ledger(conn, receiver_id, rx_cur.lastrowid)
        conn.commit()

    finally:
        conn.close()

    return jsonify({
        "success": True,
        "message": f"₹{amount:,.0f} sent successfully"
    })


# ── GET /wallet/history ───────────────────────────────────────────

@wallet_bp.route("/history")
@login_required
def history():
    user_id = session["user_id"]
    conn    = get_db()
    try:
        rows = conn.execute(
            """SELECT wt.id, wt.sender_id, wt.receiver_id, wt.amount,
                      wt.note, wt.status, wt.created_at,
                      su.name AS sender_name, ru.name AS receiver_name
               FROM wallet_transactions wt
               LEFT JOIN users su ON su.id = wt.sender_id
               LEFT JOIN users ru ON ru.id = wt.receiver_id
               WHERE wt.sender_id = ? OR wt.receiver_id = ?
               ORDER BY wt.created_at DESC
               LIMIT 50""",
            (user_id, user_id)
        ).fetchall()
    finally:
        conn.close()

    return jsonify({"history": [dict(r) for r in rows]})