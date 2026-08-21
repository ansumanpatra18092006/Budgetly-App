# ml/credit_risk/model_cache.py

"""
Process-level lazy singleton cache for production ML artifacts.

Why this exists
----------------
Several modules (predict.py, risk_engine.py, explain.py, scenario.py,
final_assessment.py) previously called joblib.load() independently,
each time a prediction was made. That means every request re-read and
re-deserialized multi-hundred-KB pickle files from disk.

This module loads each artifact exactly once per process and reuses it.
There is no background thread, no file-watching, and no global mutable
state beyond the cached objects themselves — if the process restarts
(e.g. new deployment), the cache is naturally rebuilt from disk.

Usage
-----
    preprocessor = get_preprocessor()
    model = get_model()
    anomaly_model = get_anomaly_model()   # returns None if not available
"""

import os
import threading
import joblib


# ============================================================
# PATHS
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

MODEL_DIR = os.path.join(BASE_DIR, "models")

PREPROCESSOR_PATH = os.path.join(MODEL_DIR, "calibrated_preprocessor.pkl")
MODEL_PATH = os.path.join(MODEL_DIR, "calibrated_xgboost.pkl")
ANOMALY_MODEL_PATH = os.path.join(MODEL_DIR, "anomaly_model.pkl")


# ============================================================
# INTERNAL CACHE STATE
# ============================================================

_lock = threading.Lock()

_cache = {
    "preprocessor": None,
    "model": None,
    "anomaly_model": None,
    "anomaly_model_checked": False,
}


# ============================================================
# CORE (CALIBRATED) ARTIFACTS — REQUIRED
# ============================================================

def get_preprocessor():
    """
    Returns the cached calibrated preprocessor.
    Loads it from disk exactly once per process.
    """

    if _cache["preprocessor"] is None:

        with _lock:

            if _cache["preprocessor"] is None:

                if not os.path.exists(PREPROCESSOR_PATH):
                    raise FileNotFoundError(
                        "calibrated_preprocessor.pkl not found at "
                        f"{PREPROCESSOR_PATH}. Run calibration.py first. "
                        "Do NOT fall back to preprocessor.pkl (uncalibrated)."
                    )

                _cache["preprocessor"] = joblib.load(PREPROCESSOR_PATH)

    return _cache["preprocessor"]


def get_model():
    """
    Returns the cached calibrated XGBoost model
    (a sklearn CalibratedClassifierCV wrapping XGBClassifier).
    Loads it from disk exactly once per process.
    """

    if _cache["model"] is None:

        with _lock:

            if _cache["model"] is None:

                if not os.path.exists(MODEL_PATH):
                    raise FileNotFoundError(
                        "calibrated_xgboost.pkl not found at "
                        f"{MODEL_PATH}. Run calibration.py first. "
                        "Do NOT fall back to xgboost_model.pkl (uncalibrated)."
                    )

                _cache["model"] = joblib.load(MODEL_PATH)

    return _cache["model"]


# ============================================================
# ANOMALY MODEL — OPTIONAL, MUST DEGRADE GRACEFULLY
# ============================================================

def get_anomaly_model():
    """
    Returns the cached population-fitted anomaly model, or None
    if no valid artifact exists. Never fits a new model here —
    that would mean fitting on a single applicant at inference
    time, which is statistically invalid (see anomaly_train.py).
    """

    if not _cache["anomaly_model_checked"]:

        with _lock:

            if not _cache["anomaly_model_checked"]:

                if os.path.exists(ANOMALY_MODEL_PATH):
                    _cache["anomaly_model"] = joblib.load(ANOMALY_MODEL_PATH)
                else:
                    _cache["anomaly_model"] = None

                _cache["anomaly_model_checked"] = True

    return _cache["anomaly_model"]


# ============================================================
# TEST / DEBUG HELPERS
# ============================================================

def reset_cache():
    """
    Clears the cache. Intended for tests only — production code
    should never need to call this, since artifacts don't change
    within a running process.
    """

    with _lock:
        _cache["preprocessor"] = None
        _cache["model"] = None
        _cache["anomaly_model"] = None
        _cache["anomaly_model_checked"] = False


def cache_status():
    """Returns which artifacts are currently cached in memory (for diagnostics)."""

    return {
        "preprocessor_loaded": _cache["preprocessor"] is not None,
        "model_loaded": _cache["model"] is not None,
        "anomaly_model_checked": _cache["anomaly_model_checked"],
        "anomaly_model_loaded": _cache["anomaly_model"] is not None,
    }