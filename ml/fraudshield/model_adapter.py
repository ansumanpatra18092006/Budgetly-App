"""Stable model adapter for FraudShield.

The UI/API depend only on this contract, so a new trained artifact can replace
``fraudshield_model.joblib`` without changing the dashboard or scoring routes.
A bundle should contain a classifier under ``model`` and ``feature_names``.
Optional ``isolation_forest`` is used as a secondary anomaly signal.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import joblib
import pandas as pd

DEFAULT_MODEL_PATH = Path(__file__).resolve().parent / "fraudshield_model.joblib"


def load_bundle(path: Path = DEFAULT_MODEL_PATH) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"FraudShield model artifact not found: {path}")
    raw = joblib.load(path)
    if isinstance(raw, dict) and "model" in raw:
        bundle = dict(raw)
    else:
        # Backward-compatible wrapper for a raw sklearn/XGBoost estimator.
        feature_names = list(getattr(raw, "feature_names_in_", []))
        bundle = {
            "format_version": 2,
            "model": raw,
            "feature_names": feature_names,
            "model_version": "FraudShield compatible model",
            "dataset": "External model artifact",
            "training_records": 0,
            "fraud_rate": None,
            "metrics": {},
            "thresholds": {"normal_max": 30, "suspicious_max": 60, "review_max": 80},
        }
    if not hasattr(bundle.get("model"), "predict_proba"):
        raise TypeError("FraudShield model must expose predict_proba().")
    names = list(bundle.get("feature_names") or [])
    if not names:
        names = list(getattr(bundle["model"], "feature_names_in_", []))
    if not names:
        raise ValueError("FraudShield model bundle must declare feature_names.")
    bundle["feature_names"] = names
    bundle.setdefault("format_version", 2)
    bundle.setdefault("thresholds", {"normal_max": 30, "suspicious_max": 60, "review_max": 80})
    return bundle


def make_frame(features: dict[str, Any], feature_names: list[str]) -> pd.DataFrame:
    missing = [name for name in feature_names if name not in features]
    if missing:
        raise ValueError(f"Live transaction is missing model features: {missing}")
    return pd.DataFrame([[features[name] for name in feature_names]], columns=feature_names)
