# ============================================================
# FILE: ml/recommender.py  [GOAL-AWARE RECOMMENDATIONS]
# ============================================================
# Refinement pass:
#   - Legacy get_recommendations(user_id) is UNCHANGED (same queries,
#     same logic, same return type: list[str]). It is preserved
#     verbatim for existing callers (routes/insights.py, etc.).
#   - get_financial_recommendations(...) is a NEW, separate, structured
#     engine. It does NOT touch the database and does NOT recompute
#     recurring/forecast/risk models itself - it only consumes signals
#     that the recurring engine (services/recurring_service.py), the
#     forecast engine (ml/forecast_model.py) and the financial-health
#     engine (ml/risk_model.py) have already computed, plus whatever
#     goal/category/anomaly signals the orchestration layer supplies.
#
# See the accompanying implementation report for the full field-mapping
# audit, policy thresholds, deduplication strategy and test results.
# ============================================================

from utils.db import get_db
from datetime import datetime


# ============================================================
# ============================================================
#   SECTION A — LEGACY ENGINE (UNCHANGED, DO NOT MODIFY BEHAVIOR)
# ============================================================
# ============================================================

def get_recommendations(user_id: int) -> list[str]:
    """
    Goal-aware recommendation engine.

    Priority order:
      1. Goal-specific savings suggestions (NEW)
      2. Budget-based recommendations
      3. Category spending alerts
      4. Generic healthy-finance tips
    """
    conn = get_db()
    recommendations: list[str] = []

    try:
        # ── Current month totals ─────────────────────────────────
        month_start = datetime.today().strftime("%Y-%m-01")

        row = conn.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN type='income'  THEN amount ELSE 0 END),0) AS income,
                COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END),0) AS expense
            FROM transactions
            WHERE user_id=%s AND date>=%s
        """, (user_id, month_start)).fetchone()

        income  = float(row["income"]  or 0)
        expense = float(row["expense"] or 0)
        surplus = income - expense

        budget_row = conn.execute(
            "SELECT amount FROM budgets WHERE user_id=%s", (user_id,)
        ).fetchone()
        budget = float(budget_row["amount"]) if budget_row else 0.0

        # ── Top spending category ────────────────────────────────
        top_cat_row = conn.execute("""
            SELECT COALESCE(category,'Misc') AS category,
                   SUM(amount) AS total
            FROM transactions
            WHERE user_id=%s AND type='expense' AND date>=%s
            GROUP BY category ORDER BY total DESC LIMIT 1
        """, (user_id, month_start)).fetchone()

        top_cat       = top_cat_row["category"] if top_cat_row else "discretionary spending"
        top_cat_spend = float(top_cat_row["total"]) if top_cat_row else 0.0

        # ── Goals data ───────────────────────────────────────────
        goal_rows = conn.execute("""
            SELECT name, target_amount, saved_amount, target_date
            FROM goals WHERE user_id=%s ORDER BY id ASC
        """, (user_id,)).fetchall()

        # ── Average monthly cash flow (3 months) ─────────────────
        hist = conn.execute("""
            SELECT to_char(date, 'YYYY-MM') AS month,
                   SUM(CASE WHEN type='income'  THEN amount ELSE 0 END) AS inc,
                   SUM(CASE WHEN type='expense' THEN amount ELSE 0 END) AS exp
            FROM transactions WHERE user_id=%s
            GROUP BY month ORDER BY month DESC LIMIT 3
        """, (user_id,)).fetchall()

        avg_surplus = surplus  # fallback = current month
        if hist:
            avg_surplus = max(0.0, sum(
                float(r["inc"] or 0) - float(r["exp"] or 0) for r in hist
            ) / len(hist))

    finally:
        conn.close()

    # ════════════════════════════════════════════════════════════
    # 1. GOAL-SPECIFIC SAVINGS SUGGESTIONS
    # ════════════════════════════════════════════════════════════
    today = datetime.today()
    for g in goal_rows:
        target   = float(g["target_amount"] or 0)
        saved    = float(g["saved_amount"]  or 0)
        name     = g["name"] or "Goal"
        remaining = max(0.0, target - saved)

        if target <= 0 or remaining <= 0:
            continue

        monthly_required = None
        months_left      = None
        target_date_str  = g["target_date"] if "target_date" in g.keys() else None

        if target_date_str:
            try:
                td = datetime.strptime(target_date_str, "%Y-%m-%d")
                ml = max(1, (td.year - today.year) * 12 + (td.month - today.month))
                months_left      = ml
                monthly_required = round(remaining / ml, 0)
            except (ValueError, TypeError):
                pass

        if monthly_required is None and avg_surplus > 0:
            monthly_required = round(avg_surplus * 0.5)  # allocate 50% of surplus
            months_left      = round(remaining / monthly_required, 0) if monthly_required > 0 else None

        if monthly_required and avg_surplus > 0:
            if monthly_required > avg_surplus:
                # Goal is at risk — urgent recommendation
                shortfall = int(monthly_required - avg_surplus)
                recommendations.append(
                    f"⚠️ '{name}' needs ₹{int(monthly_required)}/mo but your surplus is only ₹{int(avg_surplus)}. "
                    f"Reduce {top_cat} by ₹{shortfall} to stay on track."
                )
            elif months_left and months_left <= 3:
                # Deadline approaching — high urgency
                recommendations.append(
                    f"🎯 '{name}' deadline in {int(months_left)} month(s). Save ₹{int(monthly_required)}/mo now — you're ₹{int(remaining)} away."
                )
            else:
                progress_pct = round(saved / target * 100, 0) if target > 0 else 0
                recommendations.append(
                    f"💰 '{name}' is {int(progress_pct)}% funded. Saving ₹{int(monthly_required)}/mo will reach the goal in ~{int(months_left or 0)} months."
                )

    # ════════════════════════════════════════════════════════════
    # 2. BUDGET RECOMMENDATIONS
    # ════════════════════════════════════════════════════════════
    if budget > 0:
        usage_pct = expense / budget * 100
        if usage_pct > 90:
            over = int(expense - budget)
            recommendations.append(
                f"🚨 Budget exceeded by ₹{over}. Pause non-essential spending immediately."
            )
        elif usage_pct > 75:
            remaining_budget = int(budget - expense)
            recommendations.append(
                f"📊 {usage_pct:.0f}% of budget used. ₹{remaining_budget} left — slow down on {top_cat}."
            )

    # ════════════════════════════════════════════════════════════
    # 3. CATEGORY SPENDING ALERTS
    # ════════════════════════════════════════════════════════════
    if income > 0:
        savings_rate = surplus / income * 100
        if savings_rate < 5:
            save_amount = int(expense * 0.10)
            recommendations.append(
                f"📉 Savings rate critically low at {savings_rate:.0f}%. "
                f"A 10% cut in {top_cat} (₹{save_amount}) would meaningfully improve this."
            )
        elif savings_rate < 15:
            recommendations.append(
                f"💡 Savings rate is {savings_rate:.0f}%. Target 20% by trimming ₹{int(expense * 0.05)} from {top_cat}."
            )

    if expense > 0 and top_cat_spend / expense > 0.45:
        recommendations.append(
            f"🔍 {top_cat} is {round(top_cat_spend / expense * 100)}% of total spend — unusually high. Consider a sub-budget for this category."
        )

    # ════════════════════════════════════════════════════════════
    # 4. GENERIC TIPS (shown only if fewer than 3 specific recs)
    # ════════════════════════════════════════════════════════════
    generic_tips = [
        "Automate transfers to savings on payday — the money never hits your spending account.",
        "Apply the 48-hour rule before any purchase above ₹2,000.",
        "Review subscriptions monthly — unused services silently drain budgets.",
        "Use the 50/30/20 rule: 50% needs, 30% wants, 20% savings.",
        "Redirect all cashback and rewards directly to your goals.",
    ]

    while len(recommendations) < 3:
        tip = generic_tips.pop(0) if generic_tips else None
        if not tip:
            break
        recommendations.append(tip)

    return recommendations[:5]


# ============================================================
# ============================================================
#   SECTION B — STRUCTURED, EVIDENCE-DRIVEN ENGINE (NEW)
# ============================================================
# ============================================================
#
# get_financial_recommendations() is a pure function: it takes
# ALREADY-COMPUTED signals from the recurring / forecast / risk
# engines (plus goal / category / anomaly data supplied by the
# orchestration layer) and turns them into ranked, explainable
# recommendation objects. It never queries the database and never
# re-derives a financial figure that an upstream engine is
# authoritative for.
#
# Expected input shapes (field names copied verbatim from the
# supplied source files):
#
#   health:  dict returned by ml.risk_model.calculate_financial_health()
#            Uses: health["cash_flow"]["projected_surplus"],
#                  health["cash_flow"]["projected_savings_rate"],
#                  health["cash_flow"]["current_savings_rate"],
#                  health["budget"]["projected_budget_usage_pct"],
#                  health["budget"]["budget_usage_pct"],
#                  health["budget"]["budget_status"],
#                  health["summary"]["spending_trend_pct"]  (fallback)
#
#   recurring_items: flat list as returned by
#            services.recurring_service.get_recurring_transactions(),
#            i.e. items carrying: name, classification,
#            classification_confidence, lifecycle_status,
#            payment_status, expected_amount, latest_amount,
#            monthly_equivalent, annualized_cost, next_expected_date,
#            days_until, price_change, last_date, median_interval_days,
#            evidence_level, confidence, transaction_type.
#
#   subscription_summary: dict returned by
#            services.recurring_service.get_subscription_summary(),
#            i.e. confirmed_monthly_equivalent, possible_monthly_equivalent,
#            price_changes_detected.
#
#   recurring_bill_burden: optional pre-computed monthly ₹ figure for
#            active recurring_bills (no get_bill_summary() equivalent
#            is exposed by recurring_service.py — see final report,
#            "upstream contract mismatch #1"). If not supplied, it is
#            derived ONLY by summing the `monthly_equivalent` field
#            already present on each recurring_bills item (never by
#            re-deriving it from raw transactions).
#
#   price_changes: optional externally-supplied list of
#            {"name": ..., "price_change": {...}} — merged with
#            price_change info already embedded in recurring_items,
#            de-duplicated by normalized name.
#
#   goal_details: list of dicts shaped like ai_insights._fetch_full_metrics's
#            goal_details: id, name, remaining, monthly_required,
#            months_left, progress_percent, goal_risk, target_date.
#
#   available_surplus: sustainable ₹ surplus goals should be measured
#            against. Falls back to health["cash_flow"]["projected_surplus"].
#
#   category_trends: list of {category, amount, baseline_amount,
#            change_pct, pct_of_spend}.
#
#   anomalies: list of {transaction_id/id, amount, category,
#            expected_amount, deviation, severity, confidence, reason}
#            (shape used by routes/insights.py's /anomaly-transactions).
#
#   income: raw monthly income, needed for burden-vs-income ratios that
#            no single upstream engine already expresses as ₹/₹.
#
#   spending_trend_pct: optional override; falls back to
#            health["summary"]["spending_trend_pct"].
#
#   max_recommendations: desired cap, clamped to [1, 10], default 6.
# ============================================================


# ---- family priority order (Section 16) --------------------------------
_FAMILY_PRIORITY = {
    "cash_flow": 1,
    "budget": 2,
    "goal": 3,
    "recurring": 4,
    "subscription": 4,   # recurring & subscription share tier 4 ("recurring/subscription issue")
    "anomaly": 5,
    "category": 6,
    "savings": 7,
    "positive": 8,
    "general": 9,
}

_SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "positive": 0}


def _normalize_key(text):
    """Stable normalized key for dedup, built the same conservative way
    services.recurring_service._normalize_merchant() does (lowercase,
    strip non-alphanumerics, collapse whitespace) — but reimplemented
    locally rather than importing a private helper, and used ONLY for
    recommendation-key stability, never for reclassifying anything."""
    if not text:
        return "unknown"
    cleaned = "".join(ch if ch.isalnum() else " " for ch in text.lower())
    return "_".join(cleaned.split()) or "unknown"


def _clamp_max_recommendations(max_recommendations):
    """Section 18: accept None/0/negative/huge values, clamp to [1, 10],
    default 6."""
    try:
        n = int(max_recommendations)
    except (TypeError, ValueError):
        return 6
    if n <= 0:
        return 6
    return max(1, min(10, n))


def _make_rec(family, severity, rtype, title, message, evidence, action,
              estimated_impact, key):
    return {
        "priority": _FAMILY_PRIORITY.get(family, 9),
        "severity": severity,
        "type": rtype,
        "title": title,
        "message": message,
        "evidence": evidence,
        "action": action,
        "estimated_impact": estimated_impact,
        "recommendation_key": key,
        "_family": family,  # internal only, stripped before returning
    }


# ------------------------------------------------------------------
# 1. Cash-flow (projected deficit)
# ------------------------------------------------------------------
def _rec_cash_flow(health, category_trends, income=None):
    if not health or "cash_flow" not in health:
        return []

    cf = health["cash_flow"]
    projected_surplus = cf.get("projected_surplus")
    if projected_surplus is None or projected_surplus >= 0:
        return []

    deficit = abs(projected_surplus)

    # Severity: critical if the deficit looks severe relative to the
    # projected expense we have visibility into, otherwise high.
    # (Policy heuristic — documented, not a universal rule.)
    proj_expense = health.get("cash_flow", {}).get("projected_expense")
    severity = "high"
    if proj_expense and proj_expense > 0 and (deficit / proj_expense) > 0.15:
        severity = "critical"

    # Strongest category driver, only if the caller actually supplied
    # a supportable baseline/overage — never invented.
    driver = None
    if category_trends:
        candidates = [
            c for c in category_trends
            if c.get("baseline_amount") is not None
            and c.get("amount") is not None
            and c["amount"] > c["baseline_amount"]
        ]
        if candidates:
            driver = max(candidates, key=lambda c: c["amount"] - c["baseline_amount"])

    # No-income wording (Section 8 fix): "exceed income by ₹X" implies
    # there was income to exceed. When income is 0/unrecorded, say so
    # plainly instead — the risk is still real and still surfaced
    # (severity/evidence below are unchanged), just phrased honestly.
    no_income = income is not None and income == 0
    if no_income:
        expense_for_message = proj_expense if proj_expense is not None else deficit
        message = f"No income is recorded for this period, while projected expenses are ₹{round(expense_for_message)}."
    else:
        message = f"Projected spending will exceed income by ₹{round(deficit)} this month."
    if driver:
        overage = round(driver["amount"] - driver["baseline_amount"])
        message += f" {driver['category']} is the largest contributor, running ₹{overage} above its recent baseline."

    evidence = {"projected_surplus": projected_surplus, "source": "risk_model.calculate_financial_health.cash_flow"}
    if no_income:
        evidence["no_income"] = True
    if driver:
        evidence["top_driver_category"] = driver["category"]
        evidence["top_driver_overage"] = round(driver["amount"] - driver["baseline_amount"])

    action = "Review upcoming expenses this month and cut non-essential spending until the projected surplus turns positive."
    return [_make_rec(
        "cash_flow", severity, "cash_flow_deficit",
        "Projected cash-flow deficit",
        message, evidence, action, f"-₹{round(deficit)} projected this month",
        "cash_flow_deficit",
    )]


# ------------------------------------------------------------------
# 2. Budget
# ------------------------------------------------------------------
def _rec_budget(health):
    if not health or "budget" not in health:
        return []

    b = health["budget"]
    status = b.get("budget_status")
    proj_pct = b.get("projected_budget_usage_pct")
    cur_pct = b.get("budget_usage_pct")

    if status == "no_budget":
        return [_make_rec(
            "budget", "low", "no_budget",
            "No budget set",
            "Budget adherence can't currently be evaluated because no budget is configured.",
            {"budget_status": "no_budget"},
            "Set a monthly budget to unlock budget-adherence tracking.",
            None,
            "no_budget_configured",
        )]

    if proj_pct is not None and proj_pct > 100:
        return [_make_rec(
            "budget", "high", "budget_breach",
            "Projected budget breach",
            f"Your projected spending is {round(proj_pct)}% of budget — on track to go over.",
            {"projected_budget_usage_pct": proj_pct, "budget_usage_pct": cur_pct},
            "Cut discretionary spending for the rest of the month to stay within budget.",
            f"{round(proj_pct - 100)}% over budget projected",
            "budget_projected_breach",
        )]

    if cur_pct is not None and 75 <= cur_pct <= 90:
        return [_make_rec(
            "budget", "medium", "budget_warning",
            "Approaching budget limit",
            f"You've used {round(cur_pct)}% of your budget so far this month.",
            {"budget_usage_pct": cur_pct},
            "Slow down discretionary spending for the rest of the month.",
            None,
            "budget_approaching_limit",
        )]

    if cur_pct is not None and cur_pct < 70:
        return [_make_rec(
            "budget", "positive", "budget_on_track",
            "Budget on track",
            f"Only {round(cur_pct)}% of your budget used so far — you're in good shape.",
            {"budget_usage_pct": cur_pct},
            "Keep it up.",
            None,
            "budget_on_track",
        )]

    return []


# ------------------------------------------------------------------
# 3. Goals
# ------------------------------------------------------------------
def _rec_goals(goal_details, available_surplus):
    if not goal_details or available_surplus is None:
        return []

    at_risk = []
    for g in goal_details:
        mr = g.get("monthly_required")
        if mr is None:
            continue
        if mr > available_surplus:
            at_risk.append(g)

    if not at_risk:
        return []

    # Avoid flooding — surface at most the 2 most urgent (largest shortfall).
    at_risk.sort(key=lambda g: g["monthly_required"], reverse=True)
    out = []
    for g in at_risk[:2]:
        shortfall = round(g["monthly_required"] - available_surplus)
        name = g.get("name", "Goal")
        out.append(_make_rec(
            "goal", "high", "goal_risk",
            f"'{name}' at risk",
            f"'{name}' needs ₹{round(g['monthly_required'])}/month but your available surplus is ₹{round(available_surplus)} — a ₹{shortfall} shortfall.",
            {
                "goal_id": g.get("id"),
                "monthly_required": g["monthly_required"],
                "available_surplus": available_surplus,
            },
            f"Reduce discretionary spending by ₹{shortfall}/month or adjust the goal's target date.",
            f"₹{shortfall}/month shortfall",
            f"goal_risk_{_normalize_key(name)}",
        ))
    return out


# ------------------------------------------------------------------
# 4/5/6. Recurring / subscriptions (price changes, inactive, overdue,
#         possible subscriptions, burden)
# ------------------------------------------------------------------
def _merge_price_changes(recurring_items, price_changes):
    """De-duplicate price-change signals, keeping the full item dictionary 
    to preserve classification and lifecycle status."""
    merged = {}

    for item in (recurring_items or []):
        pc = item.get("price_change")
        if pc and pc.get("detected"):
            key = _normalize_key(item.get("name"))
            merged[key] = item

    for pc_entry in (price_changes or []):
        pc = pc_entry.get("price_change")
        if pc and pc.get("detected"):
            key = _normalize_key(pc_entry.get("name"))
            if key not in merged:
                merged[key] = pc_entry
            # If already in merged, we trust the recurring_items dictionary
            # because it carries the required classification & lifecycle state.

    return list(merged.values())


def _rec_price_changes(recurring_items, price_changes):
    """
    Generates price-change alerts strictly for active, confirmed subscriptions/bills.
    Filters out negligible changes, ranks by absolute impact, capped at 2 recommendations.
    """
    out = []
    valid_increases = []

    for item in _merge_price_changes(recurring_items, price_changes):
        pc = item.get("price_change")
        if not pc or not pc.get("detected") or pc.get("direction") != "increase":
            continue

        # 1. Strict Classification Requirement
        classification = item.get("classification")
        if classification not in ("subscription", "recurring_bill"):
            continue

        # 2. Strict Lifecycle Requirement
        if item.get("lifecycle_status") != "active":
            continue

        # 3. Absolute Change / Percentage Filter
        prev = pc.get("previous")
        curr = pc.get("current")
        pct = pc.get("change_percent")
        
        abs_change = pc.get("absolute_change")
        if abs_change is None and prev is not None and curr is not None:
            abs_change = abs(curr - prev)
        elif abs_change is None:
            abs_change = 0

        # Ignore tiny changes (<= ₹5) unless the percentage is unusually high (> 10%)
        if abs_change <= 5 and (pct is None or pct <= 10):
            continue
            
        item["_abs_change"] = abs_change
        item["_pct_change"] = pct if pct is not None else 0
        valid_increases.append(item)

    # 4. Limit and Sort by absolute impact first, then percentage
    valid_increases.sort(key=lambda x: (x["_abs_change"], x["_pct_change"]), reverse=True)

    for item in valid_increases[:2]:
        pc = item["price_change"]
        name = item.get("name", "Unknown")
        prev = pc.get("previous")
        curr = pc.get("current")
        pct = pc.get("change_percent")
        
        family = "subscription" if item.get("classification") == "subscription" else "recurring"

        out.append(_make_rec(
            family, "medium", "price_increase",
            f"Price increase: {name}",
            f"'{name}' increased from ₹{prev} to ₹{curr} ({pct:+}%).",
            {"previous": prev, "current": curr, "change_percent": pct},
            f"Review whether '{name}' is still worth the new price.",
            f"₹{round(curr - prev)}/cycle increase" if prev is not None and curr is not None else None,
            f"price_increase_{_normalize_key(name)}",
        ))
        
    return out


def _rec_lifecycle_and_payment(recurring_items):
    """Section 4/5: inactive/missed lifecycle + overdue payment
    handling, using lifecycle_status and payment_status as SEPARATE
    axes exactly as recurring_service.py defines them."""
    out = []
    if not recurring_items:
        return out

    for item in recurring_items:
        classification = item.get("classification")
        if classification not in ("subscription", "recurring_bill"):
            continue  # possible_subscription / unknown_recurring / income handled elsewhere

        name = item.get("name", "This recurring payment")
        lifecycle = item.get("lifecycle_status")
        payment = item.get("payment_status")

        if payment == "overdue":
            days_until = item.get("days_until")
            out.append(_make_rec(
                "recurring", "medium", "payment_overdue",
                f"Payment may be overdue: {name}",
                f"FinTrust expected a payment for '{name}' but it has not been seen "
                f"({abs(days_until) if days_until is not None else 'several'} days past the expected date).",
                {"lifecycle_status": lifecycle, "payment_status": payment, "days_until": days_until},
                f"Check whether '{name}' was paid or billed through a different account.",
                None,
                f"payment_overdue_{_normalize_key(name)}",
            ))
            # An overdue item can ALSO be lifecycle-flagged below if it has
            # decayed further (possibly_missed/possibly_inactive/inactive) —
            # both are distinct concerns (timing vs. commitment), so we do
            # not `continue` here.

        if lifecycle in ("possibly_missed", "possibly_inactive", "inactive"):
            out.append(_make_rec(
                "recurring", "low", "lifecycle_review",
                f"Review recurring status: {name}",
                "FinTrust has not seen this recurring payment for longer than expected. "
                f"Review whether '{name}' is still active.",
                {"lifecycle_status": lifecycle, "payment_status": payment, "last_date": item.get("last_date")},
                f"Confirm whether '{name}' is still in use; cancel it if not.",
                None,
                f"lifecycle_review_{_normalize_key(name)}",
            ))

    return out


def _rec_possible_subscriptions(recurring_items):
    out = []
    if not recurring_items:
        return out
    for item in recurring_items:
        if item.get("classification") != "possible_subscription":
            continue
        name = item.get("name", "an unrecognized charge")
        conf = item.get("classification_confidence")
        out.append(_make_rec(
            "subscription", "low", "possible_subscription",
            "Possible recurring subscription detected",
            f"FinTrust detected a possible recurring subscription: {name}. Verify whether you recognize it.",
            {"classification": "possible_subscription", "classification_confidence": conf},
            f"Check your statements to confirm whether '{name}' is a subscription you recognize.",
            None,
            f"possible_subscription_{_normalize_key(name)}",
        ))
    return out


def _rec_subscription_burden(subscription_summary, income):
    if not subscription_summary or not income or income <= 0:
        return []
    confirmed_monthly = subscription_summary.get("confirmed_monthly_equivalent")
    if confirmed_monthly is None:
        return []
    ratio = confirmed_monthly / income * 100

    # Policy heuristic (FinTrust-specific, not a universal financial rule):
    #   <10%  -> no alert
    #   10-20% -> medium
    #   >20%  -> high
    if ratio < 10:
        return []
    severity = "high" if ratio > 20 else "medium"
    return [_make_rec(
        "subscription", severity, "subscription_burden",
        "Subscription spending is a notable share of income",
        f"Confirmed subscriptions cost ₹{round(confirmed_monthly)}/month — {round(ratio)}% of your income.",
        {"confirmed_monthly_equivalent": confirmed_monthly, "income": income, "ratio_pct": round(ratio, 1)},
        "Review your subscription list and cancel anything you no longer use.",
        f"{round(ratio)}% of income",
        "subscription_burden",
    )]


def _rec_recurring_bill_burden(recurring_items, recurring_bill_burden, income):
    if not income or income <= 0:
        return []

    burden = recurring_bill_burden
    if burden is None and recurring_items:
        # Fallback: sum the already-computed monthly_equivalent field on
        # recurring_bill items only. This aggregates authoritative
        # per-item figures the recurring engine already produced; it does
        # NOT re-derive recurrence from raw transactions.
        bills = [i for i in recurring_items
                 if i.get("classification") == "recurring_bill"
                 and i.get("lifecycle_status") == "active"]
        if not bills:
            return []
        burden = sum(i.get("monthly_equivalent", 0) or 0 for i in bills)

    if burden is None:
        return []

    ratio = burden / income * 100
    # Section 8 policy thresholds (FinTrust product heuristics):
    #   <=30% -> no alert
    #   >30%  -> medium
    #   >50%  -> high
    if ratio <= 30:
        return []
    severity = "high" if ratio > 50 else "medium"
    return [_make_rec(
        "recurring", severity, "recurring_bill_burden",
        "Fixed recurring bills are a large share of income",
        f"Recurring bills total ₹{round(burden)}/month — {round(ratio)}% of your income.",
        {"recurring_bill_burden": burden, "income": income, "ratio_pct": round(ratio, 1)},
        "Look for ways to reduce or renegotiate your largest fixed bills.",
        f"{round(ratio)}% of income",
        "recurring_bill_burden",
    )]


# ------------------------------------------------------------------
# 7. Anomalies
# ------------------------------------------------------------------
def _rec_anomalies(anomalies):
    """At most ONE aggregate anomaly recommendation.

    The detailed anomaly panel already lists every individual outlier
    transaction, so the Action Plan must not repeat each one as its own
    recommendation (that's how 5-10 "Unusual transaction detected"
    entries used to flood the Action Plan and crowd out everything
    else). This produces a single, evidence-backed summary instead.
    """
    if not anomalies:
        return []

    count = len(anomalies)
    amounts = [a.get("amount") for a in anomalies if a.get("amount") is not None]
    highest = max(amounts) if amounts else None
    affected_categories = sorted({a.get("category") for a in anomalies if a.get("category")})

    # Severity reflects the worst individual anomaly present, so a
    # single critical outlier still surfaces this recommendation near
    # the top — without generating a recommendation per anomaly.
    severity_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    worst_severity, worst_rank = "low", 0
    for a in anomalies:
        s = a.get("severity")
        s = s if s in severity_rank else "low"
        if severity_rank[s] > worst_rank:
            worst_rank, worst_severity = severity_rank[s], s

    message = (
        f"You have {count} transaction{'s' if count != 1 else ''} that "
        f"{'are' if count != 1 else 'is'} significantly above your normal category spending. "
        "Review the largest ones to make sure they were expected."
    )

    evidence = {
        "anomaly_count": count,
        "highest_anomaly": round(highest) if highest is not None else None,
        "affected_categories": affected_categories,
    }

    return [_make_rec(
        "anomaly", worst_severity, "anomaly_review_aggregate",
        "Review unusual transactions",
        message,
        evidence,
        "Open the anomaly panel and check the largest transactions first.",
        None,
        "anomaly_review_aggregate",
    )]


# ------------------------------------------------------------------
# 8. Category trends
# ------------------------------------------------------------------
def _rec_category(category_trends):
    out = []
    if not category_trends:
        return out
    for c in category_trends:
        category = c.get("category")
        if not category:
            continue
        change_pct = c.get("change_pct")
        pct_of_spend = c.get("pct_of_spend")

        if change_pct is not None and change_pct >= 25:
            out.append(_make_rec(
                "category", "medium", "category_increase",
                f"{category} spending increased",
                f"{category} spending is up {round(change_pct)}% versus its baseline.",
                {"category": category, "change_pct": change_pct,
                 "amount": c.get("amount"), "baseline_amount": c.get("baseline_amount")},
                f"Review recent {category} transactions for anything unexpected.",
                f"+{round(change_pct)}%",
                f"category_increase_{_normalize_key(category)}",
            ))

        if pct_of_spend is not None and pct_of_spend > 40:
            severity = "medium" if pct_of_spend > 55 else "low"
            out.append(_make_rec(
                "category", severity, "category_concentration",
                f"{category} dominates spending",
                f"{category} makes up {round(pct_of_spend)}% of your spending this month.",
                {"category": category, "pct_of_spend": pct_of_spend},
                f"Consider a dedicated sub-budget for {category}.",
                f"{round(pct_of_spend)}% of spend",
                f"category_concentration_{_normalize_key(category)}",
            ))
    return out


# ------------------------------------------------------------------
# 9. Savings
# ------------------------------------------------------------------
def _rec_savings(health, category_trends):
    if not health or "cash_flow" not in health:
        return []
    cf = health["cash_flow"]
    rate = cf.get("projected_savings_rate")
    basis = "projected_savings_rate"
    if rate is None:
        rate = cf.get("current_savings_rate")
        basis = "current_savings_rate"
    if rate is None:
        return []

    # Section 12 policy thresholds:
    #   <5%    -> high
    #   5-10%  -> medium
    #   10-20% -> low
    #   20%+   -> positive
    if rate < 5:
        severity, rtype, title = "high", "savings_critical", "Savings rate critically low"
    elif rate < 10:
        severity, rtype, title = "medium", "savings_low", "Savings rate is low"
    elif rate < 20:
        severity, rtype, title = "low", "savings_moderate", "Savings rate could improve"
    else:
        severity, rtype, title = "positive", "savings_healthy", "Savings rate is healthy"

    message = f"Your {basis.replace('_', ' ')} is {round(rate)}%."
    evidence = {basis: rate}
    estimated_impact = None

    # If low and a category trend gives a defensible reduction, compute
    # the resulting new rate mathematically (never invent the reduction).
    if severity in ("high", "medium") and category_trends:
        candidates = [c for c in category_trends
                      if c.get("baseline_amount") is not None and c.get("amount") is not None
                      and c["amount"] > c["baseline_amount"]]
        if candidates:
            driver = max(candidates, key=lambda c: c["amount"] - c["baseline_amount"])
            reduction = driver["amount"] - driver["baseline_amount"]
            message += f" Trimming {driver['category']} back toward its recent baseline (~₹{round(reduction)}) is a lever worth considering."
            evidence["category_reduction_opportunity"] = round(reduction)
            evidence["category_reduction_source"] = driver["category"]

    return [_make_rec(
        "savings", severity, rtype, title, message, evidence,
        "Increase your savings rate by trimming discretionary spending." if severity != "positive"
        else "Keep up the strong savings habit.",
        estimated_impact,
        "savings_rate",
    )]


# ------------------------------------------------------------------
# 10. Overall spending trend (positive reinforcement)
# ------------------------------------------------------------------
def _rec_spending_trend(spending_trend_pct):
    if spending_trend_pct is None or spending_trend_pct >= -5:
        return []
    return [_make_rec(
        "positive", "positive", "spending_trend_down",
        "Spending trending down",
        f"Your overall spending is down {round(abs(spending_trend_pct))}% recently — nice work.",
        {"spending_trend_pct": spending_trend_pct},
        "Keep it up.",
        None,
        "spending_trend_down",
    )]


# ------------------------------------------------------------------
# Generic fallback tips
# ------------------------------------------------------------------
_GENERIC_TIPS = [
    ("Automate savings on payday", "Automate transfers to savings on payday so the money never hits your spending account."),
    ("Use the 48-hour rule", "Apply the 48-hour rule before any purchase above ₹2,000."),
    ("Audit subscriptions monthly", "Review subscriptions monthly — unused services silently drain budgets."),
    ("Try the 50/30/20 rule", "Use the 50/30/20 rule: 50% needs, 30% wants, 20% savings."),
]


def _rec_generic(count_needed):
    out = []
    for title, message in _GENERIC_TIPS[:count_needed]:
        out.append(_make_rec(
            "general", "low", "generic_tip", title, message,
            {"source": "generic_tip"}, "Consider adopting this habit.", None,
            f"generic_{_normalize_key(title)}",
        ))
    return out


# ------------------------------------------------------------------
# Semantic family suppression (Section 17)
# ------------------------------------------------------------------
def _apply_semantic_dedup(recs):
    """Suppress recommendations that would describe the same underlying
    root cause as a higher-priority one already present. Kept narrow and
    explicit rather than a general fuzzy grouping, so unrelated problems
    are never accidentally collapsed:

      - If a critical/high cash-flow deficit recommendation is present,
        the redundant "savings rate" recommendation (which is describing
        the same shortfall from a different angle) is dropped, UNLESS
        the savings entry carries a concrete category-reduction lever
        that the cash-flow entry does not already surface.
    """
    families = {r["_family"] for r in recs}
    if "cash_flow" not in families:
        return recs

    cash_flow_critical = any(
        r["_family"] == "cash_flow" and r["severity"] in ("critical", "high") for r in recs
    )
    if not cash_flow_critical:
        return recs

    out = []
    for r in recs:
        if r["_family"] == "savings" and r["type"] in ("savings_critical", "savings_low") \
                and "category_reduction_opportunity" not in r["evidence"]:
            continue  # redundant with the cash-flow deficit recommendation
        out.append(r)
    return out


def get_financial_recommendations(
    user_id=None,
    income=None,
    health=None,
    recurring_items=None,
    subscription_summary=None,
    recurring_bill_burden=None,
    price_changes=None,
    goal_details=None,
    available_surplus=None,
    category_trends=None,
    anomalies=None,
    spending_trend_pct=None,
    max_recommendations=6,
):
    """
    Structured, evidence-driven recommendation engine.

    Consumes already-computed signals from the recurring / forecast /
    risk-model engines (and caller-supplied goal / category / anomaly
    data). Does not query the database, does not recompute financial
    models, and skips any recommendation it cannot ground in supplied
    evidence.

    Returns:
        {"status": "ok", "recommendations": [...], "count": N}
        or
        {"status": "insufficient_data", "recommendations": [], "count": 0}
    """
    max_recommendations = _clamp_max_recommendations(max_recommendations)

    # Resolve fallbacks that legitimately live in health["summary"]
    # rather than being recomputed.
    if spending_trend_pct is None and health:
        spending_trend_pct = health.get("summary", {}).get("spending_trend_pct")

    if available_surplus is None and health:
        available_surplus = health.get("cash_flow", {}).get("projected_surplus")

    has_any_input = any([
        health, recurring_items, subscription_summary, goal_details,
        category_trends, anomalies, income,
    ])
    if not has_any_input:
        return {"status": "insufficient_data", "recommendations": [], "count": 0}

    all_recs = []
    all_recs += _rec_cash_flow(health, category_trends, income=income)
    all_recs += _rec_budget(health)
    all_recs += _rec_goals(goal_details, available_surplus)
    all_recs += _rec_price_changes(recurring_items, price_changes)
    all_recs += _rec_lifecycle_and_payment(recurring_items)
    all_recs += _rec_possible_subscriptions(recurring_items)
    all_recs += _rec_subscription_burden(subscription_summary, income)
    all_recs += _rec_recurring_bill_burden(recurring_items, recurring_bill_burden, income)
    all_recs += _rec_anomalies(anomalies)
    all_recs += _rec_category(category_trends)
    all_recs += _rec_savings(health, category_trends)
    all_recs += _rec_spending_trend(spending_trend_pct)

    # ---- recommendation_key dedup (exact) ----
    seen_keys = set()
    deduped = []
    for r in all_recs:
        if r["recommendation_key"] in seen_keys:
            continue
        seen_keys.add(r["recommendation_key"])
        deduped.append(r)

    # ---- semantic family suppression ----
    deduped = _apply_semantic_dedup(deduped)

    if not deduped:
        # No evidence-backed issues found — don't force negative
        # warnings; offer generic tips only if the caller had ANY
        # data at all (a "healthy user" case), otherwise insufficient.
        if has_any_input:
            generic = _rec_generic(min(3, max_recommendations))
            return _finalize(generic, max_recommendations)
        return {"status": "insufficient_data", "recommendations": [], "count": 0}

    # ---- sort: severity first (so critical items are never buried),
    #      then family priority tier, keeping stable relative order ----
    deduped.sort(key=lambda r: (-_SEVERITY_RANK.get(r["severity"], 0), r["priority"]))

    # ---- fill remaining slots with generic tips only if there's room
    #      AND the specific list is thin (Section 21: don't dilute) ----
    if len(deduped) < 2 and len(deduped) < max_recommendations:
        deduped += _rec_generic(max_recommendations - len(deduped))

    return _finalize(deduped, max_recommendations)


def _finalize(recs, max_recommendations):
    trimmed = recs[:max_recommendations]
    out = []
    for i, r in enumerate(trimmed, start=1):
        clean = dict(r)
        clean.pop("_family", None)
        clean["rank"] = i
        out.append(clean)
    return {"status": "ok", "recommendations": out, "count": len(out)}