# ml/credit_risk/decision_policy.py

"""
OFFLINE THRESHOLD-SELECTION ANALYSIS TOOL — NOT A SERVING PATH.

This script sweeps thresholds against calibrated_test_predictions.csv
to help a human choose where to set the production policy. It is how
the canonical thresholds in config.py (0.25 / 0.40) were originally
informed.

IMPORTANT: classify_risk() / make_decision() / RISK_THRESHOLD defined
below are NOT imported by assessment.py, predict.py, risk_engine.py,
scenario.py, or final_assessment.py. All production serving code uses
config.get_risk_level() / config.get_decision() exclusively. Nothing in
this file should be wired into a serving path — if a future change
does that, it would recreate the duplicate-threshold problem PHASE 0.5
was meant to eliminate.
"""

import os
import pandas as pd
import numpy as np

from sklearn.metrics import (
    precision_score,
    recall_score,
    f1_score,
    accuracy_score,
    confusion_matrix
)


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PREDICTIONS_PATH = os.path.join(
    BASE_DIR,
    "models",
    "calibrated_test_predictions.csv"
)

OUTPUT_PATH = os.path.join(
    BASE_DIR,
    "models",
    "decision_policy_results.csv"
)


# ============================================================
# LOAD PREDICTIONS
# ============================================================

print("=" * 90)
print("RESPONSIBLE LENDING DECISION POLICY")
print("=" * 90)

print("\nLoading calibrated holdout predictions...")

if not os.path.exists(PREDICTIONS_PATH):

    raise FileNotFoundError(
        "calibrated_test_predictions.csv not found.\n"
        "Run calibration.py first."
    )

df = pd.read_csv(PREDICTIONS_PATH)

print(f"Loaded {len(df)} test predictions.")

print("\nColumns:")
print(df.columns.tolist())


# ============================================================
# IDENTIFY TARGET / PROBABILITY COLUMNS
# ============================================================

possible_target_columns = [
    "target",
    "y_true",
    "actual",
    "actual_target"
]

possible_probability_columns = [
    "calibrated_probability",
    "calibrated_prob",
    "probability",
    "risk_probability",
    "predicted_probability"
]


target_column = None
probability_column = None


for column in possible_target_columns:

    if column in df.columns:
        target_column = column
        break


for column in possible_probability_columns:

    if column in df.columns:
        probability_column = column
        break


if target_column is None:

    raise ValueError(
        "Could not find target column in prediction file."
    )


if probability_column is None:

    raise ValueError(
        "Could not find calibrated probability column "
        "in prediction file."
    )


print(
    f"\nTarget column      : {target_column}"
)

print(
    f"Probability column : {probability_column}"
)


# ============================================================
# EXTRACT DATA
# ============================================================

y_true = df[target_column].astype(int)

probabilities = df[probability_column].astype(float)


# ============================================================
# THRESHOLD EVALUATION
# ============================================================

thresholds = np.arange(
    0.10,
    0.81,
    0.05
)

results = []


print("\n")
print("=" * 90)
print("THRESHOLD EVALUATION")
print("=" * 90)

for threshold in thresholds:

    y_pred = (
        probabilities >= threshold
    ).astype(int)

    tn, fp, fn, tp = confusion_matrix(
        y_true,
        y_pred,
        labels=[0, 1]
    ).ravel()

    precision = precision_score(
        y_true,
        y_pred,
        zero_division=0
    )

    recall = recall_score(
        y_true,
        y_pred,
        zero_division=0
    )

    f1 = f1_score(
        y_true,
        y_pred,
        zero_division=0
    )

    accuracy = accuracy_score(
        y_true,
        y_pred
    )

    results.append({

        "threshold": round(
            threshold,
            2
        ),

        "accuracy": accuracy,

        "precision": precision,

        "recall": recall,

        "f1": f1,

        "true_negatives": tn,

        "false_positives": fp,

        "false_negatives": fn,

        "true_positives": tp
    })


results_df = pd.DataFrame(
    results
)


print(
    results_df.to_string(
        index=False,
        float_format=lambda x: f"{x:.4f}"
    )
)


# ============================================================
# FIND BEST THRESHOLDS
# ============================================================

