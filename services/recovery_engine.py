"""Recovery-plan orchestration for FinTrust.

This module does not invent a second financial model. It consumes the same
canonical metrics, forecast, goal-pressure calculation, and health engine used
by the unified Insights endpoint and turns them into a decision-oriented
recovery plan.
"""
from __future__ import annotations

from math import ceil
from typing import Any

from services.financial_health_snapshot import compute_health_for_context
from services.goal_pressure import calculate_goal_pressure


_DISCRETIONARY = {
    "shopping", "food", "entertainment", "travel", "misc", "dining",
}


def _num(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _round(value: Any, digits: int = 0):
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return 0


def _confidence(metrics: dict, forecast: dict, category_history: dict) -> tuple[str, list[str]]:
    data_points = int(_num(forecast.get("data_points"), 0))
    if not data_points:
        data_points = max((len(v) for v in (category_history or {}).values()), default=0)
    income_stability = metrics.get("income_stability")

    evidence = []
    if data_points >= 6:
        evidence.append(f"{data_points} months of transaction history")
    elif data_points >= 3:
        evidence.append(f"{data_points} months of transaction history")
    if income_stability is not None:
        evidence.append("income pattern is measurable")
    if category_history:
        evidence.append("category spending history is available")

    if data_points >= 6 and income_stability is not None:
        return "high", evidence
    if data_points >= 3:
        return "medium", evidence
    return "low", evidence or ["limited transaction history"]


def _health_for_expense(metrics, base_forecast, expense, goal_details_override=None):
    income = _num(metrics.get("income"))
    surplus = income - expense
    scenario = dict(metrics)
    scenario["expense"] = expense
    scenario["savings_rate"] = (surplus / income * 100) if income > 0 else None
    scenario["goal_details"] = (goal_details_override if goal_details_override is not None else scenario.get("goal_details"))
    pressure = calculate_goal_pressure(
        scenario.get("goal_details") or [],
        scenario.get("avg_monthly_surplus"),
        current_surplus=surplus,
    )
    forecast = dict(base_forecast or {})
    forecast["forecast"] = max(0.0, expense)
    return compute_health_for_context(
        scenario,
        forecast,
        monthly_burden=_num(scenario.get("monthly_burden")),
        confirmed_monthly_cost=_num(scenario.get("confirmed_monthly_cost")),
        recurring_bill_monthly_burden=_num(scenario.get("recurring_bill_monthly_burden")),
        remaining_recurring_this_month=_num(scenario.get("remaining_recurring_this_month")),
        goal_pressure_override=pressure,
        expense_override=expense,
        days_in_month=int(_num(scenario.get("days_in_month"), 30)),
    )


def _component_breakdown(before: dict, after: dict) -> list[dict]:
    labels = {
        "savings": "Savings capacity",
        "budget": "Budget stability",
        "goals": "Goal funding",
        "recurring": "Fixed commitments",
        "trend": "Spending trend",
    }
    out = []
    for key, label in labels.items():
        b = (before.get("components") or {}).get(key) or {}
        a = (after.get("components") or {}).get(key) or {}
        out.append({
            "key": key,
            "label": label,
            "before": b.get("score"),
            "after": a.get("score"),
            "delta": _round(_num(a.get("score")) - _num(b.get("score")), 1)
                if b.get("score") is not None and a.get("score") is not None else None,
            "weight": b.get("weight") if b.get("weight") is not None else a.get("weight"),
            "status": a.get("status") or b.get("status"),
        })
    return out


def _scenario(label: str, expense: float, metrics: dict, forecast: dict, base_health: dict, goal_details_override=None) -> dict:
    health = _health_for_expense(metrics, forecast, expense, goal_details_override=goal_details_override)
    income = _num(metrics.get("income"))
    return {
        "label": label,
        "expense": _round(expense, 2),
        "surplus": _round(income - expense, 2),
        "deficit": _round(max(0.0, expense - income), 2) if income > 0 else None,
        "health": health.get("score"),
        "goal_pressure": health.get("summary", {}).get("goal_pressure"),
        "savings_rate": health.get("summary", {}).get("projected_savings_rate"),
    }


def _timeline(metrics: dict, forecast: dict, current_expense: float, monthly_recovery: float, months: int) -> list[dict]:
    baseline = max(0.0, _num(forecast.get("forecast"), current_expense))
    income = _num(metrics.get("income"))
    steps = []
    max_months = max(3, min(6, months))
    for month in range(1, max_months + 1):
        reduction = min(baseline, monthly_recovery * month) if monthly_recovery > 0 else 0.0
        expense = max(0.0, baseline - reduction)
        health = _health_for_expense(metrics, forecast, expense)
        steps.append({
            "month": month,
            "label": "Next month" if month == 1 else f"Month {month}",
            "expense": _round(expense, 2),
            "surplus": _round(income - expense, 2),
            "health": health.get("score"),
            "goal_pressure": health.get("summary", {}).get("goal_pressure"),
        })
    return steps


def build_recovery_plan(
    *,
    metrics: dict,
    health: dict,
    forecast: dict,
    current_category_totals: dict | None = None,
    category_history: dict | None = None,
    recurring_items: list | None = None,
    subscription_summary: dict | None = None,
) -> dict | None:
    current_category_totals = current_category_totals or {}
    category_history = category_history or {}
    recurring_items = recurring_items or []
    subscription_summary = subscription_summary or {}

    income = _num(metrics.get("income"))
    current_expense = _num(metrics.get("expense"))
    projected_expense = _num((health.get("cash_flow") or {}).get("projected_expense"), _num(forecast.get("forecast"), current_expense))
    budget = _num(metrics.get("budget"))
    projected_health = health.get("score")
    goal_pressure = _num((health.get("summary") or {}).get("goal_pressure"), _num(metrics.get("goal_pressure")))

    target_limits = [v for v in (income, budget) if v > 0]
    recovery_target = min(target_limits) if target_limits else None
    recovery_gap = max(0.0, projected_expense - recovery_target) if recovery_target is not None else 0.0
    budget_gap = max(0.0, projected_expense - budget) if budget > 0 else 0.0

    trigger = (
        recovery_gap > 0
        or budget_gap > 0
        or (projected_health is not None and projected_health < 50)
        or goal_pressure >= 70
    )
    if not trigger:
        return None

    confidence, confidence_evidence = _confidence(metrics, forecast, category_history)

    # Build category levers from actual current-month spend. Where history is
    # available, return to a recent baseline; otherwise use a conservative 20%
    # cut rather than pretending to know an exact saving amount.
    candidates = []
    for category, raw_spend in current_category_totals.items():
        if str(category).strip().lower() not in _DISCRETIONARY:
            continue
        spend = _num(raw_spend)
        if spend <= 0:
            continue
        series = [_num(x) for x in (category_history.get(category) or [])]
        baseline = None
        if len(series) >= 3:
            baseline = sum(series[:-1]) / max(1, len(series) - 1)
        overage = max(0.0, spend - baseline) if baseline is not None else spend * 0.20
        if overage <= 0:
            continue
        confidence_cat = "High" if len(series) >= 4 else "Medium" if len(series) >= 2 else "Low"
        candidates.append((overage, category, spend, baseline, confidence_cat))
    candidates.sort(reverse=True)

    actions = []
    remaining = recovery_gap

    for overage, category, spend, baseline, cat_conf in candidates[:2]:
        if remaining <= 0:
            break
        impact = min(remaining, overage)
        target_phrase = (
            f"bring {category} back toward its recent baseline"
            if baseline is not None else
            f"trim {category} by about 20%"
        )
        actions.append({
            "rank": len(actions) + 1,
            "title": f"{target_phrase.capitalize()}",
            "description": (
                f"{category} is currently about ₹{_round(spend):,}/month. "
                + (f"Recent baseline is about ₹{_round(baseline):,}." if baseline is not None else "There is not enough history for a precise baseline.")
            ),
            "action": f"{target_phrase}.",
            "impact": _round(impact),
            "impact_type": "cash_recovery",
            "difficulty": "Medium",
            "confidence": cat_conf,
            "evidence": [
                f"Current {category} spend: ₹{_round(spend):,}",
                *( [f"Recent baseline: ₹{_round(baseline):,}"] if baseline is not None else [] ),
            ],
        })
        remaining -= impact

    if remaining > 0:
        discretionary_spend = sum(
            _num(v) for k, v in current_category_totals.items()
            if str(k).strip().lower() in _DISCRETIONARY
        )
        if discretionary_spend > 0:
            impact = min(remaining, discretionary_spend * 0.50)
            if impact > 0:
                actions.append({
                    "rank": len(actions) + 1,
                    "title": "Pause discretionary spending",
                    "description": "Temporarily pause optional purchases while the monthly gap is repaired.",
                    "action": "Keep discretionary spending close to zero until projected surplus is positive.",
                    "impact": _round(impact),
                    "impact_type": "cash_recovery",
                    "difficulty": "Easy",
                    "confidence": "High" if discretionary_spend > 0 and _confidence(metrics, forecast, category_history)[0] != "low" else "Medium",
                    "evidence": [f"Current discretionary-category spend: ₹{_round(discretionary_spend):,}"],
                })
                remaining -= impact

    active_subscriptions = [s for s in (subscription_summary.get("subscriptions") or []) if s.get("lifecycle_status") == "active"]
    active_subscriptions.sort(key=lambda s: _num(s.get("monthly_equivalent")), reverse=True)
    if remaining > 0 and active_subscriptions:
        biggest = active_subscriptions[0]
        impact = min(remaining, _num(biggest.get("monthly_equivalent")))
        if impact > 0:
            name = biggest.get("name") or "largest recurring service"
            actions.append({
                "rank": len(actions) + 1,
                "title": f"Review {name}",
                "description": f"This recurring commitment is about ₹{_round(impact):,}/month.",
                "action": "Pause, downgrade, or remove it if it is no longer valuable.",
                "impact": _round(impact),
                "impact_type": "cash_recovery",
                "difficulty": "Medium",
                "confidence": "High",
                "evidence": ["Active recurring commitment with a known monthly equivalent"],
            })
            remaining -= impact

    # Goal-date changes do not create cash; they reduce required monthly goal
    # funding and therefore relieve pressure. Keep that benefit separate so
    # scenario cash flow is never overstated.
    risky_goal = next((g for g in metrics.get("goal_details") or [] if g.get("goal_risk") == "high" and _num(g.get("monthly_required")) > 0), None)
    if risky_goal:
        required = _num(risky_goal.get("monthly_required"))
        actions.append({
            "rank": len(actions) + 1,
            "title": f"Give {risky_goal.get('name') or 'your goal'} more time",
            "description": f"A later target date would reduce the required monthly contribution of about ₹{_round(required):,}.",
            "action": "Move the target date only if it protects essential cash flow.",
            "impact": _round(required),
            "impact_type": "goal_pressure_relief",
            "difficulty": "Hard",
            "confidence": "Very High" if risky_goal.get("target_date") else "High",
            "evidence": [f"Required monthly goal funding: ₹{_round(required):,}", "Goal is currently classified as high risk"],
        })

    if not actions:
        return None

    # Rank by actual measurable financial effect; goal-pressure-only actions
    # remain visible but do not outrank cash-recovery levers with real impact.
    actions.sort(key=lambda a: (a["impact_type"] != "cash_recovery", -_num(a.get("impact"))))
    for idx, action in enumerate(actions, 1):
        action["rank"] = idx

    cash_impacts = [a for a in actions if a["impact_type"] == "cash_recovery"]
    top_cash_impact = sum(_num(a["impact"]) for a in cash_impacts)
    top_cash_impact = min(recovery_gap, top_cash_impact) if recovery_gap > 0 else 0.0
    all_expense = max(0.0, projected_expense - top_cash_impact)
    top_expense = max(0.0, projected_expense - _num(actions[0].get("impact")) if actions[0]["impact_type"] == "cash_recovery" else projected_expense)

    goal_recovery_details = None
    for action in actions:
        if action.get("impact_type") == "goal_pressure_relief":
            goal_name = action.get("title", "")
            match = next((g for g in metrics.get("goal_details") or [] if (g.get("name") or "") in goal_name), None)
            if match is None:
                match = risky_goal
            if match is not None and _num(match.get("months_left")) > 0 and _num(match.get("remaining")) > 0:
                goal_recovery_details = [dict(g) for g in (metrics.get("goal_details") or [])]
                old_months = max(1.0, _num(match.get("months_left"), 1))
                new_required = _num(match.get("remaining")) / (old_months + 2.0)
                for g in goal_recovery_details:
                    if g.get("id") == match.get("id"):
                        g["monthly_required"] = round(new_required, 2)
                        g["months_left"] = old_months + 2.0
                        g["goal_risk"] = "medium" if new_required > _num(metrics.get("avg_monthly_surplus")) else "low"
                action["evidence"].append(f"Two-month extension would lower required monthly funding to about ₹{_round(new_required):,}")
                break

    no_action = _scenario("If you do nothing", projected_expense, metrics, forecast, health)
    top_action = _scenario(
        "Follow top action",
        top_expense,
        metrics,
        forecast,
        health,
        goal_details_override=goal_recovery_details if actions and actions[0].get("impact_type") == "goal_pressure_relief" else None,
    )
    all_actions = _scenario(
        "Follow the recovery plan",
        all_expense,
        metrics,
        forecast,
        health,
        goal_details_override=goal_recovery_details,
    )

    # The amount needed to reach the stricter of income and budget constraints.
    monthly_recovery = max(0.0, top_cash_impact)
    estimated_months = int(ceil(recovery_gap / monthly_recovery)) if recovery_gap > 0 and monthly_recovery > 0 else 0
    timeline = _timeline(metrics, forecast, current_expense, monthly_recovery, max(3, estimated_months or 3))

    cause_category = candidates[0][1] if candidates else None
    if cause_category and candidates[0][3] is not None:
        cause = (
            f"{cause_category} is the clearest spending driver: it is about "
            f"₹{_round(candidates[0][2] - candidates[0][3]):,} above its recent baseline."
        )
    elif recovery_gap > 0:
        cause = f"Projected spending is about ₹{_round(recovery_gap):,} above the amount your income/budget can safely support."
    elif budget_gap > 0:
        cause = f"Projected spending is about ₹{_round(budget_gap):,} above your configured budget."
    else:
        cause = "Goal funding is absorbing too much of the monthly headroom available."

    health_recovery = _health_for_expense(metrics, forecast, all_expense)
    health_breakdown = _component_breakdown(health, health_recovery)

    return {
        "status": "active",
        "headline": "You are off track — here is the fastest path back.",
        "cause": cause,
        "what_it_means": (
            f"Your projected monthly spending is ₹{_round(projected_expense):,}. "
            + (f"That leaves a ₹{_round(recovery_gap):,} gap against income and budget." if recovery_gap > 0 else "Your cash flow is not the main issue right now; goal pressure is.")
        ),
        "recovery_amount": _round(recovery_gap),
        "recovery_target": _round(recovery_target) if recovery_target is not None else None,
        "projected_expense": _round(projected_expense),
        "confidence": confidence,
        "confidence_evidence": confidence_evidence,
        "actions": actions[:4],
        "scenarios": [no_action, top_action, all_actions],
        "health_breakdown": health_breakdown,
        "timeline": timeline[:6],
        "estimated_recovery_months": estimated_months or None,
        "goal_pressure_relief": _round(max(0.0, goal_pressure - _num(all_actions.get("goal_pressure"))), 1),
    }
