# ml/credit_risk/fairness_service.py

"""
Serves fairness_report.csv as static, offline, dataset-level metadata.

Fairness metrics here (approval_rate_difference, tpr_difference,
fpr_difference per group) come from a one-time evaluation of model
predictions across the FULL dataset, grouped by personal_status_sex,
age_group, and foreign_worker (see fairness.py, which produces the
CSV this module reads).

This is deliberately NOT recomputed per individual request:
  - fairness is a property of the model/policy across a population,
    not a property of one applicant
  - there is no valid notion of "this one applicant's fairness score"
  - recomputing it per request would also mean retraining or
    re-scoring against the whole dataset on every API call, which is
    both wasteful and statistically nonsensical

If you want to surface this to end users, expose it as a static
"model fairness metadata" panel, not as part of an individual
assessment result.
"""

import os
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FAIRNESS_REPORT_PATH = os.path.join(BASE_DIR, "fairness_report.csv")

_cache = {"report": None, "loaded": False}


def get_fairness_report():
    """
    Returns the cached fairness report as a list of records, or an
    explicit unavailable state. Loaded from disk once per process.
    """

    if not _cache["loaded"]:

        if os.path.exists(FAIRNESS_REPORT_PATH):
            try:
                df = pd.read_csv(FAIRNESS_REPORT_PATH)
                _cache["report"] = df.to_dict(orient="records")
            except Exception as error:
                _cache["report"] = None
                _cache["error"] = str(error)
        else:
            _cache["report"] = None

        _cache["loaded"] = True

    if _cache["report"] is None:
        return {
            "available": False,
            "scope": "dataset-level (offline)",
            "records": [],
            "message": _cache.get(
                "error", "fairness_report.csv not found."
            ),
        }

    attributes = sorted({r["attribute"] for r in _cache["report"]})

    return {
        "available": True,
        "scope": "dataset-level (offline)",
        "attributes_evaluated": attributes,
        "records": _cache["report"],
        "message": (
            "Fairness differences (approval_rate, TPR, FPR) relative "
            "to a reference group, computed once offline across the "
            "full evaluation dataset. Not computed per applicant."
        ),
    }