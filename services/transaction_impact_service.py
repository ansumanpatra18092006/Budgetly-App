"""Transaction-impact orchestration.

This service is intentionally presentation-neutral. It gathers the same
financial context used by the unified Insights endpoint, simulates a pending
transaction, and evaluates both states through the canonical
``calculate_financial_health`` engine.
"""

from datetime import date, datetime, timedelta

from routes.ai_insights import (
    _fetch_full_metrics,
    _fetch_monthly_expense_history,
    _fetch_category_history,
)
from services.financial_health_snapshot import compute_health_for_context
from services.goal_pressure import calculate_goal_pressure
from services.recurring_service import (
    analyze_recurring_transactions,
    get_recurring_transactions,
    get_subscription_summary,
    get_recurring_income,
    get_upcoming_recurring,
)
from ml.forecast_model import predict_next_month_comprehensive
from utils.db import get_db


def _days_in_current_month(today):
    next_month = date(today.year + (today.month == 12), (today.month % 12) + 1, 1)
    return (next_month - timedelta(days=1)).day


def _forecast(
    history,
    metrics,
    category_history,
    monthly_burden,
    current_expense,
):
    today = datetime.today()
    days_in_month = _days_in_current_month(today)
    return predict_next_month_comprehensive(
        history=history,
        current_expense=current_expense,
        days_passed=metrics["days_passed"],
        days_in_month=days_in_month,
        historical_recurring=monthly_burden if history else None,
        expected_recurring_monthly=monthly_burden,
        target="next_month",
        month_indices=None,
        category_history=category_history,
    )


def _replace_current_month(history, month_indices, amount, expense):
    """Replace the current calendar month only; otherwise append it."""
    current_month = datetime.today().month
    if not history or not month_indices:
        return [amount]
    if int(month_indices[-1]) == current_month:
        return list(history[:-1]) + [amount]
    return list(history) + [expense]


def _goal_impacts(goal_details, avg_surplus, before_surplus, after_surplus):
    impacts = []
    for goal in goal_details or []:
        remaining = float(goal.get("remaining") or 0)
        if remaining <= 0:
            continue

        required = float(goal.get("monthly_required") or 0)
        if required <= 0:
            impacts.append({
                "goal_name": goal.get("name", "Goal"),
                "status": "on_track",
                "message": "No monthly contribution requirement is currently calculated.",
            })
            continue

        before_capacity = max(0.0, min(float(avg_surplus or 0), float(before_surplus or 0)))
        after_capacity = max(0.0, min(float(avg_surplus or 0), float(after_surplus or 0)))

        if after_capacity <= 0:
            status = "critical"
            message = "Current monthly surplus is no longer available for contributions."
        elif after_capacity < required:
            before_months = remaining / before_capacity if before_capacity > 0 else None
            after_months = remaining / after_capacity
            delay = (
                max(0.0, after_months - before_months)
                if before_months is not None else None
            )
            status = "delayed" if delay is None or delay > 0.25 else "on_track"
            message = (
                f"Projected timeline extends by ~{delay:.1f} month(s)."
                if delay is not None and delay > 0.25
                else "Goal remains broadly on track, but monthly headroom is tighter."
            )
        else:
            status = "on_track"
            message = "No material change to the monthly contribution capacity."

        impacts.append({
            "goal_name": goal.get("name", "Goal"),
            "status": status,
            "message": message,
            "monthly_required": round(required, 2),
        })
    return impacts


