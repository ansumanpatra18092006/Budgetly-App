# ml/credit_risk/anomaly_train.py

"""
OFFLINE, population-level anomaly model training.

This is the ONLY place an IsolationForest should ever be fit. It is
trained once on the full applicant population (using the SAME
production preprocessor as the risk model) and saved to disk as
models/anomaly_model.pkl.

Do NOT fit IsolationForest inside a request-handling path (that was
the bug in the original anomaly.py / final_assessment.py: fitting a
"population" model on a single incoming applicant is statistically
meaningless — every point in a 1-row dataset has decision_function
distance 0, so the resulting "anomaly score" is not a real measurement
of how unusual the applicant is relative to the population).

Run this script whenever the production model is retrained, so the
anomaly reference population stays aligned with the same feature
pipeline the risk model uses.
"""

import os
import joblib
import pandas as pd

from sklearn.ensemble import IsolationForest

from .model_cache import get_preprocessor, MODEL_DIR


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_PATH = os.path.join(BASE_DIR, "..", "..", "data", "german.data")

ANOMALY_MODEL_PATH = os.path.join(MODEL_DIR, "anomaly_model.pkl")

COLUMNS = [
    "checking_account", "duration_months", "credit_history", "purpose",
    "credit_amount", "savings_account", "employment_since",
    "installment_rate", "personal_status_sex", "other_debtors",
    "residence_since", "property", "age", "other_installment_plans",
    "housing", "existing_credits", "job", "dependents", "telephone",
    "foreign_worker", "target",
]


def load_population(data_path=DATA_PATH):

    df = pd.read_csv(data_path, sep=r"\s+", header=None, names=COLUMNS)

    return df.drop(columns=["target"])


def build_anomaly_model():

    return IsolationForest(
        n_estimators=300,
        contamination=0.05,
        max_samples="auto",
        random_state=42,
        n_jobs=-1,
    )


def train_and_save_anomaly_model(data_path=DATA_PATH, output_path=ANOMALY_MODEL_PATH):

    print("Loading applicant population...")
    X = load_population(data_path)
    print(f"Population shape: {X.shape}")

    print("\nTransforming with the PRODUCTION calibrated preprocessor...")
    # IMPORTANT: uses the exact same fitted preprocessor as the risk
    # model, via model_cache, so the anomaly model's feature space
    # matches production inference exactly (no separate encoder that
    # could silently drift out of sync).
    preprocessor = get_preprocessor()
    X_processed = preprocessor.transform(X)

    print("\nFitting population-level IsolationForest...")
    anomaly_model = build_anomaly_model()
    anomaly_model.fit(X_processed)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    joblib.dump(anomaly_model, output_path)

    print(f"\nSaved population-fitted anomaly model: {output_path}")

    return anomaly_model


if __name__ == "__main__":
    train_and_save_anomaly_model()