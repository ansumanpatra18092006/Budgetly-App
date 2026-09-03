"""Canonical budget repository/service.

All budget writes should come through this module. Routes may use it directly
or the dashboard API; there is intentionally no second SQL implementation.
"""

from utils.db import get_db


def get_budget(user_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT amount FROM budgets WHERE user_id=%s",
            (user_id,),
        ).fetchone()
        return float(row["amount"]) if row else 0.0
    finally:
        conn.close()


def set_budget(user_id, amount):
    amount = float(amount)
    if amount < 0:
        raise ValueError("Budget cannot be negative.")

    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO budgets (user_id, amount) VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE SET amount = EXCLUDED.amount
            """,
            (user_id, amount),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
