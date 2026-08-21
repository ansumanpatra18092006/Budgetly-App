# ml/credit_risk/config.py

"""
Central configuration for the AI Credit Risk System.

All risk thresholds and decision rules should be defined here
so that every ML component uses the same policy.
"""


# ============================================================
# RISK THRESHOLDS
# ============================================================

LOW_RISK_THRESHOLD = 0.25
HIGH_RISK_THRESHOLD = 0.40


# ============================================================
# DECISION LABELS
# ============================================================

APPROVE_DECISION = "APPROVE"
REVIEW_DECISION = "MANUAL REVIEW"
REJECT_DECISION = "REJECT"


# ============================================================
# RISK LABELS
# ============================================================

LOW_RISK = "LOW RISK"
MEDIUM_RISK = "MEDIUM RISK"
HIGH_RISK = "HIGH RISK"


# ============================================================
# RISK LEVEL
# ============================================================

def get_risk_level(probability):
    """
    Convert calibrated bad-credit probability into
    the project's standardized risk category.
    """

    probability = float(probability)

    if probability < LOW_RISK_THRESHOLD:
        return LOW_RISK

    elif probability < HIGH_RISK_THRESHOLD:
        return MEDIUM_RISK

    return HIGH_RISK


# ============================================================
# DECISION
# ============================================================

def get_decision(probability):
    """
    Convert calibrated bad-credit probability into
    the project's standardized lending decision.
    """

    probability = float(probability)

    if probability < LOW_RISK_THRESHOLD:
        return APPROVE_DECISION

    elif probability < HIGH_RISK_THRESHOLD:
        return REVIEW_DECISION

    return REJECT_DECISION


# ============================================================
# COMPLETE POLICY
# ============================================================

def get_policy(probability):
    """
    Return both risk level and lending decision.
    """

    probability = float(probability)

    return {
        "risk_level": get_risk_level(probability),
        "decision": get_decision(probability)
    }


# ============================================================
# TEST
# ============================================================

if __name__ == "__main__":

    print("=" * 70)
    print("CREDIT RISK DECISION POLICY")
    print("=" * 70)

    test_probabilities = [
        0.10,
        0.24,
        0.25,
        0.30,
        0.39,
        0.40,
        0.60
    ]

    print()

    for probability in test_probabilities:

        policy = get_policy(probability)

        print(
            f"{probability:.2f}"
            f"  →  "
            f"{policy['risk_level']:<12}"
            f" →  "
            f"{policy['decision']}"
        )