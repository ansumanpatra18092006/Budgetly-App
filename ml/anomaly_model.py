"""
ml/anomaly_model.py

Category-aware spending-anomaly detection.

Previous behavior (kept as `detect_anomalies` for backward compatibility):
  A flat Z-score over a plain list of amounts, with no category context.
  Pooling every category into one mean/std makes the result arbitrary —
  a single large legitimate transaction (rent, insurance) can swamp the
  standard deviation and hide a genuinely unusual small-category expense,
  while normal-for-its-category amounts (e.g. a ₹1,640 Shopping purchase)
  can get compared against unrelated categories they were never meant to
  be compared against.

New behavior (`detect_category_anomalies`):
  Category is the PRIMARY comparison group. Each expense transaction's
  amount is compared against the user's own historical amounts *in that
  same category*, using a robust statistic (median + MAD) rather than
  mean/std, because personal-finance amounts are typically right-skewed
  (many small transactions, occasional large legitimate ones) and a
  single outlier shouldn't be able to distort the baseline the way it
  does with mean/std.

  Merchant history (when available via `description`) is used only as a
  secondary signal that can strengthen (never solely determine) severity.
"""

import numpy as np

# ── Tunables ──────────────────────────────────────────────────────────
MIN_CATEGORY_HISTORY   = 5     # minimum txns in a category before trusting a category-specific baseline
MAD_THRESHOLD           = 3.5   # modified z-score cutoff (standard robust-statistics default)
FALLBACK_MAD_THRESHOLD  = 4.5   # stricter cutoff when falling back to the user-level baseline
IQR_MULTIPLIER          = 1.5   # used only when MAD == 0 (near-identical historical amounts)
MERCHANT_MIN_HISTORY    = 3     # minimum same-merchant txns before using merchant as a secondary signal
MERCHANT_BOOST_MULTIPLIER = 1.5 # amount >= this x merchant's own median => treat as a corroborating signal


def _median(values):
    return float(np.median(values)) if values else 0.0


def _mad(values, med=None):
    """Median Absolute Deviation."""
    if not values:
        return 0.0
    med = _median(values) if med is None else med
    return float(np.median(np.abs(np.array(values) - med)))


def _modified_z(x, med, mad):
    # 0.6745 makes MAD comparable to a standard deviation under normality;
    # standard robust-statistics convention (Iglewicz & Hoaglin).
    if mad == 0:
        return 0.0
    return 0.6745 * (x - med) / mad


def _iqr_upper_bound(values):
    """Fallback bound when MAD == 0 (near-constant category history)."""
    if len(values) < 4:
        return None
    q1, q3 = np.percentile(values, [25, 75])
    iqr = q3 - q1
    if iqr == 0:
        return None
    return q3 + IQR_MULTIPLIER * iqr


def _severity(modified_z, deviation_ratio):
    if modified_z >= 6 or deviation_ratio >= 5:
        return "high"
    if modified_z >= MAD_THRESHOLD or deviation_ratio >= 3:
        return "medium"
    return "low"


