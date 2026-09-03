"""
Financial Health / Risk model.

This module contains pure calculation logic only. It does not import
route/orchestration modules (routes/ai_insights.py, routes/insights.py)
to avoid circular imports. Callers are expected to gather the required
data (income, expenses, recurring burden, goal info, forecast, etc.)
and pass it in.

Two public entry points:

    calculate_financial_health(...)
        The new, explainable, deterministic scoring engine described in
        the design doc. Accepts many OPTIONAL inputs and gracefully
        degrades when data is missing instead of inventing values.

    predict_risk(income, expense, budget, days_passed=15)
        Legacy API kept for backward compatibility with existing callers
        (e.g. decision_engine.py). Internally delegates to
        calculate_financial_health().

Nothing here is a statistically calibrated probability model. All
"probability" / "score" values are deterministic, rule-based severity
scores, not outputs of a trained ML model.
"""

import math
from decimal import Decimal, InvalidOperation

# ---------------------------------------------------------------------------
# Numeric safety helpers
# ---------------------------------------------------------------------------

def _safe_num(x):
    """Coerce input into a plain float, or None if it isn't a usable number.

    Handles None, Decimal, int, float, numeric strings, NaN and Infinity.
    Never raises.
    """
    if x is None:
        return None
    if isinstance(x, bool):
        # bool is a subclass of int; treat as invalid to avoid silent bugs
        return None
    if isinstance(x, Decimal):
        try:
            x = float(x)
        except (InvalidOperation, ValueError, OverflowError):
            return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _round(x, ndigits=1):
    if x is None:
        return None
    return round(float(x), ndigits)


# ---------------------------------------------------------------------------
# Component weights (must sum to 100)
# ---------------------------------------------------------------------------

WEIGHTS = {
    "savings": 30,
    "budget": 25,
    "recurring": 20,
    "goals": 15,
    "trend": 10,
}
assert sum(WEIGHTS.values()) == 100


# ---------------------------------------------------------------------------
# Individual component calculators.
#
# Each returns a dict describing the component. When the underlying data
# isn't available, "available" is False, "score" is None, and the
# component is excluded from the final weighted score (weights are
# renormalized across whatever components ARE available) rather than
# silently defaulting to a fabricated value.
# ---------------------------------------------------------------------------

def _savings_component(income, current_expense, reliable_projected_expense, savings_rate_input):
    """Savings-rate component.

    Distinguishes a CURRENT savings rate (income vs. current_expense) from
    a PROJECTED savings rate (income vs. a *reliable* projected/forecast
    expense - i.e. an externally supplied forecast, or current_expense plus
    known remaining recurring commitments; NOT a rough burn-rate
    extrapolation, which is too speculative to drive scoring here).

    Preference order for which rate actually drives the score/status:
      1. an explicitly supplied savings_rate_input
      2. the projected savings rate, when a reliable projected expense is
         available and income > 0
      3. the current savings rate
      4. unavailable

    Whichever rate is NOT chosen as the basis is still surfaced (when
    computable) as a reference value, so callers always see both current
    and projected savings rate where possible.
    """
    weight = WEIGHTS["savings"]

    current_rate = None
    if income is not None and income > 0 and current_expense is not None:
        current_rate = (income - current_expense) / income * 100

    projected_rate = None
    if income is not None and income > 0 and reliable_projected_expense is not None:
        projected_rate = (income - reliable_projected_expense) / income * 100

    # Explicit "no income, but expenses exist" signal - a clear negative
    # finding, not "insufficient data". Only applies when nothing was
    # explicitly supplied to override it.
    if savings_rate_input is None and income == 0 and current_expense not in (None, 0):
        return {"available": True, "score": 0.0, "weight": weight,
                "value": None, "unit": "percent", "status": "no_income",
                "savings_basis": "current", "current_value": None,
                "projected_value": None,
                "note": "Expenses recorded with zero income."}

    if savings_rate_input is not None:
        rate = savings_rate_input
        basis = "explicit"
    elif projected_rate is not None:
        rate = projected_rate
        basis = "projected"
    elif current_rate is not None:
        rate = current_rate
        basis = "current"
    else:
        return {"available": False, "score": None, "weight": weight,
                "value": None, "unit": "percent", "status": "unavailable",
                "savings_basis": None, "current_value": current_rate,
                "projected_value": projected_rate}

    if rate < 5:
        status = "critical"
    elif rate < 10:
        status = "weak"
    elif rate < 20:
        status = "moderate"
    else:
        status = "strong"

    score = _clamp((rate / 20.0) * weight, 0, weight)
    return {"available": True, "score": score, "weight": weight,
            "value": _round(rate), "unit": "percent", "status": status,
            "savings_basis": basis,
            "current_value": _round(current_rate) if current_rate is not None else None,
            "projected_value": _round(projected_rate) if projected_rate is not None else None}


