from utils.db import get_db

def create_transaction(user_id, description, amount, t_type, category, date):
    conn = get_db()
    cursor = conn.execute("""
        INSERT INTO transactions (user_id, description, amount, type, category, date)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, description, amount, t_type, category, date))
    conn.commit()
    tid = cursor.lastrowid
    conn.close()
    return tid


def fetch_transactions(user_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM transactions WHERE user_id=? ORDER BY date DESC, id DESC",
        (user_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_transaction(user_id, tid):
    conn = get_db()
    conn.execute(
        "DELETE FROM transactions WHERE id=? AND user_id=?",
        (tid, user_id)
    )
    conn.commit()
    conn.close()


def update_transaction(user_id, tid, data):
    conn = get_db()
    conn.execute("""
        UPDATE transactions
        SET description=?, amount=?, category=?, type=?, date=?
        WHERE id=? AND user_id=?
    """, (
        data["description"],
        data["amount"],
        data["category"],
        data["type"],
        data["date"],
        tid,
        user_id
    ))
    conn.commit()
    conn.close()


def clear_all_transactions(user_id):
    conn = get_db()
    conn.execute(
        "DELETE FROM transactions WHERE user_id=?",
        (user_id,)
    )
    conn.commit()
    conn.close()