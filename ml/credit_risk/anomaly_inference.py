# ml/credit_risk/anomaly_inference.py

"""
Production anomaly scoring for a single applicant.

PHASE 0.5 FIX:
The previous inference-time code (final_assessment.py::calculate_anomaly)
fit a brand-new IsolationForest on a single applicant's processed
feature row at request time. That is not valid anomaly detection —
IsolationForest needs a reference population to define what "normal"
looks like. Fit on n=1, every applicant is trivially "normal" (or the
score is meaningless noise), and the result is presented to users as
if it meant something.

This module ONLY loads a pre-fitted population model (see
anomaly_train.py) and scores the applicant against it. If no valid
population model exists, it returns an explicit "UNAVAILABLE" state
rather than fabricating a score.

Also: an anomaly is not fraud. It means "statistically unusual
relative to the training population," which can happen for entirely
legitimate reasons. This module never uses the word "fraud".
"""

import numpy as np

from .model_cache import get_anomaly_model


def score_anomaly(processed_features):
    """
    Scores an already-preprocessed applicant row against the cached
    population-fitted IsolationForest.

    Parameters
    ----------
    processed_features : array-like, shape (1, n_features)
        Output of the PRODUCTION preprocessor.transform() for a single
        applicant — must use the same preprocessor the anomaly model
        was trained with (see anomaly_train.py).

    Returns
    -------
    dict with is_anomaly / anomaly_score / anomaly_level / manual_review,
    or an explicit unavailable state if no population model is cached.
    """

    anomaly_model = get_anomaly_model()

    if anomaly_model is None:
        return {
            "available": False,
            "is_anomaly": None,
            "anomaly_score": None,
            "anomaly_level": "UNAVAILABLE",
            "manual_review": False,
            "message": (
                "No population-fitted anomaly model found. Run "
                "anomaly_train.py to build models/anomaly_model.pkl. "
                "Anomaly detection is skipped, not silently guessed."
            ),
        }

    try:
        prediction = anomaly_model.predict(processed_features)[0]  # 1 normal, -1 anomaly
        raw_score = anomaly_model.decision_function(processed_features)[0]

        # Larger = more anomalous, roughly 0-1 via a logistic squashing
        # of the population's decision_function scale. This is a
        # display transform only — the underlying IsolationForest
        # comparison against the population is what makes the score
        # meaningful, not this particular squashing function.
        anomaly_score = float(1 / (1 + np.exp(raw_score * 10)))

        is_anomaly = bool(prediction == -1)

        if not is_anomaly:
            anomaly_level = "NORMAL"
        elif anomaly_score >= 0.80:
            anomaly_level = "HIGH ANOMALY"
        elif anomaly_score >= 0.60:
            anomaly_level = "MEDIUM ANOMALY"
        else:
            anomaly_level = "LOW ANOMALY"

        manual_review = anomaly_level in ("HIGH ANOMALY", "MEDIUM ANOMALY")

        return {
            "available": True,
            "is_anomaly": is_anomaly,
            "anomaly_score": round(anomaly_score, 4),
            "anomaly_level": anomaly_level,
            "manual_review": manual_review,
        }

    except Exception as error:
        return {
            "available": False,
            "is_anomaly": None,
            "anomaly_score": None,
            "anomaly_level": "UNAVAILABLE",
            "manual_review": False,
            "message": f"Anomaly scoring failed: {error}",
        }