def evaluate_transaction_impact(user_id, *, amount, tx_type="expense", category="Misc"):
    amount = float(amount)
    tx_type = str(tx_type or "expense").lower()
    if tx_type not in ("income", "expense"):
        tx_type = "expense"
    category = str(category or "Misc")

    conn = get_db()
    try:
        metrics = _fetch_full_metrics(conn, user_id)
        history, month_indices = _fetch_monthly_expense_history(conn, user_id)
        category_history = _fetch_category_history(conn, user_id)
    finally:
        conn.close()

    recurring_data = analyze_recurring_transactions(user_id)
    subscriptions = get_subscription_summary(
        user_id, recurring_data=recurring_data
    )
    recurring_items = get_recurring_transactions(
        user_id, recurring_data=recurring_data
    )
    _ = get_recurring_income(
        user_id, recurring_data=recurring_data
    )
    _ = get_upcoming_recurring(
        user_id, recurring_data=recurring_data, days_ahead=30
    )

    active_subscriptions = [
        s for s in subscriptions["subscriptions"]
        if s.get("lifecycle_status") == "active"
    ]
    active_bills = [
        b for b in recurring_data["recurring_bills"]
        if b.get("lifecycle_status") == "active"
    ]
    confirmed_monthly_cost = round(
        sum(s["monthly_equivalent"] for s in active_subscriptions), 2
    )
    recurring_bill_monthly_burden = round(
        sum(b["monthly_equivalent"] for b in active_bills), 2
    )
    monthly_burden = round(
        confirmed_monthly_cost + recurring_bill_monthly_burden, 2
    )

    current_month_key = date.today().strftime("%Y-%m")
    remaining_recurring_this_month = round(sum(
        i["monthly_equivalent"]
        for i in recurring_items
        if i.get("classification") in ("subscription", "recurring_bill")
        and i.get("lifecycle_status") == "active"
        and i.get("payment_status") in ("due_soon", "upcoming", "overdue")
        and str(i.get("next_expected_date", "")).startswith(current_month_key)
    ), 2)

    income_before = float(metrics["income"])
    expense_before = float(metrics["expense"])
    budget = float(metrics["budget"])
    surplus_before = income_before - expense_before

    if tx_type == "expense":
        income_after = income_before
        expense_after = expense_before + amount
    else:
        income_after = income_before + amount
        expense_after = expense_before

    surplus_after = income_after - expense_after

    pressure_before = calculate_goal_pressure(
        metrics["goal_details"],
        metrics["avg_monthly_surplus"],
        current_surplus=surplus_before,
    )
    pressure_after = calculate_goal_pressure(
        metrics["goal_details"],
        metrics["avg_monthly_surplus"],
        current_surplus=surplus_after,
    )

    forecast_before = _forecast(
        history,
        metrics,
        category_history,
        monthly_burden,
        expense_before,
    )
    history_after = _replace_current_month(history, month_indices, expense_after, expense_after)
    forecast_after = _forecast(
        history_after,
        metrics,
        category_history,
        monthly_burden,
        expense_after,
    )

    # Rebuild transaction-specific metric maps while preserving the same
    # historical components used by the dashboard engine.
    metrics_before = dict(metrics)
    metrics_after = dict(metrics)
    metrics_after["income"] = income_after
    metrics_after["expense"] = expense_after
    metrics_after["savings_rate"] = (
        (surplus_after / income_after) * 100 if income_after > 0 else 0.0
    )
    metrics_after["goal_pressure"] = pressure_after
    metrics_after["total_monthly_required"] = metrics["total_monthly_required"]
    metrics_after["avg_monthly_surplus"] = metrics["avg_monthly_surplus"]

    days_in_month = _days_in_current_month(datetime.today())
    health_before = compute_health_for_context(
        metrics_before,
        forecast_before,
        monthly_burden=monthly_burden,
        confirmed_monthly_cost=confirmed_monthly_cost,
        recurring_bill_monthly_burden=recurring_bill_monthly_burden,
        remaining_recurring_this_month=remaining_recurring_this_month,
        goal_pressure_override=pressure_before,
        days_in_month=days_in_month,
    )
    health_after = compute_health_for_context(
        metrics_after,
        forecast_after,
        monthly_burden=monthly_burden,
        confirmed_monthly_cost=confirmed_monthly_cost,
        recurring_bill_monthly_burden=recurring_bill_monthly_burden,
        remaining_recurring_this_month=remaining_recurring_this_month,
        goal_pressure_override=pressure_after,
        days_in_month=days_in_month,
    )

    before_score = health_before.get("score")
    after_score = health_after.get("score")
    if after_score is None:
        # No score is an insufficient-data state, not maximum risk. Fall back
        # to directly observable consequences so we can still avoid false
        # certainty while giving the user a useful decision signal.
        severity = 50
    else:
        severity = int(max(0, min(100, 100 - after_score)))

    # Make severe cash-flow failures visible even if other healthy metrics
    # would otherwise dilute the headline score.
    if tx_type == "expense":
        if surplus_after < 0:
            severity = max(severity, 75)
        if budget > 0 and expense_after > budget:
            severity = max(severity, 65)
        savings_after = (surplus_after / income_after * 100) if income_after > 0 else None
        if savings_after is not None and savings_after < 5:
            severity = max(severity, 70)
        if pressure_after > pressure_before:
            severity = max(
                severity,
                int(min(100, severity + (pressure_after - pressure_before) * 0.35)),
            )

    if tx_type == "income":
        recommendation = "proceed"
    elif severity >= 70:
        recommendation = "avoid"
    elif severity >= 40:
        recommendation = "caution"
    else:
        recommendation = "proceed"

    recommendation_label = {
        "avoid": "Avoid This Purchase",
        "caution": "Consider Waiting",
        "proceed": "This Purchase Looks Manageable",
    }[recommendation]

    savings_before = (
        (surplus_before / income_before) * 100 if income_before > 0 else None
    )
    savings_after = (
        (surplus_after / income_after) * 100 if income_after > 0 else None
    )
    budget_before = (expense_before / budget * 100) if budget > 0 else None
    budget_after = (expense_after / budget * 100) if budget > 0 else None

    # Affordability is a context-free measure of whether the purchase can be
    # absorbed by this month's actual surplus. It deliberately avoids the
    # previous Flutter rule that used absolute ₹1,000/₹5,000 thresholds,
    # which made identical purchases score differently for different incomes.
    if tx_type == "income":
        affordability_before = 100.0 if surplus_before > 0 else 0.0
        affordability_after = 100.0
    elif surplus_before > 0:
        affordability_before = 100.0
        affordability_after = max(0.0, min(100.0, surplus_after / surplus_before * 100.0))
    else:
        affordability_before = 0.0
        affordability_after = 0.0

    reasons = []
    if tx_type == "expense":
        if surplus_after < 0:
            reasons.append("Monthly surplus becomes negative")
        elif surplus_after < surplus_before:
            reasons.append(
                f"Monthly surplus falls to ₹{abs(round(surplus_after)):,}"
            )
        if pressure_after > pressure_before:
            reasons.append("Goal pressure increases")
        elif any(g.get("status") in ("delayed", "critical")
                 for g in _goal_impacts(metrics["goal_details"], metrics["avg_monthly_surplus"], surplus_before, surplus_after)):
            reasons.append("At least one savings goal becomes harder to fund")
        if budget > 0 and budget_after >= 100:
            reasons.append("Monthly budget moves beyond its limit")
        elif budget > 0 and budget_after >= 85:
            reasons.append("Budget usage moves into the caution zone")

    goal_details = _goal_impacts(
        metrics["goal_details"],
        metrics["avg_monthly_surplus"],
        surplus_before,
        surplus_after,
    )

    explanation = (
        f"After this purchase, your projected monthly balance becomes "
        f"₹{abs(round(surplus_after)):,} below target."
        if tx_type == "expense" and surplus_after < 0
        else (
            "This payment reduces your financial headroom and may make your savings plan harder to maintain."
            if tx_type == "expense" and severity >= 40
            else "This payment stays within your current financial headroom."
        )
    )

    return {
        "status": "success",
        "recommendation": recommendation,
        "recommendation_label": recommendation_label,
        "explanation": explanation,
        "risk_level": "high" if severity >= 70 else "medium" if severity >= 40 else "low",
        "severity_score": severity,
        "severity_score_before": (100 - before_score) if before_score is not None else 50,
        "affordability_score_before": round(affordability_before, 1),
        "affordability_score_after": round(affordability_after, 1),
        "risk_reason": reasons[:3],
        "financial_score_before": before_score,
        "financial_score_after": after_score,
        "goal_pressure_before": round(pressure_before, 1),
        "goal_pressure_after": round(pressure_after, 1),
        "savings_rate_before": round(savings_before, 1) if savings_before is not None else None,
        "savings_rate_after": round(savings_after, 1) if savings_after is not None else None,
        "budget_before": round(budget_before, 1) if budget_before is not None else None,
        "budget_after": round(budget_after, 1) if budget_after is not None else None,
        "current_expense": round(expense_before, 2),
        "current_surplus": round(surplus_before, 2),
        "new_expense": round(expense_after, 2),
        "new_surplus": round(surplus_after, 2),
        "budget": round(budget, 2),
        "goal_impact_detail": goal_details[:8],
        "goal_impact": [g["message"] for g in goal_details[:3]],
        "goals_at_risk": sum(
            1 for g in goal_details if g["status"] in ("delayed", "critical")
        ),
        "projected_expense_before": forecast_before.get("forecast"),
        "projected_expense_after": forecast_after.get("forecast"),
        "financial_health_before": health_before,
        "financial_health_after": health_after,
        "confidence": (
            "high"
            if health_before.get("score") is not None and health_after.get("score") is not None
            else "limited"
        ),
    }
