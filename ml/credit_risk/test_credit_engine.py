# ml/credit_risk/test_credit_engine.py

"""
Executable tests for PHASE 0.5 reconciliation, items A-O.

NOTE ON TEST ARTIFACTS:
These tests run against models/calibrated_preprocessor.pkl and
models/calibrated_xgboost.pkl. In THIS environment those are
SYNTHETIC fixtures (fit on a randomly generated dataset with the
correct schema) because the real production artifact
"calibrated_xgboost.pkl" (the CalibratedClassifierCV, ~465KB per the
provided screenshot) was not present in the uploaded files — only a
plain, uncalibrated XGBClassifier named "xgboost_model.pkl" (~440KB)
was provided. See the final report for details.

These tests verify the CODE PATHS are correct (validation, threshold
policy, caching, no train-on-single-applicant, scenario/explanation
consistency). They do not, and cannot, verify the real model's
real-world accuracy — that is independently confirmed against the
actual calibrated_test_predictions.csv / calibration_holdout_results.csv
you supplied (see final report).
"""

import copy
import pandas as pd
import pytest

from . import model_cache
from .assessment import assess_applicant
from .validation import validate_applicant, ValidationError
from .config import get_risk_level, get_decision
from .scenario import analyze_scenario
from .explain import generate_shap_explanation
from .anomaly_inference import score_anomaly
from .fairness_service import get_fairness_report


VALID_APPLICANT = {
    "checking_account": "A11",
    "duration_months": 6,
    "credit_history": "A34",
    "purpose": "A43",
    "credit_amount": 1169,
    "savings_account": "A65",
    "employment_since": "A75",
    "installment_rate": 4,
    "personal_status_sex": "A93",
    "other_debtors": "A101",
    "residence_since": 4,
    "property": "A121",
    "age": 45,
    "other_installment_plans": "A143",
    "housing": "A152",
    "existing_credits": 2,
    "job": "A173",
    "dependents": 1,
    "telephone": "A192",
    "foreign_worker": "A201",
}


@pytest.fixture(autouse=True)
def clear_cache():
    model_cache.reset_cache()
    yield
    model_cache.reset_cache()


# ============================================================
# A. Valid German Credit example applicant
# ============================================================

def test_a_valid_applicant_end_to_end():
    result = assess_applicant(VALID_APPLICANT)
    assert result["status"] == "success"
    assert 0.0 <= result["risk_probability"] <= 1.0
    assert result["risk_level"] in ("LOW RISK", "MEDIUM RISK", "HIGH RISK")
    assert result["decision"] in ("APPROVE", "MANUAL REVIEW", "REJECT")


# ============================================================
# B. Missing feature
# ============================================================

def test_b_missing_feature_rejected():
    applicant = copy.deepcopy(VALID_APPLICANT)
    del applicant["duration_months"]

    with pytest.raises(ValidationError) as exc_info:
        validate_applicant(applicant)
    assert any("duration_months" in e for e in exc_info.value.errors)

    result = assess_applicant(applicant)
    assert result["status"] == "error"
    assert result["error_type"] == "validation_error"


# ============================================================
# C. Invalid numeric type
# ============================================================

def test_c_invalid_numeric_type_rejected():
    applicant = copy.deepcopy(VALID_APPLICANT)
    applicant["duration_months"] = "twelve"

    with pytest.raises(ValidationError):
        validate_applicant(applicant)

    result = assess_applicant(applicant)
    assert result["status"] == "error"


# ============================================================
# D. Invalid categorical value
# ============================================================

def test_d_invalid_categorical_value_rejected():
    applicant = copy.deepcopy(VALID_APPLICANT)
    applicant["checking_account"] = "A99"  # not a real UCI code

    with pytest.raises(ValidationError) as exc_info:
        validate_applicant(applicant)
    assert any("checking_account" in e for e in exc_info.value.errors)

    result = assess_applicant(applicant)
    assert result["status"] == "error"


# ============================================================
# E-I. Canonical threshold boundaries
# ============================================================

@pytest.mark.parametrize("probability,expected_level,expected_decision", [
    (0.24, "LOW RISK", "APPROVE"),        # E
    (0.25, "MEDIUM RISK", "MANUAL REVIEW"),  # F
    (0.39, "MEDIUM RISK", "MANUAL REVIEW"),  # G
    (0.40, "HIGH RISK", "REJECT"),        # H
    (0.60, "HIGH RISK", "REJECT"),        # I
])
def test_e_to_i_threshold_boundaries(probability, expected_level, expected_decision):
    assert get_risk_level(probability) == expected_level
    assert get_decision(probability) == expected_decision


# ============================================================
# J. Scenario uses the same calibrated artifacts as baseline
# ============================================================

