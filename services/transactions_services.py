from datetime import datetime
from utils.db import get_db


def _parse_timestamp(raw):
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return value.replace(tzinfo=None) if value.tzinfo is not None else value
    except ValueError:
        return None


def create_transaction(user_id, description, amount, t_type, category, date, transaction_timestamp=None, reference_id=None, utr=None, source="FinTrust"):
    conn = get_db()
    try:
        ts = _parse_timestamp(transaction_timestamp)
        cur = conn.execute(
            """INSERT INTO transactions
               (user_id, description, amount, type, category, date,
                transaction_timestamp, reference_id, utr, source)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (user_id, description, amount, t_type, category, date, ts, reference_id, utr, source)
        )
        tid = cur.fetchone()["id"]
        conn.commit()
        return tid
    finally:
        conn.close()


def fetch_transactions(user_id):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE user_id=%s "
            "ORDER BY transaction_timestamp DESC NULLS LAST, date DESC, id DESC",
            (user_id,)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def delete_transaction(user_id, tid):
    conn = get_db()
    conn.execute(
        "DELETE FROM transactions WHERE id=%s AND user_id=%s",
        (tid, user_id)
    )
    conn.commit()
    conn.close()


def update_transaction(user_id, tid, data):
    conn = get_db()
    conn.execute(
        """UPDATE transactions
           SET description=%s, amount=%s, category=%s, type=%s, date=%s,
               transaction_timestamp=%s, reference_id=%s, utr=%s, source=%s
           WHERE id=%s AND user_id=%s""",
        (
            data["description"],
            data["amount"],
            data["category"],
            data["type"],
            data["date"],
            _parse_timestamp(data.get("transaction_timestamp") or data.get("timestamp")),
            data.get("reference_id"),
            data.get("utr"),
            data.get("source") or "FinTrust",
            tid,
            user_id
        )
    )
    conn.commit()
    conn.close()


def clear_all_transactions(user_id):
    conn = get_db()
    conn.execute(
        "DELETE FROM transactions WHERE user_id=%s",
        (user_id,)
    )
    conn.commit()
    conn.close()
