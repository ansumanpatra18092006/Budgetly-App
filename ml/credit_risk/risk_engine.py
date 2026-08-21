# ml/credit_risk/risk_engine.py

"""
Thin CLI/legacy wrapper around the canonical assessment path.

PHASE 0.5 FIX: this module previously reloaded the pickled preprocessor
and model on every call to load_models(), and duplicated validation
logic already present elsewhere. It now delegates entirely to
assessment.assess_applicant(), which uses model_cache (load-once) and
validation.py (single validation implementation).
"""

from .assessment import assess_applicant


def assess(applicant, **kwargs):
    """Kept for backwards compatibility with old call sites."""
    return assess_applicant(applicant, **kwargs)


if __name__ == "__main__":

    print("\n" + "=" * 80)
    print("RESPONSIBLE LENDING RISK ENGINE")
    print("=" * 80)

    applicant = {
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

    result = assess(applicant)

    if result["status"] != "success":
        print("\nValidation failed:")
        for err in result["errors"]:
            print(f"  - {err}")
    else:
        print(f"\nRisk probability : {result['risk_percentage']:.2f}%")
        print(f"Risk level       : {result['risk_level']}")
        print(f"Decision         : {result['decision']}")