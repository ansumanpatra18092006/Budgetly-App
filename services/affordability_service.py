# services/affordability_service.py

"""
Affordability Assessment Service.

Evaluates whether an applicant can reasonably afford a proposed loan 
based on their actual Budgetly financial behavior.

This is a deterministic cash-flow calculation, NOT a credit score.
"""

from services.financial_behavior_service import get_financial_behavior_profile

def calculate_affordability(user_id, applicant):
    """
    Combines the applicant's proposed loan parameters with their 
    actual Budgetly financial capacity to determine affordability.
    """
    # 1. Fetch behavioral profile
    behavior = get_financial_behavior_profile(user_id)
    coverage = behavior.get("data_coverage", {})
    
    # 2. Extract applicant inputs
    try:
        credit_amount = float(applicant.get("credit_amount", 0))
        duration_months = float(applicant.get("duration_months", 0))
    except (ValueError, TypeError):
        credit_amount = 0.0
        duration_months = 0.0
        
    # 3. Data sufficiency check
    history_months = coverage.get("history_months", 0)
    income_avail = coverage.get("income_available", False)
    expense_avail = coverage.get("spending_months", 0) > 0

    # Extract historical values
    income_data = behavior.get("income", {})
    spending_data = behavior.get("spending", {})
    recurring_data = behavior.get("recurring", {})
    cash_flow_data = behavior.get("cash_flow", {})

    monthly_income = income_data.get("monthly_average")
    monthly_expenses = spending_data.get("monthly_average")
    recurring_burden = recurring_data.get("monthly_burden")
    current_surplus = cash_flow_data.get("current_surplus")
    projected_surplus = cash_flow_data.get("projected_surplus")

    # 4. Handle Insufficient Data
    # Be conservative: Require at least 2 months of history and both income & expenses
    if not (income_avail and expense_avail and history_months >= 2):
        return {
            "status": "success",
            "data_coverage": {
                "history_months": history_months,
                "income_available": income_avail,
                "expense_data_available": expense_avail
            },
            "financial_capacity": {
                "monthly_income": monthly_income,
                "monthly_expenses": monthly_expenses,
                "recurring_burden": recurring_burden,
                "current_surplus": current_surplus,
                "projected_surplus": projected_surplus
            },
            "loan": {
                "credit_amount": credit_amount,
                "duration_months": duration_months,
                "estimated_monthly_payment": None,
                "calculation_method": "principal divided by tenure; interest rate unavailable"
            },
            "affordability": {
                "available_surplus": None,
                "payment_to_income_ratio": None,
                "payment_to_surplus_ratio": None,
                "status": "insufficient_data",
                "reason": "Insufficient financial history to accurately assess affordability."
            }
        }

    # 5. Proposed Payment (Deterministic Estimate)
    # The application schema lacks an explicit interest rate, so we approximate linearly
    estimated_payment = round(credit_amount / duration_months, 2) if duration_months > 0 else 0.0
    
    # 6. Affordability Logic
    avail_surplus = projected_surplus if projected_surplus is not None else 0.0
    
    pti = round((estimated_payment / monthly_income) * 100, 1) if monthly_income else None
    pts = round((estimated_payment / avail_surplus) * 100, 1) if avail_surplus > 0 else None

    # Thresholds:
    # UNAFFORDABLE: payment > projected_surplus (or surplus <= 0)
    # STRAINED: payment > 60% of projected_surplus
    # AFFORDABLE: payment <= 60% of projected_surplus
    
    if avail_surplus <= 0:
        status = "unaffordable"
        reason = "No available monthly surplus to support new loan payments."
    elif estimated_payment > avail_surplus:
        status = "unaffordable"
        reason = "Estimated monthly payment exceeds available monthly surplus."
    elif estimated_payment > (avail_surplus * 0.6):
        status = "strained"
        reason = "Estimated payment consumes a high portion of available surplus."
    else:
        status = "affordable"
        reason = "Estimated payment is comfortably covered by available surplus."

    return {
        "status": "success",
        "data_coverage": {
            "history_months": history_months,
            "income_available": income_avail,
            "expense_data_available": expense_avail
        },
        "financial_capacity": {
            "monthly_income": monthly_income,
            "monthly_expenses": monthly_expenses,
            "recurring_burden": recurring_burden,
            "current_surplus": current_surplus,
            "projected_surplus": projected_surplus
        },
        "loan": {
            "credit_amount": credit_amount,
            "duration_months": duration_months,
            "estimated_monthly_payment": estimated_payment,
            "calculation_method": "principal divided by tenure; interest rate unavailable"
        },
        "affordability": {
            "available_surplus": avail_surplus,
            "payment_to_income_ratio": pti,
            "payment_to_surplus_ratio": pts,
            "status": status,
            "reason": reason
        }
    }