def _budget_component(current_expense, budget, has_context):
    """Budget-adherence component.

    A missing/zero budget is *unavailable*, not evidence of poor financial
    health. The final score excludes this component and renormalizes the
    remaining available components. When a real budget exists, usage is
    measured directly from actual expenses versus that budget.
    """
    weight = WEIGHTS["budget"]

    if budget is None or budget <= 0:
        return {"available": False, "score": None, "weight": weight,
                "value": None, "unit": "percent_used", "status": "unavailable",
                "budget_status": "no_budget", "score_basis": None}

    if current_expense is None:
        return {"available": False, "score": None, "weight": weight,
                "value": None, "unit": "percent_used", "status": "unavailable",
                "budget_status": "unknown", "score_basis": None}

    usage_pct = current_expense / budget * 100
    if usage_pct > 100:
        status = "exceeded"
        score = 0.0
    elif usage_pct >= 80:
        status = "approaching"
        score = _clamp(weight * (1 - usage_pct / 100), 0, weight)
    else:
        status = "comfortable"
        score = _clamp(weight * (1 - usage_pct / 100), 0, weight)

    return {"available": True, "score": score, "weight": weight,
            "value": _round(usage_pct), "unit": "percent_used", "status": status,
            "budget_status": status, "score_basis": "measured"}

def _recurring_component(income, recurring_burden, subscription_burden, recurring_bill_burden):
    weight = WEIGHTS["recurring"]

    burden = recurring_burden
    if burden is None and (subscription_burden is not None or recurring_bill_burden is not None):
        burden = (subscription_burden or 0) + (recurring_bill_burden or 0)

    if income is None or income == 0 or burden is None:
        return {"available": False, "score": None, "weight": weight,
                "value": None, "unit": "percent_of_income", "status": "unavailable"}

    ratio = burden / income * 100
    if ratio < 20:
        status = "healthy"
    elif ratio < 30:
        status = "moderate"
    elif ratio <= 50:
        status = "elevated"
    else:
        status = "high"

    score = _clamp(weight * (1 - ratio / 50.0), 0, weight)
    return {"available": True, "score": score, "weight": weight,
            "value": _round(ratio), "unit": "percent_of_income", "status": status}


def _goals_component(goal_pressure, total_required_monthly, available_surplus, goals_at_risk_input):
    """Goal-pressure component.

    "pressure_basis" records where the pressure figure came from:
      - "explicit": caller supplied goal_pressure directly.
      - "computed": derived from total_required_monthly / available_surplus.
      - None: unavailable, no pressure could be determined.
    """
    weight = WEIGHTS["goals"]

    pressure = None
    pressure_basis = None
    if goal_pressure is not None:
        pressure = _clamp(goal_pressure, 0, 200)
        pressure_basis = "explicit"
    elif total_required_monthly is not None and available_surplus is not None:
        if total_required_monthly <= 0:
            pressure = 0.0
        elif available_surplus <= 0:
            pressure = 100.0
        else:
            pressure = _clamp(total_required_monthly / available_surplus * 100, 0, 200)
        pressure_basis = "computed"

    if pressure is None:
        return {"available": False, "score": None, "weight": weight,
                "value": None, "unit": "pressure", "status": "unavailable",
                "goals_at_risk": goals_at_risk_input, "pressure_basis": None}

    if pressure < 50:
        status = "comfortable"
    elif pressure <= 80:
        status = "moderate"
    else:
        status = "at_risk"

    score = _clamp(weight * (1 - min(pressure, 100) / 100.0), 0, weight)
    at_risk = bool(goals_at_risk_input) or pressure > 80
    return {"available": True, "score": score, "weight": weight,
            "value": _round(pressure), "unit": "pressure", "status": status,
            "goals_at_risk": at_risk, "pressure_basis": pressure_basis}


