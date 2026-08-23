# services/credit_risk_service.py

"""
FinTrust-side service wrapper for the verified AI Credit Risk System.

This module isolates the web layer from ML internals. It delegates
processing to the canonical ml.credit_risk modules without duplicating
validation, thresholds, or model-loading logic.
"""

from ml.credit_risk.assessment import assess_applicant
from ml.credit_risk.scenario import analyze_scenario
from ml.credit_risk.fairness_service import get_fairness_report


def assess_credit_risk(applicant):
    """
    Score a single applicant using the verified credit-risk model.
    """
    return assess_applicant(
        applicant,
        include_explanation=False,
        include_anomaly=False,
        include_fairness=False,
    )


def explain_credit_risk(applicant):
    """
    Produce a SHAP-based explanation for a single applicant.
    """
    result = assess_applicant(
        applicant,
        include_explanation=True,
        include_anomaly=False,
        include_fairness=False,
    )

    if result.get("status") == "error":
        return result

    explanation = result.get("explanation") or {}

    if explanation.get("available") is False:
        return {
            "status": "success",
            "explanation_available": False,
            "message": explanation.get(
                "message", "Explanation unavailable for this assessment."
            ),
        }

    return {
        "status": "success",
        "risk_probability": explanation.get("risk_probability", result.get("risk_probability")),
        "risk_percentage": explanation.get("risk_percentage", result.get("risk_percentage")),
        "risk_level": explanation.get("risk_level", result.get("risk_level")),
        "risk_increasing_factors": explanation.get("risk_increasing_factors", []),
        "risk_reducing_factors": explanation.get("risk_reducing_factors", []),
    }


def analyze_credit_scenario(applicant, changes):
    """
    Evaluates the risk impact of changing specific fields for an applicant.
    Delegates entirely to the canonical ml.credit_risk.scenario module.
    """
    return analyze_scenario(applicant, changes)


def get_responsible_ai_data():
    """
    Retrieves offline, dataset-level fairness metrics and hardcoded verified
    model performance metrics for the Responsible AI monitoring panel.
    Does not calculate metrics dynamically per request.
    """
    fairness = get_fairness_report()
    
    return {
        "status": "success",
        "model_performance": {
            "accuracy": 0.765,
            "roc_auc": 0.7931,
            "pr_auc": 0.6408,
            "brier_score": 0.1611,
            "log_loss": 0.4921
        },
        "fairness": fairness,
        "scope": "dataset_level",
        "applicant_fairness_score_available": False
    }


def check_credit_anomaly(applicant):
    """
    Checks if a single applicant is an anomaly against the offline, 
    population-fitted Isolation Forest model. 
    Does not fit a model during inference.
    """
    # Calling the canonical assessment function but only asking for the anomaly block
    result = assess_applicant(
        applicant,
        include_explanation=False,
        include_anomaly=True,
        include_fairness=False,
    )
    
    if result.get("status") == "error":
        return result
        
    anomaly_data = result.get("anomaly", {})
    
    if not anomaly_data or anomaly_data.get("available") is False:
        return {
            "status": "success",
            "available": False,
            "message": anomaly_data.get("message", "Anomaly detection unavailable.")
        }
        
    return {
        "status": "success",
        "available": True,
        "is_anomaly": anomaly_data.get("is_anomaly"),
        "anomaly_score": anomaly_data.get("anomaly_score"),
        "anomaly_level": anomaly_data.get("anomaly_level"),
        "manual_review": anomaly_data.get("manual_review")
    }