# ml/credit_risk/final_assessment.py

"""
CLI entry point for a full responsible-lending assessment
(risk + explanation + anomaly + fairness metadata).

PHASE 0.5 FIX: this module previously:
  1. defined its own LOW_RISK_THRESHOLD/HIGH_RISK_THRESHOLD (duplicating
     config.py),
  2. fit a brand-new IsolationForest on the single incoming applicant at
     request time (statistically invalid — see anomaly_inference.py),
  3. reloaded the pickled model/preprocessor on every call.

It now delegates entirely to assessment.assess_applicant(), which is
the single canonical path for all of the above.
"""

from assessment import assess_applicant


def get_example_applicant():

    return {
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
        "age": 67,
        "other_installment_plans": "A143",
        "housing": "A152",
        "existing_credits": 2,
        "job": "A173",
        "dependents": 1,
        "telephone": "A192",
        "foreign_worker": "A201",
    }


if __name__ == "__main__":

    print("\n" + "=" * 90)
    print("FINAL RESPONSIBLE LENDING ASSESSMENT")
    print("=" * 90)

    applicant = get_example_applicant()

    print("\nRunning complete ML assessment...")

    result = assess_applicant(
        applicant,
        include_explanation=True,
        include_anomaly=True,
        include_fairness=True,
    )

    if result["status"] != "success":
        print("\nValidation failed:")
        for err in result["errors"]:
            print(f"  - {err}")
        raise SystemExit(1)

    print("\n" + "=" * 90)
    print("CREDIT RISK ASSESSMENT")
    print("=" * 90)
    print(f"\nRisk probability : {result['risk_percentage']:.2f}%")
    print(f"Risk level       : {result['risk_level']}")
    print(f"Decision         : {result['decision']}")

    explanation = result.get("explanation", {})
    print("\n" + "=" * 90)
    print("MODEL EXPLANATION")
    print("=" * 90)
    print(f"\nRisk level: {explanation.get('risk_level', result['risk_level'])}")
    print("\nRisk-increasing factors:")
    for factor in explanation.get("risk_increasing_factors", []):
        print(f"  ↑ {factor['feature']} ({factor['impact']:.3f})")
    print("\nRisk-reducing factors:")
    for factor in explanation.get("risk_reducing_factors", []):
        print(f"  ↓ {factor['feature']} ({factor['impact']:.3f})")

    anomaly = result.get("anomaly", {})
    print("\n" + "=" * 90)
    print("ANOMALY ASSESSMENT")
    print("=" * 90)
    print(f"\nAvailable        : {anomaly.get('available')}")
    print(f"Anomaly score    : {anomaly.get('anomaly_score')}")
    print(f"Anomaly level    : {anomaly.get('anomaly_level')}")
    print(f"Manual review    : {anomaly.get('manual_review')}")
    if anomaly.get("message"):
        print(f"Note             : {anomaly['message']}")

    fairness = result.get("fairness", {})
    print("\n" + "=" * 90)
    print("FAIRNESS STATUS (dataset-level, offline)")
    print("=" * 90)
    print(f"\nFairness analysis available: {fairness.get('available')}")
    print(fairness.get("message", ""))

    print("\n" + "=" * 90)
    print("FINAL ML ASSESSMENT COMPLETED")
    print("=" * 90)
    print("\nStatus:", result["status"])