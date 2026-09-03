from services.recovery_engine import build_recovery_plan


def _base():
    metrics = {
        "income": 50000,
        "expense": 42000,
        "budget": 40000,
        "savings_rate": 16,
        "income_stability": 90,
        "goal_pressure": 72,
        "goal_details": [
            {"name": "Laptop", "goal_risk": "high", "monthly_required": 5000, "remaining": 20000, "target_date": "2026-12-01"}
        ],
        "avg_monthly_surplus": 5000,
        "days_in_month": 30,
        "monthly_burden": 8000,
        "confirmed_monthly_cost": 3000,
        "recurring_bill_monthly_burden": 5000,
        "remaining_recurring_this_month": 1000,
    }
    health = {
        "score": 42,
        "summary": {"goal_pressure": 72, "projected_surplus": -4000, "projected_savings_rate": -8},
        "cash_flow": {"projected_expense": 54000},
        "budget": {"budget_status": "exceeded", "projected_budget_usage_pct": 135, "budget_usage_pct": 105},
        "components": {
            "savings": {"score": 6, "weight": 30, "status": "critical"},
            "budget": {"score": 0, "weight": 25, "status": "exceeded"},
            "goals": {"score": 3, "weight": 15, "status": "at_risk"},
            "recurring": {"score": 14, "weight": 20, "status": "healthy"},
            "trend": {"score": 5, "weight": 10, "status": "increasing"},
        },
    }
    forecast = {"forecast": 54000, "data_points": 6}
    return metrics, health, forecast


def test_recovery_plan_prioritizes_discretionary_actions():
    metrics, health, forecast = _base()
    plan = build_recovery_plan(
        metrics=metrics,
        health=health,
        forecast=forecast,
        current_category_totals={"Rent": 15000, "Shopping": 12000, "Food": 9000},
        category_history={"Shopping": [8000, 9000, 9500, 10000, 10500, 12000], "Food": [7000, 7500, 8000, 8200, 8500, 9000]},
        subscription_summary={"subscriptions": []},
    )
    assert plan is not None
    impacts = [a["impact"] for a in plan["actions"] if a["impact_type"] == "cash_recovery"]
    assert impacts == sorted(impacts, reverse=True)
    assert all("rent" not in a["title"].lower() for a in plan["actions"])
    assert len(plan["scenarios"]) == 3
    assert len(plan["health_breakdown"]) == 5
    assert len(plan["timeline"]) >= 3


def test_no_recovery_when_finances_are_stable():
    metrics, health, forecast = _base()
    metrics.update({"income": 60000, "expense": 28000, "budget": 40000, "goal_pressure": 30, "goal_details": []})
    health = dict(health)
    health["score"] = 84
    health["summary"] = {"goal_pressure": 30, "projected_surplus": 30000, "projected_savings_rate": 50}
    health["cash_flow"] = {"projected_expense": 30000}
    health["budget"] = {"budget_status": "within_budget", "projected_budget_usage_pct": 75, "budget_usage_pct": 70}
    assert build_recovery_plan(
        metrics=metrics,
        health=health,
        forecast={"forecast": 30000, "data_points": 6},
        current_category_totals={"Shopping": 5000},
        category_history={"Shopping": [4000, 4200, 4500, 4800, 5000, 5000]},
        subscription_summary={"subscriptions": []},
    ) is None
