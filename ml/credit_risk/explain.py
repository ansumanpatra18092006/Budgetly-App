# ml/credit_risk/explain.py

import pandas as pd
import numpy as np
import shap

from .model_cache import get_preprocessor, get_model
from .explanation_mapper import generate_explanation
from .config import get_risk_level


# ============================================================
# EXTRACT UNDERLYING XGBOOST ESTIMATOR
# ============================================================

def _get_shap_compatible_estimator(model):
    """
    shap.TreeExplainer needs a concrete tree ensemble. The production
    model is a sklearn CalibratedClassifierCV, which wraps N cross-
    validated clones of the underlying XGBClassifier (one per CV fold)
    rather than a single fitted tree model — CalibratedClassifierCV
    itself is not something TreeExplainer can introspect directly.

    We use the first fold's underlying estimator. This is an
    approximation (the calibration layer reshapes the final
    probability, and each fold's booster differs slightly) but is
    the standard practical approach for explaining calibrated
    tree ensembles, and is far better than passing the calibrated
    wrapper directly to TreeExplainer, which does not support it.
    """

    if hasattr(model, "calibrated_classifiers_"):

        calibrated_classifier = model.calibrated_classifiers_[0]

        # sklearn >= 1.4 uses `.estimator`; older sklearn used
        # `.base_estimator`. Support both defensively.
        if hasattr(calibrated_classifier, "estimator"):
            return calibrated_classifier.estimator

        if hasattr(calibrated_classifier, "base_estimator"):
            return calibrated_classifier.base_estimator

        raise AttributeError(
            "CalibratedClassifierCV.calibrated_classifiers_[0] has "
            "neither 'estimator' nor 'base_estimator'. Check the "
            "installed scikit-learn version against requirements "
            "(scikit-learn==1.5.1)."
        )

    # Not calibrated — assume it's already a plain XGBClassifier.
    return model


# ============================================================
# GENERATE SHAP EXPLANATION
# ============================================================

def generate_shap_explanation(applicant, probability=None):
    """
    Generates a SHAP-based explanation for a single applicant using the
    PRODUCTION calibrated preprocessor + model (via model_cache, so
    repeated calls do not re-read the pickle files from disk).

    Parameters
    ----------
    applicant : dict
        Raw applicant fields (assumed already validated by
        validation.validate_applicant).

    probability : float, optional
        The canonical risk probability for this applicant, as already
        computed by the assessment layer. If not provided, it is
        recomputed here from the same production model, so the
        explanation is always consistent with the same artifacts used
        for the decision.
    """

    preprocessor = get_preprocessor()
    model = get_model()

    applicant_df = pd.DataFrame([applicant])

    processed = preprocessor.transform(applicant_df)

    if probability is None:
        probability = float(model.predict_proba(processed)[0][1])

    risk_level = get_risk_level(probability)

    feature_names = preprocessor.get_feature_names_out()

    xgb_estimator = _get_shap_compatible_estimator(model)

    explainer = shap.TreeExplainer(xgb_estimator)

    shap_values = explainer.shap_values(processed)

    # Handle both SHAP return conventions (list-per-class vs single array)
    if isinstance(shap_values, list):
        values = shap_values[-1][0]
    else:
        values = shap_values[0]

    explanation_df = pd.DataFrame({
        "feature": feature_names,
        "shap_value": values,
    })

    explanation_df["absolute_shap"] = explanation_df["shap_value"].abs()

    explanation_df = explanation_df.sort_values(
        "absolute_shap", ascending=False
    ).reset_index(drop=True)

    return generate_explanation(
        explanation_df,
        probability,
        risk_level,
    )