"""Underwriting evidence coverage and review signals, never a fraud verdict."""
from datetime import datetime, timezone
from utils.db import get_db


def summarize_provenance(row):
    total = int(row.get("total_count") or 0)
    unverified = int(row.get("unverified_count") or 0)
    total_amount = float(row.get("total_amount") or 0)
    unverified_amount = float(row.get("unverified_amount") or 0)
    recent = int(row.get("recent_income_count") or 0)
    recent_amount = float(row.get("recent_income_amount") or 0)
    baseline = float(row.get("baseline_income_amount") or 0)
    # Seven days vs the preceding 28 days, normalized to one week.
    burst = recent >= 3 and recent_amount > 0 and recent_amount > 3 * (baseline / 4)
    percentage = round(100 * unverified / total, 1) if total else None
    warnings = []
    if total:
        warnings.append(f"{percentage}% of financial records are unverified and excluded from underwriting.")
    else:
        warnings.append("No financial records available; verified evidence is required.")
    if row.get("unknown_recorded_count"):
        warnings.append("Some legacy records have unknown entry times; burst detection coverage is incomplete.")
    if burst:
        warnings.append("Review required: a burst of self-reported income was entered in the seven days before the reference time. This is a review signal, not proof of fraud.")
    return {
        "policy": "VERIFIED_ONLY",
        "total_count": total, "unverified_count": unverified,
        "unverified_percentage": percentage,
        "unverified_amount_percentage": round(100 * unverified_amount / total_amount, 1) if total_amount else None,
        "unknown_recorded_count": int(row.get("unknown_recorded_count") or 0),
        "suspicious_income_burst": burst,
        "burst_window_days": 7, "baseline_window_days": 28,
        "recent_income_count": recent, "recent_income_amount": recent_amount,
        "baseline_weekly_income_amount": round(baseline / 4, 2),
        "warnings": warnings,
    }


def get_transaction_provenance(user_id, application_at=None, connection=None):
    anchor = application_at or datetime.now(timezone.utc)
    conn = connection if connection is not None else get_db()
    try:
        row = conn.execute("""
            SELECT count(*) AS total_count,
              count(*) FILTER (WHERE provenance <> 'VERIFIED') AS unverified_count,
              coalesce(sum(abs(amount)), 0) AS total_amount,
              coalesce(sum(abs(amount)) FILTER (WHERE provenance <> 'VERIFIED'), 0) AS unverified_amount,
              count(*) FILTER (WHERE provenance <> 'VERIFIED' AND recorded_at IS NULL) AS unknown_recorded_count,
              count(*) FILTER (WHERE provenance <> 'VERIFIED' AND type='income' AND amount > 0
                AND recorded_at >= %s::timestamptz - interval '7 days' AND recorded_at <= %s) AS recent_income_count,
              coalesce(sum(amount) FILTER (WHERE provenance <> 'VERIFIED' AND type='income' AND amount > 0
                AND recorded_at >= %s::timestamptz - interval '7 days' AND recorded_at <= %s), 0) AS recent_income_amount,
              coalesce(sum(amount) FILTER (WHERE provenance <> 'VERIFIED' AND type='income' AND amount > 0
                AND recorded_at >= %s::timestamptz - interval '35 days'
                AND recorded_at < %s::timestamptz - interval '7 days'), 0) AS baseline_income_amount
            FROM transactions WHERE user_id=%s
        """, (anchor, anchor, anchor, anchor, anchor, anchor, user_id)).fetchone()
    finally:
        if connection is None:
            conn.close()
    return summarize_provenance(row)
