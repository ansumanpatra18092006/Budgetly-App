# ml/credit_risk/scenario.py

"""
"What-if" scenario analysis for a single applicant.

PHASE 0.5 FIX:
The previous version of this module trained its own separate XGBoost
model from scratch (uncalibrated, uncalibrated thresholds, potentially
different train/test split) purely to answer "what if we change one
field". That meant the scenario tool could disagree with the actual
production assessment for the SAME applicant, which is not acceptable
for a lending decision tool.

This version:
  - loads the exact same calibrated_preprocessor.pkl / calibrated_xgboost.pkl
    used by the production assessment path (via model_cache, so no
    re-training and no repeated disk reads)
  - uses config.py for both the baseline and scenario risk level/decision
  - returns a normalized structure only
"""

import pandas as pd

from .model_cache import get_preprocessor, get_model
from .config import get_risk_level, get_decision
from .validation import validate_applicant, ALL_FEATURES


# ============================================================
# PREDICT RISK (PRODUCTION ARTIFACTS ONLY)
# ============================================================

def predict_risk(applicant, preprocessor=None, model=None):
    """
    Scores one applicant using the production calibrated pipeline.
    If preprocessor/model are not supplied, the cached singletons
    are used (recommended — avoids repeated disk reads).
    """

    preprocessor = preprocessor or get_preprocessor()
    model = model or get_model()

    applicant_df = pd.DataFrame([applicant])

    processed = preprocessor.transform(applicant_df)

    probability = float(model.predict_proba(processed)[0][1])

    return probability


# ============================================================
# SCENARIO ANALYSIS
# ============================================================

def analyze_scenario(applicant, changes, preprocessor=None, model=None):
    """
    Compares an applicant's current (baseline) risk against a
    hypothetical scenario where `changes` are applied to one or more
    fields.

    Parameters
    ----------
    applicant : dict
        The applicant's current, full, valid feature set.
    changes : dict
        {feature_name: new_value} — must be a subset of the known
        applicant fields.

    Returns
    -------
    dict with the normalized structure:
        {
            "baseline_probability": ...,
            "scenario_probability": ...,
            "delta": ...,
            "baseline_risk_level": ...,
            "scenario_risk_level": ...,
            "baseline_decision": ...,
            "scenario_decision": ...,
            "interpretation": ...,
            "changes": {...},
        }
    """

    # --------------------------------------------------------
    # VALIDATE INPUTS
    # --------------------------------------------------------

    validate_applicant(applicant)

    unknown_change_fields = [f for f in changes if f not in ALL_FEATURES]

    if unknown_change_fields:
        raise ValueError(
            "Unknown scenario feature(s): "
            + ", ".join(unknown_change_fields)
        )

    preprocessor = preprocessor or get_preprocessor()
    model = model or get_model()

    # --------------------------------------------------------
    # BASELINE (uses canonical config.py policy)
    # --------------------------------------------------------

    baseline_probability = predict_risk(applicant, preprocessor, model)
    baseline_risk_level = get_risk_level(baseline_probability)
    baseline_decision = get_decision(baseline_probability)

    # --------------------------------------------------------
    # SCENARIO
    # --------------------------------------------------------

    scenario_applicant = applicant.copy()
    scenario_applicant.update(changes)

    # Re-validate the modified applicant so a scenario can't smuggle
    # in an invalid value (e.g. a slider bug sending duration=-5).
    validate_applicant(scenario_applicant)

    scenario_probability = predict_risk(scenario_applicant, preprocessor, model)
    scenario_risk_level = get_risk_level(scenario_probability)
    scenario_decision = get_decision(scenario_probability)

    # --------------------------------------------------------
    # DELTA / INTERPRETATION
    # --------------------------------------------------------

    delta = scenario_probability - baseline_probability

    if delta < -0.02:
        interpretation = "Scenario improves model-estimated risk."
    elif delta > 0.02:
        interpretation = "Scenario increases model-estimated risk."
    else:
        interpretation = "Scenario has a small change in model-estimated risk."

    return {
        "baseline_probability": baseline_probability,
        "scenario_probability": scenario_probability,
        "delta": delta,
        "baseline_risk_level": baseline_risk_level,
        "scenario_risk_level": scenario_risk_level,
        "baseline_decision": baseline_decision,
        "scenario_decision": scenario_decision,
        "interpretation": interpretation,
        "changes": changes,
    }


# ============================================================
# PRINT RESULT (CLI / DEBUG HELPER)
# ============================================================

def print_scenario_result(result):

    print("\n" + "=" * 80)
    print("CREDIT RISK SCENARIO ANALYSIS (production artifacts)")
    print("=" * 80)

    print(
        f"\nBaseline probability : "
        f"{result['baseline_probability'] * 100:.2f}%  "
        f"({result['baseline_risk_level']} -> {result['baseline_decision']})"
    )

    print("\nScenario changes:")
    for feature, value in result["changes"].items():
        print(f"  {feature}: {value}")

    print(
        f"\nScenario probability : "
        f"{result['scenario_probability'] * 100:.2f}%  "
        f"({result['scenario_risk_level']} -> {result['scenario_decision']})"
    )

    change_pp = result["delta"] * 100

    if change_pp < 0:
        print(f"\nRisk decreased by {abs(change_pp):.2f} percentage points")
    elif change_pp > 0:
        print(f"\nRisk increased by {change_pp:.2f} percentage points")
    else:
        print("\nNo meaningful change in risk.")

    print(f"\n{result['interpretation']}")
    print(
        "\nNOTE: Scenario results are model-estimated outcomes and are "
        "not guaranteed financial outcomes."
    )
    print("=" * 80)


# ============================================================
# MAIN (manual smoke test — requires production artifacts present)
# ============================================================

if __name__ == "__main__":

    example_applicant = {
        "checking_account": "A11",
        "duration_months": 48,
        "credit_history": "A34",
        "purpose": "A43",
        "credit_amount": 5000,
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

    result = analyze_scenario(
        example_applicant,
        {"duration_months": 24},
    )

    print_scenario_result(result)