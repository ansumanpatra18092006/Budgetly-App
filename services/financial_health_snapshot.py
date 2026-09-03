"""Shared orchestration for the canonical financial-health scoring engine.

The pure scoring formula lives in ``ml.risk_model.calculate_financial_health``.
This module only normalizes the inputs used by the unified Insights endpoint
and the transaction-impact preview so both paths execute the same calculation.
"""

from ml.risk_model import calculate_financial_health


def compute_health_for_context(
    metrics: dict,
    forecast: dict,
    *,
    monthly_burden: float,
    confirmed_monthly_cost: float,
    recurring_bill_monthly_burden: float,
    remaining_recurring_this_month: float,
    goal_pressure_override=None,
    income_override=None,
    expense_override=None,
    spending_trend_pct_override=None,
    days_in_month: int = 30,
):
    """Run the canonical health model for a current/simulated context.

    ``metrics`` is the canonical dictionary produced by
    ``routes.ai_insights._fetch_full_metrics``. Overrides are used only for
    prospective transaction simulation; the unified dashboard passes them
    through as ``None``.
    """
    income = float(metrics["income"] if income_override is None else income_override)
    expense = float(metrics["expense"] if expense_override is None else expense_override)

    goal_details = metrics.get("goal_details") or []
    goals_at_risk_count = sum(
        1 for g in goal_details if g.get("goal_risk") in ("medium", "high")
    )
    has_goals = bool(goal_details)

    savings_rate_arg = (
        metrics.get("savings_rate")
        if income > 0
        else None
    )

    if goal_pressure_override is None:
        goal_pressure_arg = metrics.get("goal_pressure") if has_goals else None
    else:
        goal_pressure_arg = goal_pressure_override if has_goals else None

    total_required_monthly_arg = (
        metrics.get("total_monthly_required") if has_goals else None
    )
    available_surplus_arg = (
        metrics.get("avg_monthly_surplus") if has_goals else None
    )

    spending_trend = (
        metrics.get("expense_change")
        if spending_trend_pct_override is None
        else spending_trend_pct_override
    )

    health = calculate_financial_health(
        income=income,
        current_expense=expense,
        budget=metrics.get("budget"),
        projected_expense=(forecast or {}).get("forecast"),
        recurring_burden=monthly_burden,
        subscription_burden=confirmed_monthly_cost,
        recurring_bill_burden=recurring_bill_monthly_burden,
        remaining_recurring_this_month=remaining_recurring_this_month,
        goal_pressure=goal_pressure_arg,
        total_required_monthly=total_required_monthly_arg,
        available_surplus=available_surplus_arg,
        goals_at_risk=goals_at_risk_count,
        savings_rate=savings_rate_arg,
        spending_trend_pct=spending_trend,
        days_passed=metrics.get("days_passed"),
        days_in_month=days_in_month,
    )
    return health
