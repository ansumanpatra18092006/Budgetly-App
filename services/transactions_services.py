from utils.db import get_db

def create_transaction(user_id, description, amount, t_type, category, date):
    conn = get_db()
    cursor = conn.execute("""
        INSERT INTO transactions (user_id, description, amount, type, category, date)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (user_id, description, amount, t_type, category, date))
    tid = cursor.fetchone()["id"]
    conn.commit()
    conn.close()
    return tid


def fetch_transactions(user_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM transactions WHERE user_id=%s ORDER BY date DESC, id DESC",
        (user_id,)
    ).fetchall()
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
    conn.execute("""
        UPDATE transactions
        SET description=%s, amount=%s, category=%s, type=%s, date=%s
        WHERE id=%s AND user_id=%s
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
        "DELETE FROM transactions WHERE user_id=%s",
        (user_id,)
    )
    conn.commit()
    conn.close()