def detect_category_anomalies(
    transactions,
    min_category_history=MIN_CATEGORY_HISTORY,
    mad_threshold=MAD_THRESHOLD,
    fallback_mad_threshold=FALLBACK_MAD_THRESHOLD,
):
    """
    transactions: list of dicts, each expense transaction with at least:
        {
            "id": <transaction id>,
            "amount": <float>,
            "category": <str>,
            "description": <str>   # optional; used as a merchant key
        }
    Callers should pass only `type='expense'` transactions for one user,
    fetched in a single query (see routes/insights.py integration notes).

    Returns a list of anomaly dicts sorted by severity (high first):
        {
            "transaction_id": ...,
            "amount": ...,
            "category": ...,
            "expected_amount": ...,   # category (or fallback) median
            "deviation": ...,         # amount / expected_amount, rounded
            "severity": "low" | "medium" | "high",
            "confidence": "high" | "low",   # "low" = fell back to user-level baseline
            "reason": "<human-readable, non-jargon explanation>",
        }
    """
    if not transactions:
        return []

    by_category = {}
    for t in transactions:
        cat = (t.get("category") or "Misc").strip() or "Misc"
        by_category.setdefault(cat, []).append(t)

    all_amounts = [float(t["amount"]) for t in transactions if t.get("amount") is not None]
    user_median = _median(all_amounts)
    user_mad = _mad(all_amounts, user_median)

    # Merchant baselines across the user's whole history (secondary signal only).
    by_merchant = {}
    for t in transactions:
        desc_key = (t.get("description") or "").strip().lower()
        if desc_key:
            by_merchant.setdefault(desc_key, []).append(float(t["amount"]))

    anomalies = []

    for cat, cat_txns in by_category.items():
        cat_amounts = [float(t["amount"]) for t in cat_txns]
        has_enough_history = len(cat_amounts) >= min_category_history

        if has_enough_history:
            baseline_amounts = cat_amounts
            med = _median(baseline_amounts)
            mad = _mad(baseline_amounts, med)
            active_threshold = mad_threshold
            confidence = "high"
        else:
            # Insufficient category history: fall back to the user-level
            # baseline, but require a stricter threshold and mark the
            # result as low-confidence rather than silently pretending
            # 2-3 category observations are a reliable baseline.
            baseline_amounts = all_amounts
            med = user_median
            mad = user_mad
            active_threshold = fallback_mad_threshold
            confidence = "low"

        iqr_upper = _iqr_upper_bound(baseline_amounts) if mad == 0 else None

        for t in cat_txns:
            amount = float(t["amount"])
            mz = _modified_z(amount, med, mad) if mad > 0 else 0.0

            if mad > 0:
                # Only flag unusually HIGH spending relative to the category;
                # unusually low spending isn't a meaningful "anomaly" here.
                is_outlier = amount > med and mz > active_threshold
            elif iqr_upper is not None:
                is_outlier = amount > iqr_upper
            else:
                is_outlier = False

            if not is_outlier:
                continue

            deviation_ratio = round(amount / med, 2) if med > 0 else None

            # Optional merchant secondary signal.
            desc_key = (t.get("description") or "").strip().lower()
            merchant_amounts = by_merchant.get(desc_key, [])
            merchant_boost = False
            if len(merchant_amounts) >= MERCHANT_MIN_HISTORY:
                merchant_typical = _median(merchant_amounts)
                if merchant_typical > 0 and amount >= merchant_typical * MERCHANT_BOOST_MULTIPLIER:
                    merchant_boost = True

            severity = _severity(mz, deviation_ratio or 0)
            if merchant_boost and severity != "high":
                severity = "medium" if severity == "low" else "high"
            if confidence == "low" and severity == "high":
                # Insufficient category history: never surface a "high"
                # severity anomaly on 2-4 observations, even if the
                # fallback baseline's math would otherwise call for it.
                severity = "medium"

            if deviation_ratio:
                reason = (
                    f"₹{amount:,.0f} {cat} expense is {deviation_ratio}x your usual "
                    f"{cat} spending of ₹{med:,.0f}."
                )
            else:
                reason = f"₹{amount:,.0f} {cat} expense is unusually high."
            if not has_enough_history:
                reason += " (limited category history — lower confidence)"

            anomalies.append({
                "transaction_id":  t.get("id"),
                "amount":          amount,
                "category":        cat,
                "expected_amount": round(med, 2),
                "deviation":       deviation_ratio,
                "severity":        severity,
                "confidence":      confidence,
                "reason":          reason,
            })

    anomalies.sort(key=lambda a: {"high": 0, "medium": 1, "low": 2}.get(a["severity"], 3))
    return anomalies


# ── Legacy entrypoint, preserved for backward compatibility ─────────
def detect_anomalies(amounts, threshold=2.5):
    """
    Original flat Z-score detector. Kept only in case another, unseen
    caller still depends on this exact signature (a plain list of
    amounts, returning positional indices). New/updated call sites
    should use detect_category_anomalies() instead, which is the
    category-aware replacement described in this module's docstring.
    """
    if not amounts or len(amounts) < 5:
        return []

    data = np.array(amounts)
    mean = np.mean(data)
    std = np.std(data)

    if std == 0:
        return []

    z_scores = np.abs((data - mean) / std)
    return np.where(z_scores > threshold)[0].tolist()