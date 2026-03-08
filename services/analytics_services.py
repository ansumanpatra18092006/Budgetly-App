from utils.db import get_db

def get_income_expense(user_id):
    conn = get_db()

    income = conn.execute(
        "SELECT SUM(amount) FROM transactions WHERE user_id=? AND type='income'",
        (user_id,)
    ).fetchone()[0] or 0

    expense = conn.execute(
        "SELECT SUM(amount) FROM transactions WHERE user_id=? AND type='expense'",
        (user_id,)
    ).fetchone()[0] or 0

    conn.close()
    return income, expense


def get_budget(user_id):
    conn = get_db()
    row = conn.execute(
        "SELECT amount FROM budgets WHERE user_id=?",
        (user_id,)
    ).fetchone()
    conn.close()
    return row["amount"] if row else 0


def set_budget(user_id, amount):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO budgets (user_id, amount) VALUES (?, ?)",
        (user_id, amount)
    )
    conn.commit()
    conn.close()