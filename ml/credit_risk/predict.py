# ml/credit_risk/predict.py

"""
Thin CLI/legacy wrapper around the canonical assessment path.

PHASE 0.5 FIX: this module used to define its OWN get_risk_level /
get_decision thresholds (0.30 / 0.50) which disagreed with config.py's
canonical thresholds (0.25 / 0.40). It now delegates entirely to
assessment.assess_applicant(), so there is exactly one place that
implements the risk policy.
"""

from .assessment import assess_applicant


def predict_applicant(applicant, **kwargs):
    """Kept for backwards compatibility with old call sites."""
    return assess_applicant(applicant, **kwargs)


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

    print("\n" + "=" * 80)
    print("CALIBRATED CREDIT RISK ENGINE")
    print("=" * 80)

    applicant = get_example_applicant()

    result = predict_applicant(applicant)

    if result["status"] != "success":
        print("\nValidation failed:")
        for err in result["errors"]:
            print(f"  - {err}")
    else:
        print(f"\nRisk probability : {result['risk_percentage']:.2f}%")
        print(f"Risk level       : {result['risk_level']}")
        print(f"Decision         : {result['decision']}")

    print("\nPrediction completed.")