def _trend_component(spending_trend_pct):
    """Spending-trend component.

    This is a deterministic POLICY HEURISTIC on the rate of spending
    change (e.g. from an already-calculated forecast/trend), not a
    learned or statistical model. "score_basis" is always
    "policy_heuristic" for this component when available.
    """
    weight = WEIGHTS["trend"]

    if spending_trend_pct is None:
        return {"available": False, "score": None, "weight": weight,
                "value": None, "unit": "percent_change", "status": "unavailable",
                "score_basis": None}

    if spending_trend_pct > 5:
        status = "increasing"
    elif spending_trend_pct < -5:
        status = "decreasing"
    else:
        status = "stable"

    score = _clamp(weight - (spending_trend_pct / 2.0), 0, weight)
    return {"available": True, "score": score, "weight": weight,
            "value": _round(spending_trend_pct), "unit": "percent_change", "status": status,
            "score_basis": "policy_heuristic"}


# ---------------------------------------------------------------------------
# Projected expense / cash-flow / budget-breach helpers
# ---------------------------------------------------------------------------

def _compute_projected_expense(current_expense, projected_expense_input,
                                 remaining_recurring_this_month,
                                 days_passed, days_in_month):
    """Resolve a single, non-double-counted projected monthly expense.

    Preference order:
      1. An externally supplied forecast (projected_expense_input) - e.g.
         from forecast_model.py. This is authoritative when present, so we
         don't maintain a contradictory second methodology.
      2. current_expense + remaining (not-yet-occurred) recurring
         commitments. This deliberately avoids adding the FULL recurring
         burden on top of current_expense, since recurring payments that
         already happened this month are already inside current_expense.
      3. A conservative burn-rate projection from current_expense using
         days elapsed / days in month.
      4. current_expense as-is, if nothing else is available.
    """
    if projected_expense_input is not None:
        return projected_expense_input, "forecast_supplied"

    if current_expense is not None and remaining_recurring_this_month is not None:
        return current_expense + remaining_recurring_this_month, "current_plus_remaining_recurring"

    if current_expense is not None and days_passed is not None and days_passed > 0 and days_in_month:
        return (current_expense / days_passed) * days_in_month, "burn_rate_projection"

    if current_expense is not None:
        return current_expense, "current_expense_only"

    return None, "unavailable"


# ---------------------------------------------------------------------------
# Public: calculate_financial_health
# ---------------------------------------------------------------------------

