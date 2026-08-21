"""
Recurring transaction intelligence engine.

Detects recurring expense/income patterns from raw transaction history using
deterministic, explainable heuristics (NOT machine learning). Every score
returned by this module is a hand-specified function of observable
transaction data (dates, amounts, categories, types) and can be reproduced
by hand from the `confidence_factors` / `classification_factors` returned
alongside each item.
"""

import re
import math
import calendar
from datetime import date, timedelta
from utils.db import get_db


# ============================================================
# Small math helpers
# ============================================================

def _median(lst):
    n = len(lst)
    if n == 0:
        return 0
    s = sorted(lst)
    mid = n // 2
    return (s[mid] + s[mid - 1]) / 2.0 if n % 2 == 0 else s[mid]


def _mean(lst):
    return sum(lst) / len(lst) if lst else 0


def _std_dev(lst, mean_val):
    if len(lst) < 2:
        return 0.0
    return math.sqrt(sum((x - mean_val) ** 2 for x in lst) / len(lst))


# ============================================================
# Phase 1: Merchant normalization
# ============================================================

# Words that vary transaction-to-transaction for the *same* merchant and
# should not affect grouping. Deliberately narrow - this is not a fuzzy
# matcher, just noise removal for the same underlying payee.
_PAYMENT_NOISE_WORDS = {
    "payment", "pymt", "upi", "txn", "transaction", "ref", "reference",
    "card", "netbanking", "neft", "imps", "rtgs", "ach", "debit", "credit",
    "purchase", "pos", "recurring", "autopay", "auto", "billpay",
    "standing", "instruction", "order", "no", "id", "via",
}

# Obvious transaction/reference identifiers - stripped BEFORE punctuation
# removal because they rely on digit/separator boundaries that punctuation
# stripping would otherwise blur into ambiguous digit runs.
_REF_ID_PATTERNS = [
    re.compile(r'\b[a-z]{0,3}\d{6,}[a-z0-9]*\b'),   # long numeric/alnum ids
    re.compile(r'\b\d{2}[/-]\d{2}[/-]\d{2,4}\b'),   # embedded dates
    re.compile(r'\b(?:xx+|\*+)\d{2,}\b'),           # masked card numbers
]


def _normalize_merchant(description):
    """
    Produce a stable normalized merchant key from a raw transaction
    description, used only for grouping. This is intentionally
    conservative: no edit-distance / phonetic fuzzy matching, so unrelated
    merchants are never collapsed together. It only strips noise that
    legitimately varies transaction-to-transaction for the same merchant
    (payment-rail prefixes, reference numbers, masked card digits).
    """
    if not description or not description.strip():
        return "unknown"

    desc = description.lower().strip()

    for pattern in _REF_ID_PATTERNS:
        desc = pattern.sub(' ', desc)

    desc = re.sub(r'[^a-z0-9\s]', ' ', desc)

    tokens = [t for t in desc.split() if t not in _PAYMENT_NOISE_WORDS]
    # Drop leftover isolated numeric fragments (remnants of stripped ids),
    # but keep short numbers that are plausibly part of a merchant name
    # (e.g. "7eleven", "365").
    tokens = [t for t in tokens if not (t.isdigit() and len(t) >= 4)]

    normalized = ' '.join(tokens).strip()
    return normalized if normalized else "unknown"


# ============================================================
# Phase 2: Candidate grouping
# ============================================================