best_f1 = results_df.loc[
    results_df["f1"].idxmax()
]

best_recall = results_df.loc[
    results_df["recall"].idxmax()
]


# Precision constraint

precision_constraint = (
    results_df[
        results_df["precision"] >= 0.55
    ]
)


if len(precision_constraint) > 0:

    best_balanced = precision_constraint.loc[
        precision_constraint["recall"].idxmax()
    ]

else:

    best_balanced = best_f1


# ============================================================
# PRINT BEST RESULTS
# ============================================================

print("\n")
print("=" * 90)
print("BEST F1 THRESHOLD")
print("=" * 90)

print(
    f"Threshold : {best_f1['threshold']:.2f}"
)

print(
    f"Precision : {best_f1['precision']:.4f}"
)

print(
    f"Recall    : {best_f1['recall']:.4f}"
)

print(
    f"F1        : {best_f1['f1']:.4f}"
)

print(
    f"Accuracy  : {best_f1['accuracy']:.4f}"
)


print("\n")
print("=" * 90)
print("BEST RECALL THRESHOLD")
print("=" * 90)

print(
    f"Threshold : {best_recall['threshold']:.2f}"
)

print(
    f"Precision : {best_recall['precision']:.4f}"
)

print(
    f"Recall    : {best_recall['recall']:.4f}"
)

print(
    f"F1        : {best_recall['f1']:.4f}"
)


print("\n")
print("=" * 90)
print("RECOMMENDED BALANCED POLICY")
print("=" * 90)

print(
    f"Threshold : {best_balanced['threshold']:.2f}"
)

print(
    f"Precision : {best_balanced['precision']:.4f}"
)

print(
    f"Recall    : {best_balanced['recall']:.4f}"
)

print(
    f"F1        : {best_balanced['f1']:.4f}"
)

print(
    f"Accuracy  : {best_balanced['accuracy']:.4f}"
)


# ============================================================
# RESPONSIBLE LENDING POLICY
# ============================================================

# We use the selected threshold as the
# boundary for identifying higher-risk applications.

RISK_THRESHOLD = float(
    best_balanced["threshold"]
)


# Manual review boundary.
#
# Applications below this threshold are
# considered lower risk.
#
# Applications around the threshold should
# receive additional human review.

REVIEW_MARGIN = 0.15


LOW_THRESHOLD = max(
    0.0,
    RISK_THRESHOLD - REVIEW_MARGIN
)

HIGH_THRESHOLD = RISK_THRESHOLD


def classify_risk(probability):

    if probability < LOW_THRESHOLD:

        return "LOW RISK"

    elif probability < HIGH_THRESHOLD:

        return "MEDIUM RISK"

    else:

        return "HIGH RISK"


def make_decision(probability):

    if probability < LOW_THRESHOLD:

        return "APPROVE"

    elif probability < HIGH_THRESHOLD:

        return "MANUAL REVIEW"

    else:

        return "REJECT"


# ============================================================
# APPLY POLICY TO HOLDOUT DATA
# ============================================================

df["risk_level"] = probabilities.apply(
    classify_risk
)

df["decision"] = probabilities.apply(
    make_decision
)


# ============================================================
# POLICY DISTRIBUTION
# ============================================================

print("\n")
print("=" * 90)
print("DECISION POLICY")
print("=" * 90)

print(
    f"\nLOW RISK       : probability < {LOW_THRESHOLD:.2f}"
)

print(
    f"MEDIUM RISK    : {LOW_THRESHOLD:.2f} "
    f"<= probability < {HIGH_THRESHOLD:.2f}"
)

print(
    f"HIGH RISK      : probability >= {HIGH_THRESHOLD:.2f}"
)

print("\nDecision distribution:")

print(
    df["decision"].value_counts()
)


print("\nRisk distribution:")

print(
    df["risk_level"].value_counts()
)


# ============================================================
# SAVE RESULTS
# ============================================================

results_df.to_csv(
    OUTPUT_PATH,
    index=False
)


print("\n")
print("=" * 90)
print("DECISION POLICY COMPLETED")
print("=" * 90)

print(
    f"\nSaved:"
)

print(
    OUTPUT_PATH
)