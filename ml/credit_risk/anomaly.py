# anomaly.py
#
# ============================================================
# SUPERSEDED — kept only as a batch/offline-analysis reference.
# ============================================================
#
# PHASE 0.5 replaced the anomaly pipeline with two focused modules:
#
#   anomaly_train.py      -> fits ONE population-level IsolationForest
#                             using the PRODUCTION calibrated_preprocessor,
#                             and saves it to models/anomaly_model.pkl
#
#   anomaly_inference.py  -> loads that saved model (via model_cache) and
#                             scores a single applicant against it —
#                             never fits anything at request time
#
# This file's approach (fit its OWN preprocessor from scratch, fit
# IsolationForest on the whole dataset, score the whole dataset, but
# never save the fitted anomaly model to disk) is fine for a one-off
# batch report, but is NOT what production inference should use,
# because:
#   1. it builds a separate preprocessor instead of reusing
#      calibrated_preprocessor.pkl, so its feature space can silently
#      drift from the production model's
#   2. it never persists the fitted anomaly model, so there is nothing
#      for a serving process to load
#
# If you need a full-population anomaly CSV report, prefer
# anomaly_train.py's population + calibrated_preprocessor, then score
# with the saved model — do not reintroduce a second preprocessor here.
# ============================================================

import os
import pandas as pd
import numpy as np

from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.ensemble import IsolationForest


# ============================================================
# CONFIGURATION
# ============================================================

DATA_PATH = "data/german.data"

OUTPUT_DIR = "ml/credit_risk"

COLUMNS = [
    "checking_account",
    "duration_months",
    "credit_history",
    "purpose",
    "credit_amount",
    "savings_account",
    "employment_since",
    "installment_rate",
    "personal_status_sex",
    "other_debtors",
    "residence_since",
    "property",
    "age",
    "other_installment_plans",
    "housing",
    "existing_credits",
    "job",
    "dependents",
    "telephone",
    "foreign_worker",
    "target"
]


NUMERICAL_FEATURES = [
    "duration_months",
    "credit_amount",
    "installment_rate",
    "residence_since",
    "age",
    "existing_credits",
    "dependents"
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
    "foreign_worker"
]


# ============================================================
# LOAD DATA
# ============================================================

def load_data():

    df = pd.read_csv(
        DATA_PATH,
        sep=r"\s+",
        header=None,
        names=COLUMNS
    )

    return df


# ============================================================
# PREPROCESSING
# ============================================================

def build_preprocessor():

    numerical_pipeline = Pipeline([
        (
            "imputer",
            SimpleImputer(
                strategy="median"
            )
        ),
        (
            "scaler",
            StandardScaler()
        )
    ])


    categorical_pipeline = Pipeline([
        (
            "imputer",
            SimpleImputer(
                strategy="most_frequent"
            )
        ),
        (
            "encoder",
            OneHotEncoder(
                handle_unknown="ignore",
                sparse_output=False
            )
        )
    ])


    preprocessor = ColumnTransformer([

        (
            "numerical",
            numerical_pipeline,
            NUMERICAL_FEATURES
        ),

        (
            "categorical",
            categorical_pipeline,
            CATEGORICAL_FEATURES
        )

    ])

    return preprocessor


# ============================================================
# ANOMALY DETECTION
# ============================================================

def build_anomaly_model():

    model = IsolationForest(

        n_estimators=300,

        contamination=0.05,

        max_samples="auto",

        random_state=42,

        n_jobs=-1
    )

    return model


# ============================================================
# ANOMALY SEVERITY
# ============================================================