def calculate_financial_health(
    income=None,
    current_expense=None,
    budget=None,
    projected_expense=None,
    recurring_burden=None,
    subscription_burden=None,
    recurring_bill_burden=None,
    remaining_recurring_this_month=None,
    goal_pressure=None,
    total_required_monthly=None,
    available_surplus=None,
    goals_at_risk=None,
    savings_rate=None,
    spending_trend_pct=None,
    runway_days=None,
    days_passed=None,
    days_in_month=30,
):
    """Deterministic, explainable Financial Health engine.

    All parameters are optional. When a parameter needed for a given
    component is missing, that component is marked "unavailable" and
    excluded from the weighted score (the remaining components' weights
    are renormalized to 100) rather than a value being invented.

    Returns a dict:
        {
          "status": "ok" | "insufficient_data",
          "score": int | None,
          "risk_level": str,
          "components": {...},
          "main_risk_factors": [...],
          "positive_factors": [...],
          "cash_flow": {...},
          "budget": {...},
          "runway": {...},
          "overrides_applied": [...],
        }
    """
    # --- numeric safety pass -------------------------------------------------
    income = _safe_num(income)
    current_expense = _safe_num(current_expense)
    budget = _safe_num(budget)
    projected_expense_input = _safe_num(projected_expense)
    recurring_burden = _safe_num(recurring_burden)
    subscription_burden = _safe_num(subscription_burden)
    recurring_bill_burden = _safe_num(recurring_bill_burden)
    remaining_recurring_this_month = _safe_num(remaining_recurring_this_month)
    goal_pressure = _safe_num(goal_pressure)
    total_required_monthly = _safe_num(total_required_monthly)
    available_surplus = _safe_num(available_surplus)
    savings_rate = _safe_num(savings_rate)
    spending_trend_pct = _safe_num(spending_trend_pct)
    runway_days = _safe_num(runway_days)
    days_passed = _safe_num(days_passed)
    days_in_month = _safe_num(days_in_month) or 30

    # Negative income/expense/budget are not physically meaningful; treat as
    # missing rather than propagating nonsense.
    if income is not None and income < 0:
        income = None
    if current_expense is not None and current_expense < 0:
        current_expense = None
    if budget is not None and budget < 0:
        budget = None

    # --- projected expense (computed up-front so the savings component can
    # prefer a projected savings rate when a RELIABLE projection exists) ---
    proj_expense, proj_method = _compute_projected_expense(
        current_expense, projected_expense_input, remaining_recurring_this_month,
        days_passed, days_in_month,
    )
    # "Reliable" here means an externally supplied forecast, or
    # current_expense plus a known remaining-recurring commitment - not a
    # rough burn-rate extrapolation, which is too speculative to drive the
    # savings component's scoring basis (though it's still surfaced in
    # cash_flow below).
    reliable_projected_expense = proj_expense if proj_method in (
        "forecast_supplied", "current_plus_remaining_recurring",
    ) else None

    # --- components ------------------------------------------------------
    components = {
        "savings": _savings_component(income, current_expense, reliable_projected_expense, savings_rate),
        "budget": _budget_component(
            current_expense, budget,
            has_context=(
                (income is not None or current_expense is not None)
                and not (income == 0 and current_expense == 0)
            ),
        ),
        "recurring": _recurring_component(income, recurring_burden, subscription_burden, recurring_bill_burden),
        "goals": _goals_component(goal_pressure, total_required_monthly, available_surplus, goals_at_risk),
        "trend": _trend_component(spending_trend_pct),
    }

    available_weight = sum(c["weight"] for c in components.values() if c["available"])
    raw_points = sum(c["score"] for c in components.values() if c["available"] and c["score"] is not None)

    if available_weight == 0:
        return {
            "status": "insufficient_data",
            "score": None,
            "risk_level": "insufficient_data",
            "components": components,
            "main_risk_factors": [],
            "positive_factors": [],
            "cash_flow": {"projected_expense": None, "projected_expense_method": "unavailable",
                          "projected_surplus": None, "projected_savings_rate": None, "status": "unavailable"},
            "budget": {"budget_usage_pct": None, "projected_budget_usage_pct": None, "budget_breach": None,
                       "budget_status": "no_budget" if budget is None else "unknown"},
            "runway": {"runway_days": None, "runway_status": "not_available"},
            "overrides_applied": [],
            "summary": {
                "current_savings_rate": None, "projected_savings_rate": None,
                "projected_surplus": None, "current_budget_usage_pct": None,
                "projected_budget_usage_pct": None, "recurring_burden_pct": None,
                "goal_pressure": None, "spending_trend_pct": None,
            },
        }

    score = round(_clamp((raw_points / available_weight) * 100, 0, 100))

    # --- cash flow / budget breach (proj_expense already resolved above) --
    if budget is not None and budget > 0:
        budget_usage_pct = (current_expense / budget * 100) if current_expense is not None else None
        projected_budget_usage_pct = (proj_expense / budget * 100) if proj_expense is not None else None
        budget_breach = (proj_expense > budget) if proj_expense is not None else None
        budget_status = "no_budget" if False else ("exceeded" if (budget_breach) else "within_budget" if budget_breach is not None else "unknown")
    else:
        budget_usage_pct = None
        projected_budget_usage_pct = None
        budget_breach = None
        budget_status = "no_budget"

    if income is not None and proj_expense is not None:
        projected_surplus = income - proj_expense
        projected_savings_rate = (projected_surplus / income * 100) if income > 0 else None
        if projected_surplus < 0:
            cash_flow_status = "deficit"
        elif income > 0 and projected_savings_rate is not None:
            if projected_savings_rate >= 20:
                cash_flow_status = "strong_surplus"
            elif projected_savings_rate >= 10:
                cash_flow_status = "healthy_surplus"
            else:
                cash_flow_status = "thin_surplus"
        else:
            cash_flow_status = "unknown"
    else:
        projected_surplus = None
        projected_savings_rate = None
        cash_flow_status = "unavailable"

    # --- runway (pass-through / interpret only, never fabricate) ----------
    if runway_days is not None and runway_days >= 0:
        if runway_days < 30:
            runway_status = "critical"
        elif runway_days < 90:
            runway_status = "tight"
        else:
            runway_status = "comfortable"
        runway_out = {"runway_days": _round(runway_days, 0), "runway_status": runway_status}
    else:
        runway_out = {"runway_days": None, "runway_status": "not_available"}

    # --- priority overrides -------------------------------------------
    # These are explicit POLICY OVERRIDES layered on top of the weighted
    # component score - deliberate business rules, not measurements in
    # themselves. Each caps the score so certain situations can never be
    # reported as healthier than policy allows, regardless of how the
    # weighted components alone would have scored.
    overrides_applied = []

    if cash_flow_status == "deficit":
        # Policy: a projected cash-flow deficit can never be "healthy"/"good".
        score = min(score, 49)
        overrides_applied.append("projected_deficit_cap")

    recurring_comp = components["recurring"]
    if recurring_comp["available"] and recurring_comp["value"] is not None and recurring_comp["value"] > 50:
        # Policy: fixed costs eating over half of income cap the score.
        score = min(score, 49)
        overrides_applied.append("extreme_recurring_burden_cap")

    savings_comp = components["savings"]
    no_income_with_expense = savings_comp.get("status") == "no_income"
    if no_income_with_expense:
        # Policy: real expenses with zero recorded income is a strong caution
        # signal even though it isn't "insufficient data".
        score = min(score, 29)
        overrides_applied.append("expense_with_no_income_cap")

    score = int(_clamp(round(score), 0, 100))

    # --- risk level ---------------------------------------------------------
    if score >= 80:
        risk_level = "healthy"
    elif score >= 65:
        risk_level = "good"
    elif score >= 50:
        risk_level = "moderate"
    elif score >= 30:
        risk_level = "elevated_risk"
    else:
        risk_level = "high_risk"

    # --- explanations ---------------------------------------------------
    main_risk_factors = []
    positive_factors = []

    sv = components["savings"]
    if sv["available"]:
        if sv["status"] == "no_income":
            main_risk_factors.append(
                "Income data is unavailable, so savings rate cannot be evaluated. "
                "Expenses are recorded but no income is on file for this period."
            )
        elif sv["status"] == "critical":
            main_risk_factors.append(f"Savings rate is only {sv['value']}%.")
        elif sv["status"] == "weak":
            main_risk_factors.append(f"Savings rate is {sv['value']}%, below a healthy margin.")
        elif sv["status"] == "strong":
            positive_factors.append(f"Savings rate is {sv['value']}%, a strong margin.")

    bg = components["budget"]
    if bg["status"] == "no_budget":
        main_risk_factors.append("No budget is set, so budget adherence can't be verified.")
    elif bg["status"] == "exceeded" and projected_budget_usage_pct is not None:
        over_amount = None
        if proj_expense is not None and budget is not None:
            over_amount = proj_expense - budget
        if over_amount is not None:
            main_risk_factors.append(f"Projected spending exceeds your budget by {_round(over_amount, 0)}.")
        else:
            main_risk_factors.append("Budget has been exceeded.")
    elif bg["status"] == "comfortable" and bg["value"] is not None:
        positive_factors.append(f"You are using only {bg['value']}% of your budget.")

    rc = components["recurring"]
    if rc["available"]:
        if rc["status"] in ("elevated", "high"):
            main_risk_factors.append(f"Recurring commitments consume {rc['value']}% of income.")
        elif rc["status"] == "healthy":
            positive_factors.append(f"Recurring commitments are below {int(rc['value'])}% of income." if rc['value'] < 20 else "Recurring commitments are within a healthy range of income.")

    gl = components["goals"]
    if gl["available"]:
        if gl["status"] == "at_risk":
            if total_required_monthly is not None and available_surplus is not None:
                main_risk_factors.append(
                    f"Your goals require {_round(total_required_monthly, 0)}/month but your available surplus is {_round(available_surplus, 0)}."
                )
            else:
                main_risk_factors.append("One or more goals require monthly contributions beyond your realistic surplus.")
        elif gl["status"] == "comfortable":
            positive_factors.append("Your savings goals are comfortably supported by your current surplus.")

    tr = components["trend"]
    if tr["available"]:
        if tr["status"] == "increasing":
            main_risk_factors.append(f"Spending has been trending up ({tr['value']}%).")
        elif tr["status"] == "decreasing":
            positive_factors.append(f"Spending has been trending down ({abs(tr['value'])}%).")

    if cash_flow_status == "deficit" and projected_surplus is not None:
        main_risk_factors.append(f"Projected expenses exceed income by {_round(abs(projected_surplus), 0)}.")

    components_out = {}
    for name, c in components.items():
        entry = {
            "score": _round(c["score"], 1) if c["score"] is not None else None,
            "weight": c["weight"],
            "value": c["value"],
            "unit": c["unit"],
            "status": c["status"],
        }
        # Pass through component-specific extras.
        for extra_key in ("goals_at_risk", "savings_basis", "current_value",
                          "projected_value", "score_basis", "pressure_basis"):
            if extra_key in c:
                entry[extra_key] = c[extra_key]
        components_out[name] = entry

    return {
        "status": "ok",
        "score": score,
        "risk_level": risk_level,
        "components": components_out,
        "main_risk_factors": main_risk_factors,
        "positive_factors": positive_factors,
        "cash_flow": {
            "current_savings_rate": components["savings"].get("current_value"),
            "projected_savings_rate": _round(projected_savings_rate, 1) if projected_savings_rate is not None else None,
            "projected_expense": _round(proj_expense, 2) if proj_expense is not None else None,
            "projected_expense_method": proj_method,
            "projected_expense_reliable": reliable_projected_expense is not None,
            "projected_surplus": _round(projected_surplus, 2) if projected_surplus is not None else None,
            "status": cash_flow_status,
        },
        "budget": {
            "budget_usage_pct": _round(budget_usage_pct, 1) if budget_usage_pct is not None else None,
            "projected_budget_usage_pct": _round(projected_budget_usage_pct, 1) if projected_budget_usage_pct is not None else None,
            "budget_breach": budget_breach,
            "budget_status": budget_status,
        },
        "runway": runway_out,
        "overrides_applied": overrides_applied,
        # Consolidated view for callers that want the headline figures in
        # one place without digging through components/cash_flow/budget.
        # These are references to values computed above, not new logic.
        "summary": {
            "current_savings_rate": components["savings"].get("current_value"),
            "projected_savings_rate": _round(projected_savings_rate, 1) if projected_savings_rate is not None else None,
            "projected_surplus": _round(projected_surplus, 2) if projected_surplus is not None else None,
            "current_budget_usage_pct": _round(budget_usage_pct, 1) if budget_usage_pct is not None else None,
            "projected_budget_usage_pct": _round(projected_budget_usage_pct, 1) if projected_budget_usage_pct is not None else None,
            "recurring_burden_pct": components["recurring"].get("value"),
            "goal_pressure": components["goals"].get("value"),
            "spending_trend_pct": components["trend"].get("value"),
        },
    }


