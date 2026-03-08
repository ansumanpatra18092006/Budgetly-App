from utils.db import get_db

def set_budget(user_id, amount):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO budgets (user_id, amount) VALUES (?, ?)",
        (user_id, amount)
    )
    conn.commit()
    conn.close()