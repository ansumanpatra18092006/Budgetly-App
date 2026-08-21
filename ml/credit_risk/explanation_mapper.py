# explanation_mapper.py

"""
Converts encoded German Credit Dataset feature names
into human-readable explanations.

Important:
SHAP values represent model contribution, not causal effects.
Therefore, the generated text refers to factors that
"increased" or "reduced" the model-estimated risk.
"""


# ============================================================
# CATEGORICAL FEATURE DESCRIPTIONS
# ============================================================

CATEGORY_DESCRIPTIONS = {

    # --------------------------------------------------------
    # Checking Account
    # --------------------------------------------------------

    "checking_account_A11":
        "Low balance checking account",

    "checking_account_A12":
        "Moderate balance checking account",

    "checking_account_A13":
        "High balance checking account",

    "checking_account_A14":
        "No checking account information",

    # --------------------------------------------------------
    # Credit History
    # --------------------------------------------------------

    "credit_history_A30":
        "No previous credit history",

    "credit_history_A31":
        "All previous credits paid back duly",

    "credit_history_A32":
        "Existing credits paid back duly",

    "credit_history_A33":
        "Existing credit with delays",

    "credit_history_A34":
        "Critical or problematic credit history",

    # --------------------------------------------------------
    # Savings Account
    # --------------------------------------------------------

    "savings_account_A61":
        "Low or no savings",

    "savings_account_A62":
        "Moderate savings",

    "savings_account_A63":
        "Higher savings",

    "savings_account_A64":
        "Substantial savings",

    "savings_account_A65":
        "Very high savings",

    # --------------------------------------------------------
    # Employment
    # --------------------------------------------------------

    "employment_since_A71":
        "Very short or unstable employment",

    "employment_since_A72":
        "Short employment history",

    "employment_since_A73":
        "Moderate employment history",

    "employment_since_A74":
        "Long employment history",

    "employment_since_A75":
        "Very long employment history",

    # --------------------------------------------------------
    # Housing
    # --------------------------------------------------------

    "housing_A151":
        "Renting housing",

    "housing_A152":
        "Owns housing",

    "housing_A153":
        "Free housing arrangement",

    # --------------------------------------------------------
    # Property
    # --------------------------------------------------------

    "property_A121":
        "Real estate ownership",

    "property_A122":
        "Building society savings or insurance",

    "property_A123":
        "Car or other valuable property",

    "property_A124":
        "No known property",

    # --------------------------------------------------------
    # Other Installment Plans
    # --------------------------------------------------------

    "other_installment_plans_A141":
        "Bank installment plan",

    "other_installment_plans_A142":
        "Store installment plan",

    "other_installment_plans_A143":
        "No other installment plans",

    # --------------------------------------------------------
    # Purpose
    # --------------------------------------------------------

    "purpose_A40":
        "New car purchase",

    "purpose_A41":
        "Used car purchase",

    "purpose_A42":
        "Furniture or equipment",

    "purpose_A43":
        "Radio or television",

    "purpose_A44":
        "Domestic appliances",

    "purpose_A45":
        "Repairs",

    "purpose_A46":
        "Education",

    "purpose_A47":
        "Vacation",

    "purpose_A48":
        "Retraining",

    "purpose_A49":
        "Business purpose",

    "purpose_A410":
        "Other purpose",

    # --------------------------------------------------------
    # Other Debtors
    # --------------------------------------------------------

    "other_debtors_A101":
        "No other debtors or guarantors",

    "other_debtors_A102":
        "Co-applicant",

    "other_debtors_A103":
        "Guarantor",

    # --------------------------------------------------------
    # Telephone
    # --------------------------------------------------------

    "telephone_A191":
        "No registered telephone",

    "telephone_A192":
        "Registered telephone",

    # --------------------------------------------------------
    # Foreign Worker
    # --------------------------------------------------------

    "foreign_worker_A201":
        "Foreign worker status: Yes",

    "foreign_worker_A202":
        "Foreign worker status: No",

    # --------------------------------------------------------
    # Personal Status / Sex
    # --------------------------------------------------------

    "personal_status_sex_A91":
        "Male: divorced or separated",

    "personal_status_sex_A92":
        "Female: married or widowed",

    "personal_status_sex_A93":
        "Male: single",

    "personal_status_sex_A94":
        "Male: married or widowed",

    "personal_status_sex_A95":
        "Female: single",
}