def test_j_scenario_uses_production_artifacts_not_a_new_model():
    import inspect
    from . import scenario as scenario_module

    source = inspect.getsource(scenario_module)
    assert "XGBClassifier(" not in source, (
        "scenario.py must not construct/train a new model"
    )
    assert "fit(" not in source, (
        "scenario.py must not fit any model"
    )
    assert "get_preprocessor" in source and "get_model" in source, (
        "scenario.py must use the cached production artifacts"
    )

    # Functional check: baseline probability from analyze_scenario
    # matches a direct prediction using the same cached model.
    from .scenario import predict_risk

    result = analyze_scenario(VALID_APPLICANT, {"duration_months": 24})
    direct_baseline = predict_risk(VALID_APPLICANT)

    assert result["baseline_probability"] == pytest.approx(direct_baseline)
    assert result["baseline_risk_level"] == get_risk_level(direct_baseline)
    assert result["baseline_decision"] == get_decision(direct_baseline)
    assert result["scenario_risk_level"] == get_risk_level(result["scenario_probability"])


# ============================================================
# K. Explanation uses canonical risk level
# ============================================================

def test_k_explanation_uses_canonical_risk_level():
    from .assessment import _predict_probability

    probability, _ = _predict_probability(VALID_APPLICANT)
    explanation = generate_shap_explanation(VALID_APPLICANT, probability=probability)

    assert explanation["risk_level"] == get_risk_level(probability)


def test_k2_explanation_mapper_has_no_independent_thresholds():
    import inspect
    from . import explanation_mapper

    source = inspect.getsource(explanation_mapper.generate_explanation)
    # The old bug: "if probability < 0.30" / "< 0.45" thresholds inside
    # generate_explanation. Confirm they're gone.
    assert "0.30" not in source
    assert "0.45" not in source
    assert "risk_level" in inspect.signature(
        explanation_mapper.generate_explanation
    ).parameters


# ============================================================
# L. Anomaly inference does NOT fit on a single applicant
# ============================================================

def test_l_anomaly_does_not_fit_on_single_applicant():
    import inspect
    from . import anomaly_inference

    source = inspect.getsource(anomaly_inference)
    assert ".fit(" not in source, (
        "anomaly_inference.py must never call .fit() — it only loads "
        "a pre-fitted population model"
    )

    preprocessor = model_cache.get_preprocessor()
    applicant_df = pd.DataFrame([VALID_APPLICANT])
    processed = preprocessor.transform(applicant_df)

    result = score_anomaly(processed)
    assert result["anomaly_level"] in (
        "NORMAL", "LOW ANOMALY", "MEDIUM ANOMALY", "HIGH ANOMALY", "UNAVAILABLE"
    )

    # Calling twice must reuse the same cached model instance
    # (proof no refit happens between calls).
    model_before = model_cache.get_anomaly_model()
    score_anomaly(processed)
    model_after = model_cache.get_anomaly_model()
    assert model_before is model_after


def test_l2_anomaly_unavailable_state_when_no_artifact():
    # Point the cache at a model directory with no anomaly_model.pkl
    original_path = model_cache.ANOMALY_MODEL_PATH
    model_cache.ANOMALY_MODEL_PATH = "/tmp/does_not_exist_anomaly_model.pkl"
    try:
        model_cache.reset_cache()
        preprocessor = model_cache.get_preprocessor()
        processed = preprocessor.transform(pd.DataFrame([VALID_APPLICANT]))
        result = score_anomaly(processed)
        assert result["available"] is False
        assert result["anomaly_level"] == "UNAVAILABLE"
        assert "message" in result
    finally:
        model_cache.ANOMALY_MODEL_PATH = original_path
        model_cache.reset_cache()


# ============================================================
# M. Calibration CSV loads successfully
# ============================================================

def test_m_calibration_csv_loads():
    df = pd.read_csv("calibration_holdout_results.csv")
    assert set(["model", "accuracy", "precision", "recall", "f1",
                "roc_auc", "pr_auc", "brier", "log_loss"]).issubset(df.columns)
    assert len(df) == 2  # Uncalibrated + Calibrated rows

    df2 = pd.read_csv("calibrated_test_predictions.csv")
    assert set(["actual", "uncalibrated_probability",
                "calibrated_probability"]).issubset(df2.columns)
    assert len(df2) > 0


# ============================================================
# N. Fairness CSV loads successfully
# ============================================================

def test_n_fairness_csv_loads():
    report = get_fairness_report()
    assert report["available"] is True
    assert report["scope"] == "dataset-level (offline)"
    assert len(report["records"]) > 0
    attrs = set(report["attributes_evaluated"])
    assert {"personal_status_sex", "age_group", "foreign_worker"}.issubset(attrs)


# ============================================================
# O. Multiple calls reuse cached model artifacts
# ============================================================

def test_o_model_caching_reuses_artifacts():
    status_before = model_cache.cache_status()
    assert status_before["preprocessor_loaded"] is False
    assert status_before["model_loaded"] is False

    pre1 = model_cache.get_preprocessor()
    model1 = model_cache.get_model()

    pre2 = model_cache.get_preprocessor()
    model2 = model_cache.get_model()

    assert pre1 is pre2, "preprocessor must be cached, not reloaded"
    assert model1 is model2, "model must be cached, not reloaded"

    status_after = model_cache.cache_status()
    assert status_after["preprocessor_loaded"] is True
    assert status_after["model_loaded"] is True