def _fetch_transactions(user_id):
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT id, description, amount, category, type, date::date as dt
            FROM transactions WHERE user_id=%s ORDER BY date ASC
        """, (user_id,)).fetchall()
    finally:
        conn.close()
    return rows


def _group_candidates(rows):
    """
    Group transactions into recurrence candidates by normalized merchant +
    transaction type (income and expense are always kept separate). Dedupes
    on transaction id to guard against duplicate rows.
    """
    groups = {}
    seen_ids = set()

    for r in rows:
        txn_id = r["id"]
        if txn_id in seen_ids:
            continue
        seen_ids.add(txn_id)

        norm = _normalize_merchant(r["description"])
        t_type = r["type"]
        category = r["category"] or ""
        key = f"{norm}|{t_type}"

        if key not in groups:
            groups[key] = {
                "normalized": norm,
                "type": t_type,
                "descriptions": [],
                "categories": [],
                "dates": [],
                "amounts": [],
            }
        g = groups[key]
        g["descriptions"].append(r["description"])
        g["categories"].append(category)
        g["dates"].append(r["dt"])
        g["amounts"].append(float(r["amount"]))

    # Ensure chronological order within each group (defensive - the query
    # already orders by date, but grouping doesn't guarantee it survives).
    for g in groups.values():
        paired = sorted(
            zip(g["dates"], g["amounts"], g["descriptions"], g["categories"]),
            key=lambda p: p[0],
        )
        g["dates"] = [p[0] for p in paired]
        g["amounts"] = [p[1] for p in paired]
        g["descriptions"] = [p[2] for p in paired]
        g["categories"] = [p[3] for p in paired]

    return groups


# ============================================================
# Phase 3: Recurrence statistics + frequency detection
# ============================================================

_FREQUENCY_BANDS = [
    # (name, nominal_days, tolerance_days)
    ("weekly", 7, 2),
    ("biweekly", 14, 3),
    ("monthly", 30, 6),
    ("quarterly", 91, 10),
    ("semiannual", 182, 15),
    ("annual", 365, 20),
]
_FREQUENCY_NOMINAL_DAYS = {name: nominal for name, nominal, _ in _FREQUENCY_BANDS}
_ANNUAL_MULTIPLIER = {
    "weekly": 52, "biweekly": 26, "monthly": 12,
    "quarterly": 4, "semiannual": 2, "annual": 1,
}


def _compute_stats(dates, amounts):
    intervals = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
    median_interval = _median(intervals) if intervals else 0
    mean_interval = _mean(intervals) if intervals else 0
    interval_std = _std_dev(intervals, mean_interval) if intervals else 0.0

    avg_amt = _mean(amounts)
    med_amt = _median(amounts)
    std_amt = _std_dev(amounts, avg_amt)
    cv_amt = (std_amt / avg_amt) if avg_amt else 1.0

    return {
        "first_date": dates[0],
        "last_date": dates[-1],
        "occurrences": len(dates),
        "intervals": intervals,
        "median_interval": median_interval,
        "mean_interval": mean_interval,
        "interval_std": interval_std,
        "avg_amount": avg_amt,
        "median_amount": med_amt,
        "min_amount": min(amounts),
        "max_amount": max(amounts),
        "amount_std": std_amt,
        "coefficient_of_variation": round(cv_amt, 4),
        # The calendar day-of-month this recurrence was originally anchored
        # to (from the first known occurrence). Monthly/quarterly/etc.
        # projections re-anchor to this day every cycle rather than
        # chaining off the day of the *previous* projection, so a short
        # month doesn't permanently shift the billing day forward (see
        # _add_months).
        "anchor_day": dates[0].day,
    }


def _detect_frequency(median_interval):
    """Maps a median interval (days) to the closest realistic frequency band."""
    if median_interval <= 0:
        return "unknown"
    best, best_diff = None, None
    for name, nominal, tolerance in _FREQUENCY_BANDS:
        if nominal - tolerance <= median_interval <= nominal + tolerance:
            diff = abs(median_interval - nominal)
            if best is None or diff < best_diff:
                best, best_diff = name, diff
    return best or "unknown"


def _evidence_level(occurrences):
    if occurrences >= 4:
        return "high_confidence_eligible"
    if occurrences == 3:
        return "probable_recurring"
    if occurrences == 2:
        return "possible_recurring"
    return "insufficient"


# ============================================================
# Phase 4: Confidence scoring (deterministic heuristic, explainable)
# ============================================================

def _score_occurrence_count(n):
    return round(min(1.0, n / 6.0), 3)


def _score_interval_consistency(intervals):
    """
    Higher = more consistent gaps between transactions. A single observed
    interval (2 occurrences) cannot prove a stable cadence on its own, so
    it is deliberately capped rather than scored as "perfect".
    """
    if not intervals:
        return 0.0
    if len(intervals) == 1:
        return 0.5
    mean_int = _mean(intervals)
    if mean_int <= 0:
        return 0.0
    std_int = _std_dev(intervals, mean_int)
    return round(max(0.0, 1.0 - (std_int / mean_int)), 3)


def _score_amount_consistency(cv_amt):
    return round(max(0.0, 1.0 - min(cv_amt, 1.0)), 3)


def _score_recency(last_date, median_interval, today):
    if median_interval <= 0:
        return 0.5
    cycles_since = (today - last_date).days / median_interval
    return round(max(0.0, 1.0 - (cycles_since / 3.0)), 3)


def _score_frequency_fit(median_interval, freq):
    nominal = _FREQUENCY_NOMINAL_DAYS.get(freq)
    if not nominal:
        return 0.0
    return round(max(0.0, 1.0 - (abs(median_interval - nominal) / nominal)), 3)


def _calculate_confidence(stats, freq, today):
    occurrences = stats["occurrences"]
    scores = {
        "occurrence_history": _score_occurrence_count(occurrences),
        "interval_consistency": _score_interval_consistency(stats["intervals"]),
        "amount_consistency": _score_amount_consistency(stats["coefficient_of_variation"]),
        "recency": _score_recency(stats["last_date"], stats["median_interval"], today),
        "frequency_fit": _score_frequency_fit(stats["median_interval"], freq),
    }
    weights = {
        "occurrence_history": 0.20,
        "interval_consistency": 0.30,
        "amount_consistency": 0.20,
        "recency": 0.15,
        "frequency_fit": 0.15,
    }
    confidence = sum(scores[k] * weights[k] for k in weights)

    capped = False
    if occurrences == 2:
        # Two points define a line, not a pattern - never let two
        # transactions read as "definite" recurring behavior.
        capped_confidence = min(confidence, 0.55)
        capped = capped_confidence < confidence
        confidence = capped_confidence

    return round(min(confidence, 1.0), 3), {
        "scores": scores,
        "weights": weights,
        "evidence_level": _evidence_level(occurrences),
        "low_evidence_cap_applied": capped,
    }


# ============================================================
# Phase 5: Calendar-aware next occurrence
# ============================================================

def _add_months(d, months, anchor_day=None):
    """
    Adds calendar months to a date, landing on `anchor_day` of the target
    month (clamped to that month's real length - handles Jan 31 -> Feb
    28/29, leap years, year boundaries).

    `anchor_day` defaults to `d.day` for callers that don't have a stored
    anchor. Passing the *original* anchor day explicitly (rather than
    reusing `d.day`) matters for chained monthly/quarterly projections:
    if a short month clamps a projection down (e.g. day 31 -> 28), adding
    months again from that clamped date would otherwise permanently drift
    the billing day to 28, even once back in a month with 31 days.
    """
    if anchor_day is None:
        anchor_day = d.day
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(anchor_day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _next_occurrence(freq, stats, today):
    last_date = stats["last_date"]
    median_interval = stats["median_interval"]
    anchor_day = stats["anchor_day"]

    if freq in ("weekly", "biweekly"):
        # Interval-based: preserves day-of-week naturally.
        step = int(round(median_interval)) if median_interval > 0 else _FREQUENCY_NOMINAL_DAYS[freq]
        next_expected = last_date + timedelta(days=step)
        window_days = 3 if freq == "weekly" else 4
    elif freq == "monthly":
        next_expected = _add_months(last_date, 1, anchor_day)
        window_days = 5
    elif freq == "quarterly":
        next_expected = _add_months(last_date, 3, anchor_day)
        window_days = 7
    elif freq == "semiannual":
        next_expected = _add_months(last_date, 6, anchor_day)
        window_days = 10
    elif freq == "annual":
        next_expected = _add_months(last_date, 12, anchor_day)
        window_days = 14
    else:
        step = int(median_interval) if median_interval > 0 else 30
        next_expected = last_date + timedelta(days=step)
        window_days = 7

    window_start = next_expected - timedelta(days=window_days)
    window_end = next_expected + timedelta(days=window_days)

    return {
        "next_expected_date": next_expected,
        "window_start": window_start,
        "window_end": window_end,
        "window_days": window_days,
        "days_until": (next_expected - today).days,
    }


# ============================================================
# Phase 6: Status
# ============================================================
#
# Lifecycle and payment timing are deliberately separate axes:
#   - lifecycle_status answers "is this commitment still active?"
#     (active / possibly_missed / possibly_inactive / inactive / uncertain)
#   - payment_status answers "where are we in the current billing cycle?"
#     (not_due / upcoming / due_soon / overdue / uncertain)
#
# A subscription can be `active` while its payment is `not_due` (e.g. next
# charge in 18 days) - collapsing both into one `status` field made that
# impossible to express, so callers had no way to distinguish "this is
# fine, just not due yet" from "this looks cancelled".

def _determine_statuses(stats, next_occ, confidence_factors):
    """Returns (lifecycle_status, payment_status)."""
    days_until = next_occ["days_until"]
    window_days = next_occ["window_days"]
    median_interval = stats["median_interval"] or 30
    occurrences = stats["occurrences"]

    # Low-evidence / poor-fit candidates get an honest "uncertain" label
    # instead of a falsely precise timing status.
    int_score = confidence_factors["scores"]["interval_consistency"]
    if occurrences == 2 and int_score < 0.35:
        return "uncertain", "uncertain"

    if -window_days <= days_until <= window_days:
        return "active", "due_soon"

    if days_until > window_days:
        # Comfortably mid-cycle vs. genuinely approaching.
        fraction_elapsed = (median_interval - days_until) / median_interval if median_interval else 0
        payment_status = "not_due" if fraction_elapsed < 0.5 else "upcoming"
        return "active", payment_status

    # days_until < -window_days: the expected date has passed without a
    # matching transaction yet. Require multiple missed cycles before
    # calling something inactive - one late payment is not enough evidence.
    # The payment is unambiguously overdue regardless of how many cycles
    # have been missed; lifecycle escalates separately as evidence mounts.
    cycles_late = abs(days_until) / median_interval
    if cycles_late < 1:
        lifecycle_status = "active"
    elif cycles_late < 2:
        lifecycle_status = "possibly_missed"
    elif cycles_late < 3:
        lifecycle_status = "possibly_inactive"
    else:
        lifecycle_status = "inactive"
    return lifecycle_status, "overdue"


def _legacy_status(lifecycle_status, payment_status):
    """
    Reconstructs the original single-field `status` value from the new
    (lifecycle_status, payment_status) pair, kept only for callers that
    have not migrated to the new fields yet. The new fields are
    authoritative; this is a lossy compatibility view of them.
    """
    if lifecycle_status == "uncertain" or payment_status == "uncertain":
        return "uncertain"
    if payment_status == "due_soon":
        return "due_soon"
    if payment_status == "overdue":
        return lifecycle_status if lifecycle_status != "active" else "overdue"
    if payment_status == "not_due":
        return "active"
    if payment_status == "upcoming":
        return "upcoming"
    return lifecycle_status


# ============================================================
# Phase 7: Classification
# ============================================================

# ------------------------------------------------------------------
# Keyword matching is TOKEN/PHRASE-aware, never a naive substring check.
# `norm_desc` is a space-joined token string (see _normalize_merchant),
# so a "word" is matched with boundaries and a "phrase" is matched as an
# exact consecutive run of tokens. This is what prevents e.g. "Subham
# Jio" from matching a bare "jio" the way a substring check would.
# ------------------------------------------------------------------

def _has_token(norm_desc, word):
    return re.search(rf'(?:^|\s){re.escape(word)}(?:\s|$)', norm_desc) is not None


def _has_phrase(norm_desc, phrase):
    return _has_token(norm_desc, phrase) if ' ' not in phrase else (
        re.search(rf'(?:^|\s){re.escape(phrase)}(?:\s|$)', norm_desc) is not None
    )


def _any_token(norm_desc, words):
    return any(_has_token(norm_desc, w) for w in words)


def _any_phrase(norm_desc, phrases):
    return any(_has_phrase(norm_desc, p) for p in phrases)


# Strong, low-ambiguity standalone words: seeing these anywhere in the
# merchant description is meaningful on its own.
_SUBSCRIPTION_STRONG_WORDS = {
    "netflix", "spotify", "hotstar", "hulu", "disney", "icloud", "gym",
    "membership", "subscription", "fitness", "audible", "xbox",
    "playstation", "vpn", "saas", "gaana", "wynk", "sonyliv", "zee5",
}
# Ambiguous roots (common brand/provider names that are ALSO used for
# completely ordinary purchases) require an explicit qualifying phrase
# before they count as subscription evidence - see Rule 4.
_SUBSCRIPTION_PHRASES = {
    "amazon prime", "prime video", "youtube premium", "youtube music",
    "google one", "apple music", "apple tv", "apple one", "disney hotstar",
    "storage plan", "cloud storage",
}

_BILL_STRONG_WORDS = {
    "electricity", "water", "gas", "rent", "insurance", "emi", "broadband",
    "internet", "utility", "utilities", "mortgage", "municipal", "wifi",
    "dth", "postpaid",
}
# Provider names alone (jio/airtel/vodafone/vi) are ambiguous - they only
# become a strong bill signal paired with explicit billing language.
_BILL_PHRASES = {
    "jio postpaid", "jio bill", "jio broadband", "airtel postpaid",
    "airtel broadband", "airtel bill", "vodafone postpaid", "vi postpaid",
    "vodafone bill", "mobile bill", "phone bill", "internet bill",
    "water bill", "gas bill", "electricity bill", "loan emi",
}
# Weak/medium signal: a provider name plus "recharge" is *potentially* a
# bill (Rule 4/C) but not as certain as an explicit postpaid/bill phrase.
_PROVIDER_WEAK_PHRASES = {
    "jio recharge", "airtel recharge", "vodafone recharge", "vi recharge",
}
_PROVIDER_NAMES = {"jio", "airtel", "vodafone", "vi"}

_INCOME_KEYWORDS = {
    "salary", "payroll", "wages", "stipend", "pension", "dividend",
    "interest", "reimbursement",
}

_SUBSCRIPTION_CATEGORIES = {"subscription", "subscriptions", "entertainment", "streaming", "software"}
_BILL_CATEGORIES = {"utilities", "utility", "rent", "insurance", "loan", "emi", "housing", "telecom", "bills"}
_INCOME_CATEGORIES = {"salary", "income", "payroll"}
# Rule 3: discretionary spending is strong negative evidence for both
# subscription and recurring_bill classification, even if it recurs.
_DISCRETIONARY_CATEGORIES = {
    "food", "dining", "restaurant", "restaurants", "groceries", "grocery",
    "shopping", "travel", "p2p", "personal", "misc", "miscellaneous",
    "other", "retail", "entertainment_oneoff", "stationery", "cash",
}


def _classify_candidate(norm_desc, category, t_type, freq, stats, confidence_factors):
    cat_lower = (category or "").lower().strip()
    cv_amt = stats["coefficient_of_variation"]
    int_score = confidence_factors["scores"]["interval_consistency"]
    occurrences = stats["occurrences"]

    if t_type == "income":
        score = 0.45 + 0.30 * int_score
        if cat_lower in _INCOME_CATEGORIES or _any_token(norm_desc, _INCOME_KEYWORDS):
            score += 0.20
        return "recurring_income", round(min(score, 0.97), 3), {
            "signals": {
                "type_is_income": True,
                "category_match": cat_lower in _INCOME_CATEGORIES,
                "keyword_match": _any_token(norm_desc, _INCOME_KEYWORDS),
                "interval_consistency": int_score,
            }
        }

    scores = {"subscription": 0.0, "recurring_bill": 0.0, "unknown_recurring": 0.15}

    category_signal = {"subscription": False, "recurring_bill": False}
    if cat_lower in _SUBSCRIPTION_CATEGORIES:
        scores["subscription"] += 0.35
        category_signal["subscription"] = True
    if cat_lower in _BILL_CATEGORIES:
        scores["recurring_bill"] += 0.35
        category_signal["recurring_bill"] = True

    # --- Rule 4/5: token/phrase-aware, tiered keyword evidence ---------
    sub_strong = _any_token(norm_desc, _SUBSCRIPTION_STRONG_WORDS) or _any_phrase(norm_desc, _SUBSCRIPTION_PHRASES)
    bill_strong = _any_token(norm_desc, _BILL_STRONG_WORDS) or _any_phrase(norm_desc, _BILL_PHRASES)
    bill_weak = (not bill_strong) and _any_phrase(norm_desc, _PROVIDER_WEAK_PHRASES)
    # A bare provider name with NO qualifying phrase is deliberately inert
    # (Rule C): it contributes nothing on its own.

    keyword_signal = {"subscription": False, "recurring_bill": False}
    if sub_strong:
        scores["subscription"] += 0.30
        keyword_signal["subscription"] = True
    if bill_strong:
        scores["recurring_bill"] += 0.30
        keyword_signal["recurring_bill"] = True
    elif bill_weak:
        scores["recurring_bill"] += 0.12
        scores["unknown_recurring"] += 0.05

    # Amount stability: subscriptions are typically flat-fee, bills often
    # fluctuate (usage-based utilities) but are still legitimately recurring.
    # This is supporting evidence only - see the semantic-evidence gate
    # below, which prevents amount/frequency regularity alone from ever
    # producing a confirmed "subscription" classification (Rule 5).
    if cv_amt < 0.03:
        scores["subscription"] += 0.15
    elif cv_amt < 0.15:
        scores["subscription"] += 0.07
        scores["recurring_bill"] += 0.07
    else:
        scores["recurring_bill"] += 0.07

    # Frequency fit
    if freq == "monthly":
        scores["subscription"] += 0.10
        scores["recurring_bill"] += 0.08
    elif freq in ("quarterly", "semiannual", "annual"):
        scores["recurring_bill"] += 0.12
        scores["subscription"] += 0.04
    elif freq in ("weekly", "biweekly"):
        scores["recurring_bill"] += 0.05

    # Regularity
    scores["subscription"] += 0.08 * int_score
    scores["recurring_bill"] += 0.05 * int_score

    # Rule 3: discretionary categories heavily suppress subscription/bill
    # scores even if the merchant happens to recur.
    if cat_lower in _DISCRETIONARY_CATEGORIES:
        scores["subscription"] *= 0.25
        scores["recurring_bill"] *= 0.35
        scores["unknown_recurring"] += 0.20

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_class, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = best_score - second_score

    has_strong_sub_evidence = category_signal["subscription"] or keyword_signal["subscription"]
    has_strong_bill_evidence = category_signal["recurring_bill"] or keyword_signal["recurring_bill"]

    factors = {
        "signals": {
            "category_match": category_signal,
            "keyword_match": keyword_signal,
            "provider_weak_signal": bill_weak,
            "coefficient_of_variation": cv_amt,
            "frequency": freq,
            "interval_consistency": int_score,
        },
        "raw_scores": {k: round(v, 3) for k, v in scores.items()},
        "margin": round(margin, 3),
    }

    # --- Rule 2/5/6: recurrence/amount regularity alone is NEVER enough
    # to confirm "subscription" or "recurring_bill" - real merchant
    # semantics (category or a strong keyword/phrase) are required.
    # Without them, the best this can honestly claim is a possible
    # subscription (if there's at least some ambiguous signal) or
    # unknown_recurring (if there's none at all).
    if best_class == "subscription" and not has_strong_sub_evidence:
        best_class = "possible_subscription" if (bill_weak or best_score >= 0.35) else "unknown_recurring"
    elif best_class == "recurring_bill" and not has_strong_bill_evidence:
        best_class = "unknown_recurring"

    # Even WITH semantic evidence, thin/inconsistent evidence keeps a
    # "subscription" verdict at "possible" rather than confirmed.
    if best_class == "subscription" and (occurrences < 3 or margin < 0.15):
        best_class = "possible_subscription"

    if best_score < 0.30:
        best_class = "unknown_recurring"
        best_score = max(best_score, 0.20)

    return best_class, round(min(best_score, 0.97), 3), factors


# ============================================================
# Phase 8/9: Assemble a single recurring item
# ============================================================

def _build_recurring_item(data, today):
    dates = data["dates"]
    amounts = data["amounts"]

    if len(dates) < 2:
        return None

    stats = _compute_stats(dates, amounts)
    freq = _detect_frequency(stats["median_interval"])
    if freq == "unknown":
        return None

    confidence, confidence_factors = _calculate_confidence(stats, freq, today)
    next_occ = _next_occurrence(freq, stats, today)
    lifecycle_status, payment_status = _determine_statuses(stats, next_occ, confidence_factors)
    status = _legacy_status(lifecycle_status, payment_status)

    norm_desc = data["normalized"]
    # Category: use the most recent non-empty category (categories can be
    # corrected/backfilled over time; the latest is the most trustworthy).
    category = next((c for c in reversed(data["categories"]) if c), "")

    classification, class_confidence, class_factors = _classify_candidate(
        norm_desc, category, data["type"], freq, stats, confidence_factors
    )

    annual_multiplier = _ANNUAL_MULTIPLIER.get(freq, 12)
    annualized_cost = stats["median_amount"] * annual_multiplier
    monthly_equivalent = annualized_cost / 12.0

    price_change = None
    if len(amounts) >= 2:
        prev, curr = amounts[-2], amounts[-1]
        if prev > 0 and curr != prev:
            pct = ((curr - prev) / prev) * 100
            if abs(pct) > 3:
                price_change = {
                    "detected": True,
                    "direction": "increase" if pct > 0 else "decrease",
                    "previous": prev,
                    "current": curr,
                    "change_percent": round(pct, 1),
                }

    item = {
        # --- backward-compatible fields (match the original schema) ---
        "name": data["descriptions"][-1],
        "category": category,
        "frequency": freq,
        "expected_amount": round(stats["median_amount"], 2),
        "monthly_equivalent": round(monthly_equivalent, 2),
        "annualized_cost": round(annualized_cost, 2),
        "next_expected_date": next_occ["next_expected_date"].strftime("%Y-%m-%d"),
        "days_until": next_occ["days_until"],
        "status": status,  # legacy, derived from the fields below - kept for compatibility
        "confidence": confidence,
        "price_change": price_change,

        # --- authoritative status fields (see Phase 6) ---
        "lifecycle_status": lifecycle_status,
        "payment_status": payment_status,

        # --- new, richer fields ---
        "normalized_merchant": norm_desc,
        "transaction_type": data["type"],
        "classification": classification,
        "classification_confidence": class_confidence,
        "classification_factors": class_factors,
        "evidence_level": _evidence_level(stats["occurrences"]),
        "occurrences": stats["occurrences"],
        "first_date": stats["first_date"].strftime("%Y-%m-%d"),
        "last_date": stats["last_date"].strftime("%Y-%m-%d"),
        "calendar_anchor_day": stats["anchor_day"],
        "average_amount": round(stats["avg_amount"], 2),
        "median_amount": round(stats["median_amount"], 2),
        "min_amount": round(stats["min_amount"], 2),
        "max_amount": round(stats["max_amount"], 2),
        "latest_amount": round(amounts[-1], 2),
        "amount_std_dev": round(stats["amount_std"], 2),
        "coefficient_of_variation": stats["coefficient_of_variation"],
        "median_interval_days": round(stats["median_interval"], 1),
        "mean_interval_days": round(stats["mean_interval"], 1),
        "interval_std_dev": round(stats["interval_std"], 1),
        "expected_date_window": {
            "start": next_occ["window_start"].strftime("%Y-%m-%d"),
            "end": next_occ["window_end"].strftime("%Y-%m-%d"),
        },
        "confidence_factors": confidence_factors,
    }
    return item


# ============================================================
# Phase 10: Public service API
# ============================================================

_CLASS_TO_BUCKET = {
    "subscription": "subscriptions",
    "possible_subscription": "possible_subscriptions",
    "recurring_bill": "recurring_bills",
    "recurring_income": "recurring_income",
    "unknown_recurring": "unknown_recurring",
}


def analyze_recurring_transactions(user_id):
    """
    Analyzes a user's transaction history and returns structured recurring
    intelligence, bucketed by classification. Preserves the original
    top-level keys (subscriptions, recurring_bills, recurring_income,
    unknown_recurring) and adds `possible_subscriptions` for weaker-evidence
    subscription candidates.
    """
    rows = _fetch_transactions(user_id)
    groups = _group_candidates(rows)
    today = date.today()

    results = {
        "subscriptions": [],
        "possible_subscriptions": [],
        "recurring_bills": [],
        "recurring_income": [],
        "unknown_recurring": [],
    }

    for data in groups.values():
        item = _build_recurring_item(data, today)
        if item is None:
            continue
        bucket = _CLASS_TO_BUCKET.get(item["classification"])
        if bucket:
            results[bucket].append(item)

    for bucket_items in results.values():
        bucket_items.sort(key=lambda x: x["days_until"])

    return results


def get_recurring_transactions(user_id, recurring_data=None):
    """
    Flat list of every detected recurring candidate, across all
    classifications. Pass `recurring_data` (the result of an earlier
    `analyze_recurring_transactions(user_id)` call) to reuse it instead of
    re-running the full analysis.
    """
    data = recurring_data if recurring_data is not None else analyze_recurring_transactions(user_id)
    flat = []
    for bucket_items in data.values():
        flat.extend(bucket_items)
    flat.sort(key=lambda x: x["days_until"])
    return flat


def get_subscription_summary(user_id, recurring_data=None):
    """
    Summary of subscriptions, with confirmed and possible items kept in
    separate buckets so a low-confidence `possible_subscription` can never
    silently inflate a confirmed financial-health total.

    "Confirmed" means classification == "subscription" AND
    lifecycle_status == "active". A subscription the engine has lost
    track of (possibly_missed / possibly_inactive / inactive) is no
    longer a live financial commitment - counting it toward the
    confirmed monthly/annual cost, the confirmed count, or the
    price-change signals surfaced from this summary would let a lapsed
    or cancelled subscription (with a stale price bump) show up as an
    active cost while the displayed "confirmed subscriptions" count is
    0. The two must always agree, so the active-lifecycle filter is
    applied once here rather than left to every caller to re-derive.

    Pass `recurring_data` to reuse an already-computed analysis result.
    """
    data = recurring_data if recurring_data is not None else analyze_recurring_transactions(user_id)
    confirmed = [s for s in data["subscriptions"] if s.get("lifecycle_status") == "active"]
    possible = data["possible_subscriptions"]

    confirmed_monthly = sum(s["monthly_equivalent"] for s in confirmed)
    confirmed_annual = sum(s["annualized_cost"] for s in confirmed)
    possible_monthly = sum(s["monthly_equivalent"] for s in possible)
    possible_annual = sum(s["annualized_cost"] for s in possible)

    return {
        "subscriptions": confirmed,
        "possible_subscriptions": possible,
        "confirmed_monthly_equivalent": round(confirmed_monthly, 2),
        "possible_monthly_equivalent": round(possible_monthly, 2),
        "confirmed_annualized_cost": round(confirmed_annual, 2),
        "possible_annualized_cost": round(possible_annual, 2),
        # --- additional backward-compatible fields, confirmed-only ---
        "count": len(confirmed),
        "price_changes_detected": [
            s for s in confirmed
            if s.get("price_change") and s["price_change"].get("detected")
        ],
    }


def get_recurring_income(user_id, recurring_data=None):
    """
    Recurring income items with aggregate monthly income estimate.
    Pass `recurring_data` to reuse an already-computed analysis result.
    """
    data = recurring_data if recurring_data is not None else analyze_recurring_transactions(user_id)
    income = data["recurring_income"]
    total_monthly = sum(i["monthly_equivalent"] for i in income)
    return {
        "recurring_income": income,
        "count": len(income),
        "total_monthly_equivalent": round(total_monthly, 2),
    }


def get_upcoming_recurring(user_id, days_ahead=30, recurring_data=None):
    """
    Items expected to occur within `days_ahead` days (or already due),
    sorted soonest-first. Skips `unknown_recurring` items, items with
    `uncertain` status, and items whose lifecycle has decayed past
    "possibly_missed" (possibly_inactive / inactive), since surfacing a
    likely-cancelled commitment as "upcoming" would be misleading.

    Pass `recurring_data` to reuse an already-computed analysis result.
    """
    data = recurring_data if recurring_data is not None else analyze_recurring_transactions(user_id)
    items = get_recurring_transactions(user_id, recurring_data=data)

    excluded_lifecycle = {"possibly_inactive", "inactive", "uncertain"}
    relevant_payment = {"due_soon", "upcoming", "overdue"}

    upcoming = [
        i for i in items
        if i["classification"] != "unknown_recurring"
        and i["lifecycle_status"] not in excluded_lifecycle
        and i["payment_status"] in relevant_payment
        and i["days_until"] <= days_ahead
    ]
    upcoming.sort(key=lambda x: x["days_until"])
    return upcoming


# ============================================================
# Phase 11: Backward compatibility
# ============================================================

def get_recurring_suggestions(user_id, recurring_data=None):
    """
    Preserves the pre-rewrite API:

        get_recurring_suggestions(user_id) -> [
            {"description": ..., "amount": ..., "expected_day": ...}, ...
        ]

    Thin compatibility wrapper around the new recurrence engine - it does
    NOT re-query transaction history itself. It reuses `recurring_data`
    (or runs one `analyze_recurring_transactions` call if none is passed)
    and derives suggestions from the already-computed items, mirroring the
    original behavior: only expense items, skip anything that already has
    a matching transaction this month, and only surface items whose
    expected day-of-month is within 3 days of today.
    """
    today = date.today()
    data = recurring_data if recurring_data is not None else analyze_recurring_transactions(user_id)
    current_month = (today.year, today.month)

    suggestions = []
    seen = set()
    for bucket_name, items in data.items():
        if bucket_name == "recurring_income":
            continue  # original only ever considered type='expense'
        for item in items:
            if item["transaction_type"] != "expense":
                continue

            last_date = date.fromisoformat(item["last_date"])
            if (last_date.year, last_date.month) == current_month:
                continue  # already have a matching transaction this month

            next_expected = date.fromisoformat(item["next_expected_date"])
            expected_day = next_expected.day
            if abs(today.day - expected_day) > 3:
                continue

            key = (item["name"], item["expected_amount"])
            if key in seen:
                continue
            seen.add(key)

            suggestions.append({
                "description": item["name"],
                "amount": item["expected_amount"],
                "expected_day": expected_day,
            })

    return suggestions