def get_anomaly_level(score, percentile):

    if percentile < 95:
        return "NORMAL"

    elif percentile < 97:
        return "LOW ANOMALY"

    elif percentile < 99:
        return "MEDIUM ANOMALY"

    else:
        return "HIGH ANOMALY"


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    print("Loading dataset...")

    df = load_data()

    print(
        f"Dataset shape: {df.shape}"
    )


    # ========================================================
    # REMOVE TARGET FROM ANOMALY MODEL
    # ========================================================

    X = df.drop(
        columns=["target"]
    )


    print(
        "\nBuilding preprocessing pipeline..."
    )

    preprocessor = build_preprocessor()


    X_processed = (
        preprocessor.fit_transform(X)
    )


    print(
        f"Processed shape: "
        f"{X_processed.shape}"
    )


    # ========================================================
    # TRAIN ISOLATION FOREST
    # ========================================================

    print(
        "\nTraining Isolation Forest..."
    )

    anomaly_model = (
        build_anomaly_model()
    )

    anomaly_model.fit(
        X_processed
    )

    print(
        "Anomaly detection model trained."
    )


    # ========================================================
    # GENERATE PREDICTIONS
    # ========================================================

    predictions = anomaly_model.predict(
        X_processed
    )


    # Isolation Forest:
    #
    #  1 = normal
    # -1 = anomaly

    anomaly_flags = np.where(
        predictions == -1,
        1,
        0
    )


    # ========================================================
    # ANOMALY SCORE
    # ========================================================

    raw_scores = (
        anomaly_model.decision_function(
            X_processed
        )
    )


    # Larger value = more anomalous
    anomaly_scores = -raw_scores


    # Normalize approximately to 0-1
    min_score = anomaly_scores.min()
    max_score = anomaly_scores.max()

    if max_score > min_score:

        normalized_scores = (
            anomaly_scores - min_score
        ) / (
            max_score - min_score
        )

    else:

        normalized_scores = np.zeros(
            len(anomaly_scores)
        )


    # ========================================================
    # CREATE RESULT DATAFRAME
    # ========================================================

    results = df.copy()

    results.insert(
        0,
        "application_id",
        range(
            1,
            len(results) + 1
        )
    )


    results["anomaly_score"] = (
        normalized_scores.round(4)
    )


    results["anomaly_flag"] = (
        anomaly_flags
    )


    # Calculate percentile rank of anomaly score
    results["anomaly_percentile"] = (
        results["anomaly_score"]
        .rank(pct=True)
        * 100
    )
    
    results["anomaly_level"] = results.apply(
        lambda row: get_anomaly_level(
            row["anomaly_score"],
            row["anomaly_percentile"]
        ),
        axis=1
    )


    results["manual_review"] = (
        results["anomaly_flag"]
        .map({
            0: "NO",
            1: "YES"
        })
    )


    # ========================================================
    # SUMMARY
    # ========================================================

    total_applications = len(
        results
    )

    total_anomalies = int(
        results["anomaly_flag"].sum()
    )


    anomaly_percentage = (
        total_anomalies
        /
        total_applications
        *
        100
    )


    print("\n")

    print("=" * 85)

    print(
        "ANOMALY DETECTION RESULTS"
    )

    print("=" * 85)


    print(
        f"\nTotal applications: "
        f"{total_applications}"
    )

    print(
        f"Anomalous applications: "
        f"{total_anomalies}"
    )

    print(
        f"Anomaly percentage: "
        f"{anomaly_percentage:.2f}%"
    )


    # ========================================================
    # ANOMALY LEVEL DISTRIBUTION
    # ========================================================

    print("\n")

    print(
        "ANOMALY LEVEL DISTRIBUTION"
    )

    print("-" * 50)


    level_counts = (
        results["anomaly_level"]
        .value_counts()
    )


    for level, count in (
        level_counts.items()
    ):

        percentage = (
            count
            /
            total_applications
            *
            100
        )

        print(
            f"{level:<20}"
            f"{count:>6} "
            f"({percentage:.2f}%)"
        )


    # ========================================================
    # TOP ANOMALOUS APPLICATIONS
    # ========================================================

    print("\n")

    print("=" * 85)

    print(
        "TOP 10 MOST ANOMALOUS APPLICATIONS"
    )

    print("=" * 85)


    top_anomalies = (
        results
        .sort_values(
            "anomaly_score",
            ascending=False
        )
        .head(10)
    )


    display_columns = [

        "application_id",

        "credit_amount",

        "duration_months",

        "age",

        "checking_account",

        "credit_history",

        "purpose",

        "employment_since",

        "anomaly_score",

        "anomaly_level",

        "manual_review"
    ]


    print(
        top_anomalies[
            display_columns
        ].to_string(
            index=False
        )
    )


    # ========================================================
    # SAVE RESULTS
    # ========================================================

    os.makedirs(
        OUTPUT_DIR,
        exist_ok=True
    )


    output_path = os.path.join(
        OUTPUT_DIR,
        "anomaly_results.csv"
    )


    results.to_csv(
        output_path,
        index=False
    )


    # ========================================================
    # SAVE ONLY FLAGGED APPLICATIONS
    # ========================================================

    flagged_path = os.path.join(
        OUTPUT_DIR,
        "anomalous_applications.csv"
    )


    anomalous_applications = (
        results[
            results["anomaly_flag"] == 1
        ]
        .sort_values(
            "anomaly_score",
            ascending=False
        )
    )


    anomalous_applications.to_csv(
        flagged_path,
        index=False
    )


    # ========================================================
    # FINAL OUTPUT
    # ========================================================

    print("\n")

    print("=" * 85)

    print(
        "ANOMALY DETECTION COMPLETED"
    )

    print("=" * 85)


    print(
        "\nSaved files:"
    )

    print(
        f"1. {output_path}"
    )

    print(
        f"2. {flagged_path}"
    )