# ============================================================
# NUMERICAL FEATURE DESCRIPTIONS
# ============================================================

NUMERICAL_DESCRIPTIONS = {

    "duration_months":
        "Loan duration",

    "credit_amount":
        "Credit amount",

    "installment_rate":
        "Installment burden",

    "residence_since":
        "Years at current residence",

    "age":
        "Applicant age",

    "existing_credits":
        "Number of existing credits",

    "dependents":
        "Number of dependents",
}


# ============================================================
# FEATURE NAME CLEANING
# ============================================================

def clean_feature_name(feature):

    """
    Converts sklearn/SHAP feature names into
    human-readable names.
    """

    # Remove preprocessing prefixes
    feature = feature.replace(
        "categorical__",
        ""
    )

    feature = feature.replace(
        "numerical__",
        ""
    )

    # Check categorical mapping
    if feature in CATEGORY_DESCRIPTIONS:

        return CATEGORY_DESCRIPTIONS[
            feature
        ]

    # Check numerical mapping
    if feature in NUMERICAL_DESCRIPTIONS:

        return NUMERICAL_DESCRIPTIONS[
            feature
        ]

    # Fallback
    return feature.replace(
        "_",
        " "
    ).title()


# ============================================================
# SHAP VALUE INTERPRETATION
# ============================================================

def explain_shap_value(
    feature,
    shap_value
):

    """
    Converts one SHAP value into a readable
    explanation dictionary.

    Positive SHAP:
        increases model-estimated bad-credit risk.

    Negative SHAP:
        reduces model-estimated bad-credit risk.
    """

    readable_name = clean_feature_name(
        feature
    )

    if shap_value > 0:

        direction = "increases"

    else:

        direction = "reduces"

    return {

        "feature": readable_name,

        "impact": round(
            abs(float(shap_value)),
            4
        ),

        "direction": direction
    }


# ============================================================
# COMPLETE EXPLANATION GENERATOR
# ============================================================

def generate_explanation(
    explanation_df,
    probability,
    risk_level
):

    """
    Generates a complete human-readable
    responsible lending explanation.

    Parameters
    ----------
    explanation_df:
        DataFrame containing:
            feature
            shap_value
            absolute_shap

    probability:
        Model-estimated probability of bad credit.

    risk_level:
        The CANONICAL risk level for this probability, as produced by
        config.get_risk_level(). This module does not compute its own
        risk level — it only formats SHAP contributions into readable
        text and reports whatever risk level the assessment layer
        (which is the single source of truth for thresholds) supplies.
    """

    risk_increasing = []

    risk_reducing = []


    # --------------------------------------------------------
    # Process SHAP values
    # --------------------------------------------------------

    for _, row in explanation_df.iterrows():

        explanation = explain_shap_value(
            row["feature"],
            row["shap_value"]
        )

        if row["shap_value"] > 0:

            risk_increasing.append(
                explanation
            )

        elif row["shap_value"] < 0:

            risk_reducing.append(
                explanation
            )


    # --------------------------------------------------------
    # Sort by SHAP impact
    # --------------------------------------------------------

    risk_increasing = sorted(
        risk_increasing,
        key=lambda x: x["impact"],
        reverse=True
    )[:5]


    risk_reducing = sorted(
        risk_reducing,
        key=lambda x: x["impact"],
        reverse=True
    )[:5]


    # --------------------------------------------------------
    # Final explanation
    #
    # NOTE: risk_level is passed in, not computed here. The
    # canonical thresholds live in config.py only.
    # --------------------------------------------------------

    return {

        "risk_probability": round(
            float(probability),
            4
        ),

        "risk_percentage": round(
            float(probability) * 100,
            2
        ),

        "risk_level": risk_level,

        "risk_increasing_factors":
            risk_increasing,

        "risk_reducing_factors":
            risk_reducing
    }