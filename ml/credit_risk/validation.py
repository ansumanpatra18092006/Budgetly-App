# ml/credit_risk/validation.py

"""
Strict input validation for the German Credit applicant schema.

This module exists so unknown/malformed categorical codes never silently
become a meaningless all-zero one-hot vector (OneHotEncoder(handle_unknown
="ignore") will do exactly that if we don't validate first), and so
invalid numeric values never reach the model.

Raises ValidationError with a structured list of problems; callers should
catch it and translate to whatever error format their transport layer needs
(HTTP 400, etc.).
"""


NUMERICAL_FEATURES = [
    "duration_months",
    "credit_amount",
    "installment_rate",
    "residence_since",
    "age",
    "existing_credits",
    "dependents",
]

CATEGORICAL_FEATURES = [
    "checking_account",
    "credit_history",
    "purpose",
    "savings_account",
    "employment_since",
    "personal_status_sex",
    "other_debtors",
    "property",
    "other_installment_plans",
    "housing",
    "job",
    "telephone",
    "foreign_worker",
]

ALL_FEATURES = NUMERICAL_FEATURES + CATEGORICAL_FEATURES


# ============================================================
# VALID CATEGORICAL CODES
#
# These are the standard UCI German Credit Dataset codes.
# Any value outside this set is rejected rather than silently
# passed through to OneHotEncoder(handle_unknown="ignore"),
# which would otherwise zero-vector it without warning.
# ============================================================

VALID_CATEGORY_CODES = {
    "checking_account": {"A11", "A12", "A13", "A14"},
    "credit_history": {"A30", "A31", "A32", "A33", "A34"},
    "purpose": {
        "A40", "A41", "A42", "A43", "A44", "A45",
        "A46", "A47", "A48", "A49", "A410",
    },
    "savings_account": {"A61", "A62", "A63", "A64", "A65"},
    "employment_since": {"A71", "A72", "A73", "A74", "A75"},
    "personal_status_sex": {"A91", "A92", "A93", "A94", "A95"},
    "other_debtors": {"A101", "A102", "A103"},
    "property": {"A121", "A122", "A123", "A124"},
    "other_installment_plans": {"A141", "A142", "A143"},
    "housing": {"A151", "A152", "A153"},
    "job": {"A171", "A172", "A173", "A174"},
    "telephone": {"A191", "A192"},
    "foreign_worker": {"A201", "A202"},
}


# ============================================================
# NUMERICAL BOUNDS
#
# Loose sanity bounds, not statistical outlier bounds — the
# anomaly model is responsible for flagging unusual-but-valid
# applicants. This layer only rejects impossible values.
# ============================================================

NUMERICAL_BOUNDS = {
    "duration_months": (1, 120),
    "credit_amount": (1, 1_000_000),
    "installment_rate": (1, 4),
    "residence_since": (1, 4),
    "age": (18, 100),
    "existing_credits": (1, 10),
    "dependents": (1, 10),
}


class ValidationError(ValueError):
    """Raised when an applicant payload fails validation."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(errors))


# ============================================================
# VALIDATION
# ============================================================

def validate_applicant(applicant):
    """
    Validates a raw applicant payload against the German Credit schema.

    Raises ValidationError (with a list of human-readable problems) if
    the payload is invalid. Returns nothing on success.
    """

    errors = []

    if not isinstance(applicant, dict):
        raise ValidationError(
            ["Applicant payload must be a JSON object / dict."]
        )

    # --------------------------------------------------------
    # MISSING FEATURES
    # --------------------------------------------------------

    missing = [f for f in ALL_FEATURES if f not in applicant]

    if missing:
        errors.append(
            "Missing required features: " + ", ".join(missing)
        )

    # --------------------------------------------------------
    # NUMERICAL FEATURES
    # --------------------------------------------------------

    for feature in NUMERICAL_FEATURES:

        if feature not in applicant:
            continue

        value = applicant[feature]

        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(
                f"'{feature}' must be a number, got "
                f"{type(value).__name__}: {value!r}"
            )
            continue

        if isinstance(value, float) and (value != value):  # NaN check
            errors.append(f"'{feature}' is NaN, which is not allowed.")
            continue

        low, high = NUMERICAL_BOUNDS[feature]

        if value < low or value > high:
            errors.append(
                f"'{feature}' value {value} is outside the allowed "
                f"range [{low}, {high}]."
            )

    # --------------------------------------------------------
    # CATEGORICAL FEATURES
    # --------------------------------------------------------

    for feature in CATEGORICAL_FEATURES:

        if feature not in applicant:
            continue

        value = applicant[feature]
        valid_codes = VALID_CATEGORY_CODES[feature]

        if not isinstance(value, str):
            errors.append(
                f"'{feature}' must be a string code, got "
                f"{type(value).__name__}: {value!r}"
            )
            continue

        if value not in valid_codes:
            errors.append(
                f"'{feature}' has invalid code '{value}'. "
                f"Valid codes: {sorted(valid_codes)}"
            )

    if errors:
        raise ValidationError(errors)


def find_unexpected_fields(applicant):
    """
    Returns fields in the payload that aren't part of the schema.
    Not fatal (extra fields are simply ignored by the model), but useful
    for callers who want to warn about likely naming mistakes.
    """

    if not isinstance(applicant, dict):
        return []

    return [k for k in applicant if k not in ALL_FEATURES]


def is_valid_applicant(applicant):
    """Boolean convenience wrapper around validate_applicant."""

    try:
        validate_applicant(applicant)
        return True
    except ValidationError:
        return False