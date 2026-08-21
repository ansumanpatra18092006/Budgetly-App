# ml/credit_risk/assessment.py

"""
THE canonical credit-risk assessment entry point.

Every other module that needs to produce a risk decision (predict.py,
risk_engine.py, any future API route) should call assess_applicant()
here rather than re-implementing validation / prediction / threshold
logic. This is the "one place" required by PHASE 0.5 §10.

Design:
  - validation.py            -> strict input validation
  - model_cache.py           -> cached calibrated preprocessor + model
  - config.py                -> the ONLY source of risk thresholds/decisions
  - explain.py                -> optional SHAP explanation (reuses config's risk level)
  - scenario.py               -> optional what-if analysis (reuses production artifacts)
  - anomaly_inference.py      -> optional population-based anomaly score
  - fairness_service.py       -> optional static dataset-level fairness metadata
"""

import pandas as pd

from .validation import validate_applicant, ValidationError
from .model_cache import get_preprocessor, get_model
from .config import get_risk_level, get_decision


# ============================================================
# CORE PREDICTION
# ============================================================

def _predict_probability(applicant):

    preprocessor = get_preprocessor()
    model = get_model()

    applicant_df = pd.DataFrame([applicant])

    processed = preprocessor.transform(applicant_df)

    probability = float(model.predict_proba(processed)[0][1])

    return probability, processed


# ============================================================
# CANONICAL ASSESSMENT
# ============================================================

def assess_applicant(
    applicant,
    include_explanation=False,
    include_anomaly=False,
    include_fairness=False,
):
    """
    The single canonical entry point for scoring an applicant.

    Returns (on success):
        {
            "status": "success",
            "risk_probability": float,
            "risk_percentage": float,
            "risk_level": "LOW RISK" | "MEDIUM RISK" | "HIGH RISK",
            "decision": "APPROVE" | "MANUAL REVIEW" | "REJECT",
            # + optional blocks if requested:
            "explanation": {...},
            "anomaly": {...},
            "fairness": {...},
        }

    Returns (on validation failure):
        {
            "status": "error",
            "error_type": "validation_error",
            "errors": [...],
        }
    """

    # --------------------------------------------------------
    # VALIDATE
    # --------------------------------------------------------

    try:
        validate_applicant(applicant)
    except ValidationError as error:
        return {
            "status": "error",
            "error_type": "validation_error",
            "errors": error.errors,
        }

    # --------------------------------------------------------
    # PREDICT + CANONICAL POLICY (config.py is the only source
    # of thresholds/decisions — nothing else in this function
    # duplicates that logic)
    # --------------------------------------------------------

    probability, processed = _predict_probability(applicant)

    risk_level = get_risk_level(probability)
    decision = get_decision(probability)

    result = {
        "status": "success",
        "risk_probability": probability,
        "risk_percentage": probability * 100,
        "risk_level": risk_level,
        "decision": decision,
    }

    # --------------------------------------------------------
    # OPTIONAL: EXPLANATION
    # --------------------------------------------------------

    if include_explanation:
        from .explain import generate_shap_explanation

        try:
            result["explanation"] = generate_shap_explanation(
                applicant, probability=probability
            )
        except Exception as error:
            result["explanation"] = {
                "available": False,
                "message": f"Explanation generation failed: {error}",
            }

    # --------------------------------------------------------
    # OPTIONAL: ANOMALY (population-fitted model only)
    # --------------------------------------------------------

    if include_anomaly:
        from .anomaly_inference import score_anomaly

        result["anomaly"] = score_anomaly(processed)

    # --------------------------------------------------------
    # OPTIONAL: FAIRNESS (static, dataset-level metadata only)
    # --------------------------------------------------------

    if include_fairness:
        from .fairness_service import get_fairness_report

        result["fairness"] = get_fairness_report()

    return result