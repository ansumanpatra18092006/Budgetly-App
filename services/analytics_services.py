from utils.db import get_db

def get_income_expense(user_id):
    conn = get_db()

    income = conn.execute(
        "SELECT SUM(amount) AS total FROM transactions WHERE user_id=%s AND type='income'",
        (user_id,)
    ).fetchone()["total"] or 0

    expense = conn.execute(
        "SELECT SUM(amount) AS total FROM transactions WHERE user_id=%s AND type='expense'",
        (user_id,)
    ).fetchone()["total"] or 0

    conn.close()
    return income, expense


def get_budget(user_id):
    conn = get_db()
    row = conn.execute(
        "SELECT amount FROM budgets WHERE user_id=%s",
        (user_id,)
    ).fetchone()
    conn.close()
    return row["amount"] if row else 0


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