# Alias, since the design doc allows either name.
calculate_financial_health_score = calculate_financial_health


# ---------------------------------------------------------------------------
# Legacy backward-compatible API
# ---------------------------------------------------------------------------

_LEGACY_RISK_MAP = {
    "healthy": "HEALTHY",
    "good": "GOOD",
    "moderate": "MODERATE",
    "elevated_risk": "ELEVATED",
    "high_risk": "HIGH",
    "insufficient_data": "UNKNOWN",
}


def predict_risk(income, expense, budget, days_passed=15):
    """Legacy API. Preserved for existing callers (e.g. decision_engine.py).

    Internally delegates to calculate_financial_health(). The returned
    "probability" is NOT a statistically calibrated probability - it is a
    deterministic severity percentage derived as (100 - health score).
    It is kept under this key only for backward compatibility with
    existing callers that expect it.
    """
    days_passed = _safe_num(days_passed)
    if days_passed is None or days_passed <= 0:
        days_passed = 1

    health = calculate_financial_health(
        income=income,
        current_expense=expense,
        budget=budget,
        days_passed=days_passed,
        days_in_month=30,
    )

    score = health["score"]
    risk_level = health["risk_level"]

    if score is None:
        # No usable data at all - treat conservatively rather than crash.
        probability = 100.0
    else:
        probability = max(0.0, 100.0 - score)

    projected_expense = health.get("cash_flow", {}).get("projected_expense")
    if projected_expense is None:
        # Absolute last resort fallback so this field is never missing.
        expense_safe = _safe_num(expense) or 0.0
        projected_expense = (expense_safe / days_passed) * 30

    return {
        "risk": _LEGACY_RISK_MAP.get(risk_level, "UNKNOWN"),
        "probability": round(probability, 1),
        "projected_expense": round(projected_expense, 2),
    }