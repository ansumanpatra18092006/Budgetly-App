"""
FinTrust borrower spending-anomaly detection.

An anomaly is an unusually high expense compared with the user's own
historical behavior. It is a review signal, NOT a fraud label.

Design safeguards:
1. Recurring merchants are expected and are never flagged as anomalies.
2. A candidate is compared with its category history, so unrelated categories
   are not mixed together.
3. A candidate can still use recurring category observations as context. This
   lets a new/unusual Health transaction be compared with normal Health
   spending while a recurring Rent payment is simply excluded as a candidate.
4. Insufficient category history means insufficient evidence, not an anomaly.
5. Only recent transactions are surfaced in the Insights panel.
6. If a category has many simultaneous outliers, only the strongest recent one
   is surfaced so the panel remains actionable rather than becoming a list of
   repeated versions of the same spending problem.
"""
from datetime import date, datetime, timedelta
import numpy as np

MIN_CATEGORY_HISTORY = 5
MAD_THRESHOLD = 3.5
IQR_MULTIPLIER = 1.5
MERCHANT_MIN_HISTORY = 3
MERCHANT_AMOUNT_TOLERANCE = 0.15
MONTHLY_GAP_MIN = 24
MONTHLY_GAP_MAX = 40
DEFAULT_RECENT_DAYS = 120
MAX_ANOMALIES_PER_CATEGORY = 1
MAX_TOTAL_ANOMALIES = 12


def _median(values):
    return float(np.median(values)) if len(values) else 0.0


def _mad(values, med=None):
    if not len(values):
        return 0.0
    med = _median(values) if med is None else med
    return float(np.median(np.abs(np.asarray(values, dtype=float) - med)))


def _modified_z(x, med, mad):
    return 0.0 if mad == 0 else 0.6745 * (x - med) / mad


def _iqr_upper_bound(values):
    if len(values) < 4:
        return None
    q1, q3 = np.percentile(values, [25, 75])
    iqr = q3 - q1
    return q3 + IQR_MULTIPLIER * iqr if iqr > 0 else None


def _severity(modified_z, deviation_ratio):
    if modified_z >= 6 or deviation_ratio >= 5:
        return "high"
    if modified_z >= MAD_THRESHOLD or deviation_ratio >= 3:
        return "medium"
    return "low"


def _as_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        s=value.strip()
        for fmt in ("%Y-%m-%d","%Y-%m-%d %H:%M:%S"):
            try: return datetime.strptime(s[:19],fmt).date()
            except ValueError: pass
        try: return datetime.fromisoformat(s.replace("Z","+00:00")).date()
        except ValueError: return None
    return None


def _merchant_is_regular(items):
    """Detect stable, roughly monthly merchant/description recurrence."""
    if len(items) < MERCHANT_MIN_HISTORY:
        return False

    amounts=np.asarray([float(t["amount"]) for t in items],dtype=float)
    typical=_median(amounts)
    if typical <= 0:
        return False

    if float(np.max(np.abs(amounts-typical)/typical)) > MERCHANT_AMOUNT_TOLERANCE:
        return False

    dates=sorted(d for d in (_as_date(t.get("date")) for t in items) if d is not None)
    if len(dates) < MERCHANT_MIN_HISTORY:
        return True

    gaps=[(dates[i]-dates[i-1]).days for i in range(1,len(dates))]
    if not gaps:
        return True

    median_gap=float(np.median(gaps))
    return MONTHLY_GAP_MIN <= median_gap <= MONTHLY_GAP_MAX


def detect_category_anomalies(
    transactions,
    min_category_history=MIN_CATEGORY_HISTORY,
    mad_threshold=MAD_THRESHOLD,
    recent_days=DEFAULT_RECENT_DAYS,
    max_per_category=MAX_ANOMALIES_PER_CATEGORY,
    max_total=MAX_TOTAL_ANOMALIES,
):
    if not transactions:
        return []

    today=date.today()
    by_category={}
    by_merchant={}

    for t in transactions:
        try:
            amount=float(t.get("amount") or 0)
        except (TypeError,ValueError):
            continue
        if amount <= 0:
            continue

        txn_date=_as_date(t.get("date"))
        if txn_date is not None and txn_date > today:
            continue

        item=dict(t)
        item["amount"]=amount
        item["_category"]=(item.get("category") or "Misc").strip() or "Misc"
        item["_merchant_key"]=(item.get("description") or "").strip().lower()

        by_category.setdefault(item["_category"],[]).append(item)
        if item["_merchant_key"]:
            by_merchant.setdefault(item["_merchant_key"],[]).append(item)

    regular_merchants={
        key for key,items in by_merchant.items()
        if _merchant_is_regular(items)
    }

    cutoff=(today-timedelta(days=recent_days)
            if recent_days is not None and recent_days >= 0 else None)

    candidates=[]

    for category, category_txns in by_category.items():
        # Keep ALL category history for context. Only stable recurring merchants
        # are excluded from being candidates, not from the category baseline.
        if len(category_txns) < min_category_history:
            continue

        baseline=[float(t["amount"]) for t in category_txns]
        median=_median(baseline)
        mad=_mad(baseline,median)
        iqr_upper=_iqr_upper_bound(baseline) if mad == 0 else None

        for t in category_txns:
            if t.get("_merchant_key") in regular_merchants:
                continue  # expected recurring expense

            txn_date=_as_date(t.get("date"))
            if txn_date is not None:
                if txn_date > today:
                    continue
                if cutoff is not None and txn_date < cutoff:
                    continue

            amount=float(t["amount"])

            if mad > 0:
                modified_z=_modified_z(amount,median,mad)
                is_outlier=amount > median and modified_z > mad_threshold
            elif iqr_upper is not None:
                modified_z=0.0
                is_outlier=amount > iqr_upper
            else:
                continue

            if not is_outlier:
                continue

            deviation=round(amount/median,2) if median > 0 else None
            severity=_severity(modified_z,deviation or 0)

            candidates.append({
                "transaction_id":t.get("id"),
                "amount":amount,
                "category":category,
                "expected_amount":round(median,2),
                "deviation":deviation,
                "severity":severity,
                "confidence":"high",
                "reason":(
                    f"₹{amount:,.0f} {category} expense is {deviation}× your usual "
                    f"{category} spending of ₹{median:,.0f}."
                    if deviation else
                    f"₹{amount:,.0f} {category} expense is unusually high."
                ),
                "_date":txn_date or date.min,
            })

    # Keep one strongest actionable anomaly per category.
    severity_rank={"high":0,"medium":1,"low":2}
    candidates.sort(
        key=lambda a:(severity_rank.get(a["severity"],3), -a["amount"], -a["_date"].toordinal())
    )

    selected=[]
    seen_categories=set()
    for item in candidates:
        if item["category"] in seen_categories:
            continue
        seen_categories.add(item["category"])
        item.pop("_date",None)
        selected.append(item)
        if len(selected) >= max_total:
            break

    return selected


def detect_anomalies(amounts, threshold=2.5):
    """Legacy positional Z-score helper retained for compatibility."""
    if not amounts or len(amounts) < 5:
        return []
    data=np.asarray(amounts,dtype=float)
    mean=np.mean(data)
    std=np.std(data)
    if std == 0:
        return []
    return np.where(np.abs((data-mean)/std)>threshold)[0].tolist()
