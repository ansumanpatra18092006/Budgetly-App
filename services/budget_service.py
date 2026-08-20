from utils.db import get_db

def set_budget(user_id, amount):
    conn = get_db()
    # NOTE: SQLite's "INSERT OR REPLACE" -> Postgres upsert via
    # "INSERT ... ON CONFLICT (user_id) DO UPDATE". Requires a
    # UNIQUE/PRIMARY KEY constraint on budgets.user_id.
    conn.execute("""
        INSERT INTO budgets (user_id, amount) VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET amount = EXCLUDED.amount
    """, (user_id, amount))
    conn.commit()
    conn.close()