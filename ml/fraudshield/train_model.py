from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.metrics import f1_score, precision_score, recall_score, roc_auc_score
from sklearn.model_selection import train_test_split
from xgboost import XGBClassifier

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = ROOT / "data" / "fraudshield" / "synthetic_upi_transactions.csv"
DEFAULT_MODEL = ROOT / "ml" / "fraudshield" / "fraudshield_model.joblib"

FEATURES = [
    "amount",
    "txn_count_1h",
    "txn_count_24h",
    "location_distance_km",
    "is_new_device",
    "merchant_risk",
    "account_age_days",
    "transaction_hour",
    "relationship_count",
    "merchant_seen_before",
    "device_trust_score",
    "time_deviation_hours",
    "amount_to_account_median",
    "network_risk",
]
TARGET = "is_fraud"


def generate_synthetic_dataset(path: Path, n: int = 20000, seed: int = 20260910) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    amount = np.exp(rng.normal(np.log(1800), 1.05, n)).clip(50, 150000)
    txn_count_1h = rng.poisson(1.35, n).clip(0, 15)
    txn_count_24h = (txn_count_1h + rng.poisson(3.2, n)).clip(0, 35)
    location_distance_km = np.abs(rng.normal(12, 45, n)).clip(0, 2500)
    is_new_device = rng.binomial(1, 0.17, n)
    merchant_risk = rng.beta(1.5, 5.0, n)
    account_age_days = rng.gamma(3.0, 135, n).clip(1, 2500).round().astype(int)
    transaction_hour = rng.integers(0, 24, n)
    relationship_count = rng.poisson(1.6, n).clip(0, 15)
    merchant_seen_before = (rng.random(n) > (0.08 + 0.45 * merchant_risk)).astype(int)
    device_trust_score = np.clip(1 - (0.75 * is_new_device + rng.beta(2.4, 5.5, n) * 0.55), 0, 1)
    time_deviation_hours = np.abs(rng.normal(1.8, 3.1, n)).clip(0, 12)

    # Build a behavioral baseline independent of the final risk label.
    account_median = np.exp(rng.normal(np.log(1700), 0.78, n)).clip(80, 25000)
    amount_to_account_median = np.clip(amount / account_median, 0.05, 40)
    network_risk = np.clip(
        0.45 * (relationship_count / 8.0)
        + 0.35 * (merchant_risk)
        + 0.20 * (1 - device_trust_score), 0, 1
    )

    night = ((transaction_hour <= 5) | (transaction_hour >= 23)).astype(float)
    velocity = np.clip(txn_count_1h / 5.0, 0, 2)
    amount_signal = np.clip(np.log1p(amount_to_account_median) / np.log(8), 0, 1.8)
    location_signal = np.clip(location_distance_km / 650.0, 0, 2)
    young_account = np.clip((150 - account_age_days) / 150.0, 0, 1)
    unseen_merchant = 1 - merchant_seen_before

    propensity = (
        1.55 * amount_signal
        + 2.20 * velocity
        + 2.20 * merchant_risk
        + 2.10 * is_new_device
        + 2.15 * location_signal
        + 1.05 * night
        + 1.15 * young_account
        + 0.90 * (relationship_count / 8.0)
        + 1.10 * unseen_merchant
        + 1.00 * network_risk
        + 0.75 * (1 - device_trust_score)
        + 0.70 * (time_deviation_hours / 8.0)
        + rng.normal(0, 0.22, n)
    )
    threshold = float(np.quantile(propensity, 0.88))
    is_fraud = (propensity >= threshold).astype(int)
    flips = rng.random(n) < 0.025
    is_fraud = np.where(flips, 1 - is_fraud, is_fraud).astype(int)

    merchants = np.array(["Amazon", "Flipkart", "Swiggy", "Myntra", "IRCTC", "UPI Wallet", "Unknown Merchant"])
    merchant = rng.choice(merchants, size=n, p=[0.20, 0.17, 0.16, 0.12, 0.09, 0.14, 0.12])
    channel = rng.choice(["UPI", "CARD", "NETBANKING"], size=n, p=[0.65, 0.23, 0.12])
    cities = np.array(["Bhubaneswar", "Cuttack", "Puri", "Kolkata", "New Delhi", "Hyderabad", "Mumbai"])
    location = rng.choice(cities, size=n)
    device_id = np.array([f"DEV-{i:05d}" for i in rng.integers(1000, 99999, n)])

    df = pd.DataFrame({
        "transaction_id": [f"TRX-{100000+i}" for i in range(n)],
        "account_ref": [f"AC-{10000+i}" for i in rng.integers(10000, 99999, n)],
        "merchant": merchant,
        "channel": channel,
        "location": location,
        "device_id": device_id,
        "amount": np.round(amount, 2),
        "txn_count_1h": txn_count_1h.astype(int),
        "txn_count_24h": txn_count_24h.astype(int),
        "location_distance_km": np.round(location_distance_km, 2),
        "is_new_device": is_new_device.astype(int),
        "merchant_risk": np.round(merchant_risk, 5),
        "account_age_days": account_age_days,
        "transaction_hour": transaction_hour.astype(int),
        "relationship_count": relationship_count.astype(int),
        "merchant_seen_before": merchant_seen_before.astype(int),
        "device_trust_score": np.round(device_trust_score, 5),
        "time_deviation_hours": np.round(time_deviation_hours, 2),
        "amount_to_account_median": np.round(amount_to_account_median, 3),
        "network_risk": np.round(network_risk, 5),
        "is_fraud": is_fraud,
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return df


def train(dataset_path: Path, model_path: Path, dataset_label: str) -> dict:
    df = pd.read_csv(dataset_path)
    missing = [c for c in FEATURES + [TARGET] if c not in df.columns]
    if missing:
        raise ValueError(f"Dataset missing required columns: {missing}")

    X = df[FEATURES].apply(pd.to_numeric, errors="coerce").fillna(0)
    y = pd.to_numeric(df[TARGET], errors="coerce").fillna(0).astype(int)
    if y.nunique() < 2:
        raise ValueError("Training target must contain at least two classes.")

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.25, random_state=42, stratify=y
    )

    pos = max(int(y_train.sum()), 1)
    neg = max(int(len(y_train) - pos), 1)
    scale_pos_weight = neg / pos

    model = XGBClassifier(
        n_estimators=320,
        max_depth=5,
        learning_rate=0.05,
        min_child_weight=2,
        subsample=0.90,
        colsample_bytree=0.90,
        reg_lambda=1.2,
        objective="binary:logistic",
        eval_metric="logloss",
        scale_pos_weight=scale_pos_weight,
        random_state=42,
        n_jobs=2,
    )
    model.fit(X_train, y_train)

    prob = model.predict_proba(X_test)[:, 1]
    pred = (prob >= 0.50).astype(int)
    iso = IsolationForest(n_estimators=220, contamination=min(max(float(y.mean()), 0.02), 0.12), random_state=42, n_jobs=2)
    iso.fit(X_train)

    metrics = {
        "roc_auc": round(float(roc_auc_score(y_test, prob)), 4),
        "precision": round(float(precision_score(y_test, pred, zero_division=0)), 4),
        "recall": round(float(recall_score(y_test, pred, zero_division=0)), 4),
        "f1": round(float(f1_score(y_test, pred, zero_division=0)), 4),
    }
    bundle = {
        "format_version": 1,
        "model": model,
        "isolation_forest": iso,
        "feature_names": FEATURES,
        "target_name": TARGET,
        "model_version": "FraudShield XGBoost v2.0",
        "dataset": dataset_label,
        "training_records": int(len(df)),
        "fraud_rate": round(float(y.mean()) * 100, 2),
        "metrics": metrics,
        "thresholds": {"normal_max": 30, "suspicious_max": 60, "review_max": 80},
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, model_path, compress=3)
    metadata_path = model_path.with_suffix(".json")
    metadata_path.write_text(json.dumps({k: v for k, v in bundle.items() if k not in {"model", "isolation_forest"}}, indent=2, default=str), encoding="utf-8")
    return {k: v for k, v in bundle.items() if k not in {"model", "isolation_forest"}}


def main():
    parser = argparse.ArgumentParser(description="Train FraudShield's replaceable fraud model bundle.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--generate", action="store_true")
    parser.add_argument("--rows", type=int, default=20000)
    args = parser.parse_args()

    if args.generate or not args.dataset.exists():
        generate_synthetic_dataset(args.dataset, n=args.rows)
    meta = train(args.dataset, args.model, args.dataset.name)
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
