# services/financial_behavior_service.py

"""
Deterministic Financial Behavior Profile.

This module assesses an applicant's actual financial history recorded
in the Budgetly database to generate a transparent behavioral profile. 
It does not use machine learning and does not calculate a "credit score."

METHODOLOGY:
- Trend: Month-over-month percentage change (requires >= 2 months).
- Stability/Volatility: Coefficient of Variation (StdDev / Mean) or 
  Standard Deviation across all available months (requires >= 3 months).
"""

import math
from datetime import datetime
from utils.db import get_db
from services.recurring_service import analyze_recurring_transactions


def _calc_stats(values):
    """
    Returns (mean, standard_deviation, coefficient_of_variation).
    Using population standard deviation for descriptive history.
    """
    if not values:
        return 0.0, 0.0, 0.0
    mean = sum(values) / len(values)
    if len(values) < 2:
        return mean, 0.0, 0.0
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    std_dev = math.sqrt(variance)
    cov = (std_dev / mean) if mean > 0 else 0.0
    return mean, std_dev, cov


def get_financial_behavior_profile(user_id):
    """
    Builds a comprehensive financial behavior profile using multi-month 
    database aggregates to calculate genuine stability and volatility.
    """
    conn = get_db()
    try:
        # 1. Monthly Aggregates (Chronological)
        rows = conn.execute("""
            SELECT to_char(date, 'YYYY-MM') as month,
                   SUM(CASE WHEN type='income' THEN amount ELSE 0 END) as income,
                   SUM(CASE WHEN type='expense' THEN amount ELSE 0 END) as expense
            FROM transactions
            WHERE user_id=%s
            GROUP BY month
            ORDER BY month ASC
        """, (user_id,)).fetchall()

        # 2. Current Budget
        budget_row = conn.execute(
            "SELECT amount FROM budgets WHERE user_id=%s", (user_id,)
        ).fetchone()
        budget = float(budget_row["amount"]) if budget_row else 0.0

        # 3. Current Month Top Category
        current_month = datetime.today().strftime("%Y-%m")
        top_cat_row = conn.execute("""
            SELECT COALESCE(category, 'Misc') as category, SUM(amount) as total
            FROM transactions
            WHERE user_id=%s AND type='expense' AND to_char(date, 'YYYY-MM')=%s
            GROUP BY category
            ORDER BY total DESC LIMIT 1
        """, (user_id, current_month)).fetchone()
        
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Process Historical Data
    months_data = []
    for r in rows:
        months_data.append({
            "month": r["month"],
            "income": float(r["income"] or 0),
            "expense": float(r["expense"] or 0)
        })

    active_months = len(months_data)
    
    incomes = [m["income"] for m in months_data if m["income"] > 0]
    expenses = [m["expense"] for m in months_data if m["expense"] > 0]
    savings_rates = [
        ((m["income"] - m["expense"]) / m["income"] * 100) 
        for m in months_data if m["income"] > 0
    ]

    income_months = len(incomes)
    expense_months = len(expenses)
    savings_months = len(savings_rates)

    # Latest vs Previous for Trends
    current_income = months_data[-1]["income"] if active_months > 0 else 0.0
    current_expense = months_data[-1]["expense"] if active_months > 0 else 0.0
    prev_income = months_data[-2]["income"] if active_months > 1 else 0.0
    prev_expense = months_data[-2]["expense"] if active_months > 1 else 0.0
    
    current_surplus = current_income - current_expense
    current_savings_rate = ((current_surplus / current_income) * 100) if current_income > 0 else None

    # Calculate Trends (Requires >= 2 months)
    income_trend = round(((current_income - prev_income) / prev_income) * 100, 1) if prev_income > 0 else None
    expense_trend = round(((current_expense - prev_expense) / prev_expense) * 100, 1) if prev_expense > 0 else None

    # Calculate Real Stability/Volatility (Requires >= 3 months)
    inc_mean, inc_std, inc_cov = _calc_stats(incomes)
    exp_mean, exp_std, exp_cov = _calc_stats(expenses)
    sav_mean, sav_std, _ = _calc_stats(savings_rates)

    # THRESHOLDS: Coefficient of Variation (StdDev / Mean)
    # < 0.15 is generally considered low variance.
    # >= 0.30 indicates highly erratic values.
    if income_months >= 3:
        if inc_cov < 0.15: inc_stability = "Stable"
        elif inc_cov < 0.30: inc_stability = "Moderate variability"
        else: inc_stability = "Volatile"
    else:
        inc_stability = "Insufficient data"

    if expense_months >= 3:
        if exp_cov < 0.15: exp_volatility = "Low"
        elif exp_cov < 0.30: exp_volatility = "Moderate"
        else: exp_volatility = "High"
    else:
        exp_volatility = "Insufficient data"

    # THRESHOLDS: Savings Rate StdDev (Measured in raw percentage points)
    # A standard deviation of < 5% is very consistent saving.
    if savings_months >= 3:
        if sav_std < 5.0: sav_stability = "Consistent"
        elif sav_std < 15.0: sav_stability = "Moderate variation"
        else: sav_stability = "Highly variable"
    else:
        sav_stability = "Insufficient data"

    # 1. DATA COVERAGE
    data_coverage = {
        "history_months": active_months,
        "income_months": income_months,
        "spending_months": expense_months,
        "savings_months": savings_months,
        "transactions_available": active_months > 0,
        "income_available": income_months > 0
    }

    # Recurring Burden
    recurring_data_res = analyze_recurring_transactions(user_id)
    active_subs = [s for s in recurring_data_res.get("subscriptions", []) if s.get("lifecycle_status") == "active"]
    active_bills = [b for b in recurring_data_res.get("recurring_bills", []) if b.get("lifecycle_status") == "active"]
    monthly_burden = sum(s["monthly_equivalent"] for s in active_subs) + sum(b["monthly_equivalent"] for b in active_bills)
    
    data_coverage["recurring_data_available"] = monthly_burden > 0

    # 2. INCOME
    income_data = {
        "monthly_average": round(inc_mean, 2) if income_months > 0 else None,
        "stability": inc_stability,
        "trend": income_trend
    }

    # 3. SPENDING
    top_cat_name = top_cat_row["category"] if top_cat_row else None
    top_cat_total = float(top_cat_row["total"]) if top_cat_row else 0.0
    top_cat_share = round((top_cat_total / current_expense) * 100, 1) if top_cat_total > 0 and current_expense > 0 else None

    spending_data = {
        "monthly_average": round(exp_mean, 2) if expense_months > 0 else None,
        "volatility": exp_volatility,
        "trend": expense_trend,
        "top_category": top_cat_name,
        "top_category_share": top_cat_share
    }

    # 4. SAVINGS
    savings_data = {
        "savings_rate": round(current_savings_rate, 1) if current_savings_rate is not None else None,
        "stability": sav_stability
    }

    # 5. RECURRING
    burden_ratio = round((monthly_burden / current_income) * 100, 1) if current_income > 0 else None
    recurring_data = {
        "monthly_burden": round(monthly_burden, 2),
        "burden_ratio": burden_ratio
    }

    # 6. BUDGET
    budget_usage = round((current_expense / budget) * 100, 1) if budget > 0 else None
    budget_data = {
        "usage_percent": budget_usage,
        "adherence": "Good" if budget > 0 and budget_usage <= 100 else ("Poor" if budget > 0 else "Unknown")
    }

    # 7. CASH FLOW
    avg_surplus = inc_mean - exp_mean
    cash_flow_data = {
        "current_surplus": round(current_surplus, 2) if active_months > 0 else None,
        "projected_surplus": round(avg_surplus, 2) if active_months > 0 else None,
        "deficit_frequency": "Unknown" 
    }

    # 8. BEHAVIORAL FLAGS (Refined logically)
    flags = []
    
    if burden_ratio is not None and burden_ratio > 40:
        flags.append({
            "type": "burden",
            "severity": "high",
            "message": "High recurring burden",
            "evidence": f"Recurring commitments consume {burden_ratio}% of current recorded income."
        })
        
    if active_months > 0 and current_surplus < 0:
        flags.append({
            "type": "deficit",
            "severity": "high",
            "message": "Current cash-flow deficit",
            "evidence": f"Current month expenses exceed income by ₹{abs(int(current_surplus))}."
        })

    if expense_trend is not None and expense_trend > 30:
        flags.append({
            "type": "trend",
            "severity": "medium",
            "message": "Spending trend increasing",
            "evidence": f"Spending increased {expense_trend}% compared to the previous month."
        })
        
    if exp_cov >= 0.30 and expense_months >= 3:
        flags.append({
            "type": "volatility",
            "severity": "medium",
            "message": "High monthly spending variability",
            "evidence": f"Historical spending fluctuates significantly month-to-month."
        })
        
    if budget_usage is not None and budget_usage > 100:
        flags.append({
            "type": "budget",
            "severity": "medium",
            "message": "Budget overrun",
            "evidence": f"Current spending is {budget_usage}% of allocated budget."
        })
        
    if current_savings_rate is not None and current_savings_rate < 5:
        flags.append({
            "type": "savings",
            "severity": "medium",
            "message": "Low current savings rate",
            "evidence": f"Current month savings rate is at {round(current_savings_rate, 1)}%.",
        })

    # Summary Generation
    summary_text = "Insufficient Data"
    if active_months < 3:
        summary_text = "Limited financial history"
    elif any(f["severity"] == "high" for f in flags):
        summary_text = "High financial pressure"
    elif any(f["severity"] == "medium" for f in flags):
        summary_text = "Moderate financial pressure"
    else:
        summary_text = "Healthy financial behavior"

    return {
        "status": "success",
        "data_coverage": data_coverage,
        "income": income_data,
        "spending": spending_data,
        "savings": savings_data,
        "recurring": recurring_data,
        "budget": budget_data,
        "cash_flow": cash_flow_data,
        "behavioral_flags": flags,
        "summary": summary_text
    }