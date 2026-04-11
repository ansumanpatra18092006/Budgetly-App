"""
services/ledger_service.py
Blockchain-style append-only ledger.

Each entry stores:
  - SHA-256 hash of (user_id, transaction_id, amount, timestamp, prev_hash)
  - prev_hash links to the previous entry for the same user

This gives an auditable, tamper-evident chain for every completed transaction.
The chain is per-user (not global) so it scales with user count.
"""

import hashlib
import json
from datetime import datetime


# ─────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────

def append_ledger(conn, user_id: int, transaction_id: int) -> dict | None:
    """
    Fetch the transaction row, compute the hash chain, and insert a
    ledger entry.  Returns the new ledger row dict, or None on failure.

    The caller is responsible for conn.commit() after this call so that
    ledger + transaction writes land in the same SQLite transaction.
    """
    try:
        tx = conn.execute(
            "SELECT id, amount, type, description, date FROM transactions WHERE id = ?",
            (transaction_id,)
        ).fetchone()
        if tx is None:
            return None

        prev_hash = _get_prev_hash(conn, user_id)
        timestamp = datetime.utcnow().isoformat()

        payload = {
            "user_id":        user_id,
            "transaction_id": transaction_id,
            "amount":         tx["amount"],
            "type":           tx["type"],
            "description":    tx["description"],
            "date":           tx["date"],
            "timestamp":      timestamp,
            "prev_hash":      prev_hash,
        }

        new_hash = _sha256(payload)

        conn.execute(
            """INSERT INTO ledger
               (user_id, transaction_id, hash, prev_hash, timestamp)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, transaction_id, new_hash, prev_hash, timestamp)
        )

        return {
            "hash":      new_hash,
            "prev_hash": prev_hash,
            "timestamp": timestamp,
        }

    except Exception as exc:
        # Ledger is non-critical — log but never crash the payment flow
        print(f"[ledger] append failed for tx={transaction_id}: {exc}")
        return None


def verify_chain(conn, user_id: int) -> dict:
    """
    Walk the ledger chain for a user and verify hash integrity.
    Returns { valid: bool, broken_at: int|None, total: int }
    """
    rows = conn.execute(
        """SELECT id, transaction_id, hash, prev_hash, timestamp
           FROM ledger WHERE user_id = ?
           ORDER BY id ASC""",
        (user_id,)
    ).fetchall()

    if not rows:
        return {"valid": True, "broken_at": None, "total": 0}

    prev_hash = "0" * 64   # genesis sentinel

    for row in rows:
        tx = conn.execute(
            "SELECT id, amount, type, description, date FROM transactions WHERE id = ?",
            (row["transaction_id"],)
        ).fetchone()
        if tx is None:
            return {"valid": False, "broken_at": row["id"], "total": len(rows)}

        payload = {
            "user_id":        user_id,
            "transaction_id": row["transaction_id"],
            "amount":         tx["amount"],
            "type":           tx["type"],
            "description":    tx["description"],
            "date":           tx["date"],
            "timestamp":      row["timestamp"],
            "prev_hash":      row["prev_hash"],
        }

        expected = _sha256(payload)

        if expected != row["hash"]:
            return {"valid": False, "broken_at": row["id"], "total": len(rows)}

        if row["prev_hash"] != prev_hash:
            return {"valid": False, "broken_at": row["id"], "total": len(rows)}

        prev_hash = row["hash"]

    return {"valid": True, "broken_at": None, "total": len(rows)}


# ─────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────

def _get_prev_hash(conn, user_id: int) -> str:
    """Return the most recent hash for this user, or the genesis sentinel."""
    row = conn.execute(
        "SELECT hash FROM ledger WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (user_id,)
    ).fetchone()
    return row["hash"] if row else ("0" * 64)


def _sha256(payload: dict) -> str:
    """Deterministic JSON → SHA-256 hex digest."""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()