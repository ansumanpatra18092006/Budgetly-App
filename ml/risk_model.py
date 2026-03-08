import numpy as np
from sklearn.linear_model import LogisticRegression

def predict_risk(income, expense, budget, days_passed=15):
    if income == 0:
        return {"risk": "HIGH", "probability": 100}

    # Feature Engineering
    burn_rate = expense / max(days_passed, 1)
    projected_month_expense = burn_rate * 30
    savings_ratio = (income - expense) / income

    X = np.array([[projected_month_expense, savings_ratio, budget]])

    # Simple pretrained-like weights (simulated model)
    risk_score = projected_month_expense / max(income, 1)

    if risk_score > 1.2:
        risk = "HIGH"
    elif risk_score > 0.9:
        risk = "MEDIUM"
    else:
        risk = "LOW"

    probability = min(100, int(risk_score * 100))

    return {
        "risk": risk,
        "probability": probability,
        "projected_expense": round(projected_month_expense)
    }
