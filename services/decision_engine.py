"""Compatibility adapter for legacy transaction-preview callers.

The old implementation duplicated the purchase-impact logic, queried a
non-existent ``wallets`` table, used a separate risk formula, and could
disagree with the unified Insights health score. All transaction-impact
decisions now go through ``transaction_impact_service``.
"""

from services.transaction_impact_service import evaluate_transaction_impact


def evaluate_transaction(user_id: int, payload: dict) -> dict:
    return evaluate_transaction_impact(
        user_id,
        amount=payload.get("amount", 0),
        tx_type=payload.get("type", "expense"),
        category=payload.get("category", "Misc"),
    )
