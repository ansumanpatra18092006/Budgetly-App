"""Compatibility analytics helpers.

Financial aggregates should be computed by the unified metrics layer. These
helpers remain only for legacy callers and delegate budget operations to the
canonical budget service.
"""

from utils.db import get_db
from services.budget_service import get_budget as _get_budget, set_budget as _set_budget


def get_income_expense(user_id):
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN type='income' THEN amount ELSE 0 END), 0) AS income,
                COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END), 0) AS expense
            FROM transactions
            WHERE user_id=%s AND status <> 'failed'
            """,
            (user_id,),
        ).fetchone()
        return float(row["income"] or 0), float(row["expense"] or 0)
    finally:
        conn.close()


def get_budget(user_id):
    return _get_budget(user_id)


def set_budget(user_id, amount):
    return _set_budget(user_id, amount)
