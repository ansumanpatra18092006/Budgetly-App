"""Canonical goal-pressure calculation used by health and purchase impact."""


def calculate_goal_pressure(goal_details, avg_monthly_surplus, current_surplus=None):
    """Return a continuous 0-100 goal-pressure score.

    Pressure is based on sustainable monthly headroom versus the amount
    active goals require each month. A purchase that reduces headroom therefore
    produces a predictable pressure delta instead of a jump between arbitrary
    score buckets.
    """
    active = [
        g for g in (goal_details or [])
        if float(g.get("remaining") or 0) > 0
    ]
    total_required = sum(
        float(g.get("monthly_required") or 0) for g in active
    )
    if total_required <= 0:
        return 0.0

    historical_surplus = max(float(avg_monthly_surplus or 0), 0.0)
    if current_surplus is None:
        sustainable_surplus = historical_surplus
    else:
        sustainable_surplus = min(
            historical_surplus,
            max(float(current_surplus), 0.0),
        )

    # 150% coverage is the target comfort margin. At 100% coverage,
    # pressure is ~33; at 75% ~50; at 50% ~67; at 0% = 100.
    coverage = sustainable_surplus / total_required
    pressure = max(0.0, min(100.0, 100.0 * (1.0 - coverage / 1.5)))

    urgent = any(
        g.get("goal_risk") == "high"
        and float(g.get("remaining") or 0) > 0
        and (
            g.get("months_left") is None
            or float(g.get("months_left")) <= 2
        )
        for g in active
    )
    if urgent:
        pressure = min(100.0, pressure + 7.0)

    return round(pressure, 1)
