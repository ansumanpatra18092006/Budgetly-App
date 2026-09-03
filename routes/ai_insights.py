# ============================================================
# FILE: routes/ai_insights.py  [UNIFIED FINANCIAL INTELLIGENCE]
# ============================================================

from flask import Blueprint, jsonify, session, request
from utils.db import get_db
from utils.decorators import login_required
from datetime import datetime, date, timedelta

from services.recurring_service import (
    analyze_recurring_transactions,
    get_recurring_transactions,
    get_subscription_summary,
    get_recurring_income,
    get_upcoming_recurring,
)
from ml.forecast_model import predict_next_month_comprehensive
from services.goal_pressure import calculate_goal_pressure
from services.financial_health_snapshot import compute_health_for_context
from ml.recommender import get_financial_recommendations
from ml.anomaly_model import detect_category_anomalies

ai_insights_bp = Blueprint("ai_insights", __name__)


def _safe_close(conn):
    try:
        conn.close()
    except Exception:
        pass


def _get_month_bounds():
    today = datetime.today()
    cur_start = today.strftime("%Y-%m-01")
    if today.month == 1:
        prev_start = f"{today.year-1}-12-01"
        prev_end   = f"{today.year-1}-12-31"
    else:
        first_of_cur = datetime(today.year, today.month, 1)
        last_of_prev = first_of_cur - timedelta(days=1)
        prev_start   = last_of_prev.strftime("%Y-%m-01")
        prev_end     = last_of_prev.strftime("%Y-%m-%d")
    return cur_start, prev_start, prev_end


def _current_month_end_exclusive():
    today = datetime.today()
    if today.month == 12:
        return f"{today.year + 1}-01-01"
    return f"{today.year:04d}-{today.month + 1:02d}-01"


def _parse_target_date(value):
    """
    Normalizes a goal's target_date into a plain `date`, regardless of
    what shape it comes back as from PostgreSQL / the driver.

    ROOT-CAUSE NOTE (fixed): the previous code assumed target_date was
    always a "%Y-%m-%d" string and called
    `datetime.strptime(target_date_str, "%Y-%m-%d")` directly on it.
    psycopg2 returns DATE columns as `datetime.date` objects, not
    strings — strptime() on a `date` object raises TypeError, which was
    caught by a bare `except (ValueError, TypeError): pass` and left
    monthly_required/months_left as None. That's why a goal with a
    perfectly valid target_date (24 Aug 2027) was showing "Required
    ₹0.00/month": the date silently failed to parse, not because the
    goal had no deadline.

    Supports: datetime.date, datetime.datetime, ISO date strings
    ("YYYY-MM-DD" and full ISO timestamps, with or without a "Z"
    suffix), and None/empty (returns None — genuinely no deadline).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def _income_stability_from_history(rows):
    """Return a bounded income-stability score from recent monthly income."""
    values = [float(r["inc"] or 0) for r in rows]
    if len(values) < 2 or max(values) <= 0:
        return None
    mean = sum(values) / len(values)
    if mean <= 0:
        return None
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    cv = (variance ** 0.5) / mean
    return round(max(0.0, min(100.0, 100.0 / (1.0 + cv))), 1)


def _fetch_full_metrics(conn, user_id):
    """
    Unified metrics layer — single source of truth for all intelligence
    modules: insights, risk, roadmap, transaction preview, recommendations.

    Added in this version:
      - goal_pressure  : urgency index (0–100). High = goals falling behind.
      - goal_details   : per-goal breakdown with monthly_required, months_left.
      - combined_risk  : blended risk from expense ratio + goal pressure.
    """
    cur_start, prev_start, prev_end = _get_month_bounds()
    cur_end = _current_month_end_exclusive()
    today = datetime.today()

    # ── Current month income / expense ──────────────────────────
    cur = conn.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN type='income'  THEN amount ELSE 0 END),0) AS income,
            COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END),0) AS expense
        FROM transactions WHERE user_id=%s AND date>=%s AND date<%s AND status <> 'failed'
    """, (user_id, cur_start, cur_end)).fetchone()

    # ── Previous month ───────────────────────────────────────────
    prev = conn.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN type='income'  THEN amount ELSE 0 END),0) AS income,
            COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END),0) AS expense
        FROM transactions WHERE user_id=%s AND date>=%s AND date<=%s AND status <> 'failed'
    """, (user_id, prev_start, prev_end)).fetchone()

    # ── Budget ───────────────────────────────────────────────────
    budget_row = conn.execute(
        "SELECT COALESCE(amount,0) AS amount FROM budgets WHERE user_id=%s",
        (user_id,)
    ).fetchone()

    # ── Top spending category ────────────────────────────────────
    top_cat = conn.execute("""
        SELECT COALESCE(category,'Misc') AS category, SUM(amount) AS total
        FROM transactions WHERE user_id=%s AND type='expense' AND date>=%s AND date<%s AND status <> 'failed'
        GROUP BY category ORDER BY total DESC LIMIT 1
    """, (user_id, cur_start, cur_end)).fetchone()

    # ── Goals with full detail ───────────────────────────────────
    goal_rows = conn.execute(
        """SELECT id, name, target_amount, saved_amount, category, target_date
           FROM goals WHERE user_id=%s ORDER BY id ASC""",
        (user_id,)
    ).fetchall()

    # ── Monthly cash flow (last 3 months for forecasting) ────────
    hist_rows = conn.execute("""
        SELECT to_char(date,'YYYY-MM') AS month,
               SUM(CASE WHEN type='income'  THEN amount ELSE 0 END) AS inc,
               SUM(CASE WHEN type='expense' THEN amount ELSE 0 END) AS exp
        FROM transactions WHERE user_id=%s AND status <> 'failed'
        GROUP BY month ORDER BY month DESC LIMIT 3
    """, (user_id,)).fetchall()

    # ── Anomaly count (last 30 days) ─────────────────────────────
    recent_amounts_row = conn.execute("""
        SELECT amount FROM transactions
        WHERE user_id=%s AND type='expense' AND status <> 'failed' ORDER BY date ASC
    """, (user_id,)).fetchall()

    # ═══════════════════════════════════════════════════════════
    # COMPUTE BASE METRICS
    # ═══════════════════════════════════════════════════════════
    # Safely convert PostgreSQL Decimals to float before calculations
    income    = float(cur["income"] or 0)
    expense   = float(cur["expense"] or 0)
    surplus   = income - expense
    budget    = float(budget_row["amount"] or 0) if budget_row else 0.0
    p_expense = float(prev["expense"] or 0)
    p_income  = float(prev["income"] or 0)
    income_stability = _income_stability_from_history(hist_rows)

    savings_rate    = round(surplus / income * 100, 1)          if income  > 0 else 0.0
    budget_used_pct = round(expense / budget * 100, 1)          if budget  > 0 else 0.0
    # No previous-month spend means there is no defensible percentage change.
    # Returning 0 here used to create a fake "stable" trend and, on an
    # otherwise empty account, supplied the trend component with a full score.
    expense_change  = round((expense - p_expense) / p_expense * 100, 1) if p_expense > 0 else None

    if today.month == 12:
        next_month = datetime(today.year + 1, 1, 1)
    else:
        next_month = datetime(today.year, today.month + 1, 1)
    days_in_month = (next_month - datetime(today.year, today.month, 1)).days
    days_passed = min(max(today.day, 1), days_in_month)
    days_left = max(days_in_month - days_passed, 0)
    daily_burn = expense / days_passed if days_passed > 0 else 0.0

    top_cat_name  = top_cat["category"] if top_cat else "N/A"
    top_cat_total = float(top_cat["total"] or 0) if top_cat else 0.0
    top_cat_pct   = round(top_cat_total / expense * 100, 1) if top_cat and expense > 0 else 0.0

    # ── Average monthly cash flow (3-month history) ──────────────
    if hist_rows:
        avg_income_hist  = sum(float(r["inc"] or 0) for r in hist_rows) / len(hist_rows)
        avg_expense_hist = sum(float(r["exp"] or 0) for r in hist_rows) / len(hist_rows)
        avg_monthly_surplus = max(0.0, avg_income_hist - avg_expense_hist)
    else:
        avg_monthly_surplus = max(0.0, surplus)

    # ═══════════════════════════════════════════════════════════
    # PER-GOAL DETAIL — monthly_required, months_left, goal_risk.
    # total_target/total_saved are kept for the progress% fields
    # elsewhere; they no longer feed into goal_pressure (see below —
    # pressure is affordability-based, not unfunded-percentage-based).
    # ═══════════════════════════════════════════════════════════
    total_target = 0.0
    total_saved  = 0.0
    goal_details = []

    for row in goal_rows:
        g_id     = row["id"]
        g_name   = row["name"]
        target   = float(row["target_amount"] or 0)
        saved    = float(row["saved_amount"]  or 0)
        remaining = max(0.0, target - saved)
        progress_pct = round(saved / target * 100, 1) if target > 0 else 0.0

        total_target += target
        total_saved  += saved

        # Per-goal monthly requirement
        monthly_required = None
        months_left_goal = None
        goal_risk        = "low"
        target_date_raw  = row["target_date"] if "target_date" in row.keys() else None
        target_date_str  = target_date_raw.isoformat() if hasattr(target_date_raw, "isoformat") else target_date_raw

        # A goal that's already fully funded (remaining <= 0) needs no
        # further monthly contribution and can't be "at risk" — a
        # completed goal must not contribute to total_monthly_required
        # or pick up a stray goal_risk just because its deadline
        # happens to be close.
        if remaining <= 0:
            monthly_required = 0.0
            months_left_goal = None
            goal_risk = "low"
        else:
            td = _parse_target_date(target_date_raw)
            if td is not None:
                ml = max(1, (td.year - today.year) * 12 + (td.month - today.month))
                months_left_goal = ml
                monthly_required = round(remaining / ml, 2)

                overdue = td < today.date()
                if overdue or monthly_required > avg_monthly_surplus * 1.5:
                    goal_risk = "high"
                elif monthly_required > avg_monthly_surplus or ml <= 2:
                    goal_risk = "medium"
            elif avg_monthly_surplus > 0:
                months_left_goal = round(remaining / avg_monthly_surplus, 1)
                monthly_required = round(avg_monthly_surplus, 2)

        goal_details.append({
            "id":               g_id,
            "name":             g_name,
            "target_amount":    target,
            "saved_amount":     saved,
            "remaining":        round(remaining, 2),
            "progress_percent": progress_pct,
            "monthly_required": monthly_required,
            "months_left":      months_left_goal,
            "target_date":      target_date_str,
            "goal_risk":        goal_risk,
            "category":         row["category"],
        })

    # ═══════════════════════════════════════════════════════════
    # GOAL PRESSURE — affordability, not unfunded-percentage
    #
    # ROOT-CAUSE NOTE (fixed): pressure used to be
    #   ((total_target - total_saved) / total_target) * 100
    # i.e. literally "what % of the goal is still unfunded". That's a
    # PROGRESS metric, not a pressure metric — it produced "93/100 HIGH
    # PRESSURE" for a goal that was easily affordable (₹7,750/mo
    # required against ₹17,146/mo surplus) purely because 93% of the
    # rupee amount hadn't been saved yet. A goal 5% funded with a
    # distant deadline and low required contribution is not "under
    # pressure"; a goal 90% funded but due next week with no surplus
    # to cover the rest is. Pressure = can-they-afford-it, which is
    # coverage_ratio = available surplus / required monthly contribution.
    # ═══════════════════════════════════════════════════════════
    total_monthly_required = sum(
        float(g["monthly_required"] or 0) for g in goal_details if g["remaining"] > 0
    )

    # Canonical goal pressure: affordability of required monthly contributions
    # against sustainable current cash headroom. This is shared with purchase
    # preview so a proposed expense can change pressure instead of merely
    # repeating the pre-transaction value.
    goal_pressure = calculate_goal_pressure(
        goal_details,
        avg_monthly_surplus,
        current_surplus=surplus,
    )

    # ═══════════════════════════════════════════════════════════
    # COMBINED RISK SCORE
    # Blends: expense ratio (50%) + goal pressure (30%) + budget (20%)
    # ═══════════════════════════════════════════════════════════
    expense_ratio = (expense / income * 100) if income > 0 else 100.0
    goal_pressure_weight = 0.30
    expense_ratio_weight = 0.50
    budget_weight        = 0.20

    combined_risk_score = (
        (expense_ratio        * expense_ratio_weight) +
        (goal_pressure        * goal_pressure_weight) +
        (budget_used_pct      * budget_weight)
    )

    if   combined_risk_score >= 80: combined_risk = "high"
    elif combined_risk_score >= 55: combined_risk = "medium"
    else:                           combined_risk = "low"

    return dict(
        # ── Core financials ──────────────────────────────────
        income=income, expense=expense, surplus=surplus, budget=budget,
        savings_rate=savings_rate, budget_used_pct=budget_used_pct,
        expense_change=expense_change, p_income=p_income, p_expense=p_expense,
        days_left=days_left, days_passed=days_passed, daily_burn=daily_burn,
        income_stability=income_stability,
        today_day=today.day, days_in_month=days_in_month,
        # ── Category intelligence ────────────────────────────
        top_cat_name=top_cat_name, top_cat_pct=top_cat_pct,
        # ── Goal intelligence (NEW) ──────────────────────────
        goal_pressure=goal_pressure,
        goal_details=goal_details,
        total_target=total_target,
        total_saved=total_saved,
        avg_monthly_surplus=avg_monthly_surplus,
        total_monthly_required=total_monthly_required,
        # ── Combined risk (NEW) ──────────────────────────────
        combined_risk=combined_risk,
        combined_risk_score=round(combined_risk_score, 1),
        # ── Legacy compat ────────────────────────────────────
        goals=[{
            "name": g["name"],
            "target_amount": g["target_amount"],
            "saved_amount": g["saved_amount"],
        } for g in goal_details],
    )


# ============================================================
# PART E — UNIFIED FINANCIAL INTELLIGENCE API
# ============================================================
#
# The single authoritative endpoint for the new Insights dashboard. It
# orchestrates the recurring engine, forecast model, risk/health model,
# anomaly engine, goal data, and recommender - each of which is called
# EXACTLY ONCE - and returns one structured payload. routes/insights.py
# and static/js/insights.js must NOT duplicate any of this logic; they
# either delegate to it or remain untouched legacy/compat surfaces.

def _fetch_monthly_expense_history(conn, user_id, months=12):
    """
    Total expense per calendar month, oldest -> newest, plus the aligned
    calendar month number for each entry (for optional seasonal
    forecasting). Used only by the unified endpoint's forecast section -
    NOT a general-purpose helper duplicated elsewhere.
    """
    rows = conn.execute("""
        SELECT to_char(date,'YYYY-MM') AS month,
               EXTRACT(MONTH FROM date)::int AS month_num,
               COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END),0) AS total
        FROM transactions
        WHERE user_id=%s AND status <> 'failed'
        GROUP BY month, month_num
        ORDER BY month ASC
    """, (user_id,)).fetchall()
    rows = rows[-months:] if len(rows) > months else rows
    history = [float(r["total"] or 0) for r in rows]
    month_indices = [int(r["month_num"]) for r in rows]
    return history, month_indices


def _fetch_category_history(conn, user_id, months=6):
    """category -> [monthly totals oldest -> newest], for the last N
    calendar months that have any expense data. Single query."""
    rows = conn.execute("""
        SELECT to_char(date,'YYYY-MM') AS month,
               COALESCE(category,'Misc') AS category,
               SUM(amount) AS total
        FROM transactions
        WHERE user_id=%s AND type='expense' AND status <> 'failed'
        GROUP BY month, category
        ORDER BY month ASC
    """, (user_id,)).fetchall()

    by_month = {}
    for r in rows:
        by_month.setdefault(r["month"], {})[r["category"]] = float(r["total"] or 0)
    recent_months = sorted(by_month.keys())[-months:]

    categories = set()
    for mo in recent_months:
        categories.update(by_month[mo].keys())

    return {cat: [by_month[mo].get(cat, 0.0) for mo in recent_months] for cat in categories}


_NO_INCOME_DEFICIT_PREFIX = "Projected expenses exceed income by"
# Every phrasing risk_model.py (or an earlier pass of this function) can
# produce for the same underlying "no income recorded" situation. All of
# these are semantically the same risk factor and must collapse to one
# deterministic message rather than being shown side by side.
_NO_INCOME_SIGNAL_PREFIXES = (
    _NO_INCOME_DEFICIT_PREFIX,
    "Income data is unavailable",
    "No income is recorded",
)


def _reword_no_income_risk_factors(main_risk_factors, income, cash_flow):
    """Display-layer fix only (risk_model.py is not modified this pass).

    calculate_financial_health() phrases a projected cash-flow deficit as
    "Projected expenses exceed income by ₹X" - accurate when there IS
    income to compare against, but misleading when income == 0, since
    there's no recorded income to "exceed". Separately, it can also emit
    a dedicated "Income data is unavailable..." risk factor. Left alone,
    a no-income user could see BOTH factors on the dashboard, saying the
    same thing twice ("Income data is unavailable" and "No income is
    recorded"). This collapses every no-income-signal factor into
    exactly ONE deterministic message, placed at the position of the
    first such factor; every unrelated risk factor passes through
    unchanged, in its original order.
    """
    if income != 0 or not main_risk_factors:
        return main_risk_factors

    proj_expense = (cash_flow or {}).get("projected_expense")
    if proj_expense is not None:
        canonical_message = f"No income is recorded for this period, while projected expenses are ₹{round(proj_expense)}."
    else:
        canonical_message = "No income is recorded for this period."

    lower_prefixes = tuple(p.lower() for p in _NO_INCOME_SIGNAL_PREFIXES)
    reworded = []
    emitted_canonical = False
    for factor in main_risk_factors:
        if factor.lower().startswith(lower_prefixes):
            if not emitted_canonical:
                reworded.append(canonical_message)
                emitted_canonical = True
            continue  # drop the duplicate no-income-signal factor
        reworded.append(factor)
    return reworded


def _fetch_current_month_category_totals(conn, user_id, cur_start):
    """category -> current-calendar-month expense total, for categories
    that actually have at least one transaction this month. Used only to
    tell "no current-month activity" apart from "spending decreased" when
    annotating category_forecasts below — a category simply missing from
    this dict this month is NOT evidence of a downward trend."""
    rows = conn.execute("""
        SELECT COALESCE(category,'Misc') AS category,
               COALESCE(SUM(amount),0) AS total
        FROM transactions
        WHERE user_id=%s AND type='expense' AND date>=%s
        GROUP BY category
    """, (user_id, cur_start)).fetchall()
    return {r["category"]: float(r["total"] or 0) for r in rows if float(r["total"] or 0) > 0}


def _fetch_anomaly_input(conn, user_id):
    rows = conn.execute("""
        SELECT id, amount, category, description
        FROM transactions
        WHERE user_id=%s AND type='expense' AND status <> 'failed'
        ORDER BY date ASC
    """, (user_id,)).fetchall()
    return [
        {"id": r["id"], "amount": float(r["amount"] or 0),
         "category": r["category"], "description": r["description"]}
        for r in rows
    ]


@ai_insights_bp.route("/api/insights/unified")
@login_required
def unified_insights():
    user_id = session["user_id"]
    conn = get_db()
    try:
        m = _fetch_full_metrics(conn, user_id)
        history, month_indices = _fetch_monthly_expense_history(conn, user_id)
        category_history = _fetch_category_history(conn, user_id)
        anomaly_input = _fetch_anomaly_input(conn, user_id)
        cur_start, _prev_start, _prev_end = _get_month_bounds()
        current_month_category_totals = _fetch_current_month_category_totals(conn, user_id, cur_start)
    finally:
        _safe_close(conn)

    # ── ONE recurring analysis call — everything recurring-related below
    # is DERIVED from this single result (Part Q: no repeated recurrence
    # calculation, no N+1 queries). ──────────────────────────────────
    recurring_data = analyze_recurring_transactions(user_id)
    recurring_items = get_recurring_transactions(user_id, recurring_data=recurring_data)
    subscription_summary = get_subscription_summary(user_id, recurring_data=recurring_data)
    recurring_income = get_recurring_income(user_id, recurring_data=recurring_data)
    upcoming = get_upcoming_recurring(user_id, recurring_data=recurring_data, days_ahead=30)

    # ── Overdue / upcoming payment sections (Part 1, 2, 5 fix) ──────────
    # These sections must only ever contain actual EXPENSE commitments
    # that are still believed to be live:
    #   - classification: subscription / recurring_bill only — never
    #     recurring_income (that belongs in its own recurring_income
    #     section, see below) and never unknown_recurring (too
    #     unconfirmed to present as a commitment needing action).
    #   - lifecycle_status: active or possibly_missed only — inactive
    #     and possibly_inactive commitments are not live financial
    #     obligations and must never be shown as something due.
    # Lifecycle status governs whether the commitment is still live;
    # payment_status governs timing (overdue vs due_soon/upcoming). Both
    # gates are applied using the fields recurring_service.py already
    # supplies — no new recurrence/classification logic is introduced.
    _LIVE_COMMITMENT_CLASSIFICATIONS = ("subscription", "recurring_bill")
    _LIVE_COMMITMENT_LIFECYCLES = ("active", "possibly_missed")

    def _is_live_expense_commitment(item):
        return (
            item.get("classification") in _LIVE_COMMITMENT_CLASSIFICATIONS
            and item.get("lifecycle_status") in _LIVE_COMMITMENT_LIFECYCLES
        )

    overdue = [
        i for i in recurring_items
        if _is_live_expense_commitment(i) and i["payment_status"] == "overdue"
    ]

    # Defensive filter on `upcoming` too: get_upcoming_recurring() is the
    # authoritative source, but the unified dashboard's "Upcoming
    # Recurring Payments" section must never surface recurring_income or
    # inactive/possibly_inactive items even if a future change to that
    # helper's output ever included them.
    upcoming = [
        i for i in upcoming
        if _is_live_expense_commitment(i) and i.get("payment_status") in ("due_soon", "upcoming")
    ]

    # Part D: "active financial burden" is defined by lifecycle_status ==
    # "active", never by payment timing (payment_status). A subscription
    # that's simply not due yet for 3 more weeks is still an active
    # commitment and belongs in the burden total.
    active_subscriptions = [s for s in subscription_summary["subscriptions"] if s["lifecycle_status"] == "active"]
    active_bills = [b for b in recurring_data["recurring_bills"] if b["lifecycle_status"] == "active"]

    confirmed_monthly_cost = round(sum(s["monthly_equivalent"] for s in active_subscriptions), 2)
    confirmed_annual_cost = round(sum(s["annualized_cost"] for s in active_subscriptions), 2)
    recurring_bill_monthly_burden = round(sum(b["monthly_equivalent"] for b in active_bills), 2)
    monthly_burden = round(confirmed_monthly_cost + recurring_bill_monthly_burden, 2)
    annual_burden = round(monthly_burden * 12, 2)

    # Rule 2/3: must be sourced from the SAME active-lifecycle set that
    # produces "Confirmed subscriptions" above (active_subscriptions),
    # never from the broader subscription_summary["subscriptions"]
    # bucket, or a subscription the app no longer considers active could
    # still surface a price-increase alert next to "Confirmed
    # subscriptions: ₹0/mo". get_subscription_summary() now also
    # active-filters its own "subscriptions" bucket, so this is
    # defense-in-depth rather than the only guard.
    price_changes = [
        s for s in active_subscriptions
        if s.get("price_change") and s["price_change"].get("detected")
    ]

    # Recurring commitments not yet incurred THIS calendar month (used by
    # the health model so a same-month upcoming bill isn't silently
    # ignored, without double-counting anything already in current_expense).
    current_month_key = date.today().strftime("%Y-%m")
    remaining_recurring_this_month = round(sum(
        i["monthly_equivalent"] for i in recurring_items
        if i["classification"] in ("subscription", "recurring_bill")
        and i["lifecycle_status"] == "active"
        and i["payment_status"] in ("due_soon", "upcoming", "overdue")
        and i["next_expected_date"][:7] == current_month_key
    ), 2)

    # ── Forecast (Part F) — the finalized comprehensive model, called
    # once. historical_recurring is passed as a constant monthly load
    # (we don't have a reliable month-by-month recurring history from the
    # recurring engine, only its current-state snapshot), which
    # predict_next_month_comprehensive explicitly supports. ────────────
    today = datetime.today()
    days_in_month = (date(today.year + (today.month == 12), (today.month % 12) + 1, 1) - timedelta(days=1)).day
    forecast = predict_next_month_comprehensive(
        history=history,
        current_expense=m["expense"],
        days_passed=m["days_passed"],
        days_in_month=days_in_month,
        historical_recurring=monthly_burden if history else None,
        expected_recurring_monthly=monthly_burden,
        target="next_month",
        month_indices=month_indices if len(month_indices) == len(history) else None,
        category_history=category_history,
    )

    # Count goals that currently need attention. Keep this derived from the
    # canonical per-goal risk classification so the unified health payload and
    # goal payload cannot drift apart.
    goals_at_risk_count = sum(
        1 for g in m["goal_details"] if g.get("goal_risk") in ("medium", "high")
    )

    # ── Financial health / risk (Part G) — canonical shared engine. ──
    health = compute_health_for_context(
        m,
        forecast,
        monthly_burden=monthly_burden,
        confirmed_monthly_cost=confirmed_monthly_cost,
        recurring_bill_monthly_burden=recurring_bill_monthly_burden,
        remaining_recurring_this_month=remaining_recurring_this_month,
        days_in_month=days_in_month,
    )

    # Canonical current-period values travel with the canonical health object.
    # This prevents Flutter from combining a health score from this endpoint
    # with income/expense/budget values from a separate, independently timed
    # dashboard request.
    health["current"] = {
        "income": m["income"],
        "expense": m["expense"],
        "surplus": m["surplus"],
        "balance": m["surplus"],
        "budget": m["budget"],
    }
    health["summary"]["avg_monthly_surplus"] = m["avg_monthly_surplus"]
    health["summary"]["total_monthly_required"] = m["total_monthly_required"]
    health["summary"]["goals_at_risk"] = goals_at_risk_count
    health["summary"]["income_stability"] = m.get("income_stability")
    health["summary"]["budget_adherence"] = (
        max(0.0, 100.0 - m["budget_used_pct"]) if m["budget"] > 0 else None
    )

    # Fix (Section 8/9 — no-income wording): rephrase the one risk
    # factor that would otherwise misleadingly say income was
    # "exceeded" when no income was recorded at all. risk_model.py
    # itself is unchanged; this only adjusts the string in the payload
    # this endpoint returns.
    health["main_risk_factors"] = _reword_no_income_risk_factors(
        health.get("main_risk_factors", []), m["income"], health.get("cash_flow", {})
    )

    # ── Anomalies (existing engine, called once) ────────────────────
    raw_anomalies = detect_category_anomalies(anomaly_input)
    anomalies = [
        {
            "id": a["transaction_id"], "transaction_id": a["transaction_id"],
            "amount": a["amount"], "category": a["category"],
            "expected_amount": a["expected_amount"], "deviation": a["deviation"],
            "severity": a["severity"], "confidence": a["confidence"],
            # Part P: never call it "fraud".
            "reason": a["reason"] or "Unusual transaction — review to make sure it was expected.",
        }
        for a in raw_anomalies
    ]

    # ── Recommendations (Part H) — pure orchestration consumer, called
    # once with the structured outputs already computed above. ────────
    recs = get_financial_recommendations(
        user_id=user_id,
        income=m["income"],
        health=health,
        recurring_items=recurring_items,
        subscription_summary=subscription_summary,
        recurring_bill_burden=recurring_bill_monthly_burden,
        price_changes=price_changes,
        goal_details=m["goal_details"],
        available_surplus=m["avg_monthly_surplus"],
        category_trends=forecast.get("category_forecasts"),
        anomalies=anomalies,
        spending_trend_pct=m["expense_change"],
    )

    # Fix (category trend false zeroes): forecast["category_forecasts"]
    # can label a category "down"/"₹0" purely because the CURRENT
    # calendar month has no transactions for it yet, which is not the
    # same thing as spending having decreased. Cross-check each entry
    # against actual current-month activity (queried above, independent
    # of the forecast model's own trend math) and relabel any category
    # with zero current-month spend as "no_data" rather than a trend, for
    # display purposes only. This does not touch forecast_model.py or
    # the numbers fed to the recommender above.
    display_category_forecasts = []
    for c in (forecast.get("category_forecasts") or []):
        entry = dict(c)
        cat = entry.get("category")
        if cat is not None and current_month_category_totals.get(cat, 0.0) == 0.0:
            entry["trend"] = "no_data"
            entry["note"] = f"{cat} — no spending recorded this month"
        display_category_forecasts.append(entry)

    # Fix (Rule 11 — don't overstate trend reliability): forecast_model.py
    # is unchanged, and every number derived from `forecast` above
    # (health, recommendations) already used the raw dict before this
    # point. This only softens what's SHOWN when the model itself
    # flagged low confidence, so a shaky 2-3-transaction trend doesn't
    # read as a confident "up"/"down" call. If confidence isn't "low",
    # the forecast's own trend is left exactly as supplied.
    display_forecast = dict(forecast)
    if display_forecast.get("confidence") == "low" and display_forecast.get("trend"):
        display_forecast["trend"] = "insufficient_history"
        insufficiency_note = "Limited transaction history — the spending trend isn't reliable yet."
        existing_message = display_forecast.get("message")
        display_forecast["message"] = (
            f"{existing_message} {insufficiency_note}" if existing_message else insufficiency_note
        )

    payload = {
        "status": "success",

        "financial_health": health,

        "forecast": display_forecast,

        "recurring": {
            "upcoming": upcoming,
            "overdue": overdue,
            "monthly_burden": monthly_burden,
            "annual_burden": annual_burden,
        },

        "subscriptions": {
            "active": active_subscriptions,
            "possible": subscription_summary["possible_subscriptions"],
            "count": len(active_subscriptions),
            "confirmed_monthly_cost": confirmed_monthly_cost,
            "confirmed_annual_cost": confirmed_annual_cost,
            "price_changes": price_changes,
        },

        "recurring_income": recurring_income["recurring_income"],

        "spending": {
            "trend_pct": m["expense_change"],
            "categories": display_category_forecasts,
            "top_category": {"name": m["top_cat_name"], "percent": m["top_cat_pct"]},
        },

        "anomalies": anomalies,

        "goals": {
            "details": m["goal_details"],
            "pressure": m["goal_pressure"],
            "goals_at_risk": goals_at_risk_count,
        },

        "recommendations": recs.get("recommendations", []),
        "recovery_plan": recs.get("overspending_recovery"),
    }

    return jsonify(payload)


# ── 1. PROACTIVE AI INSIGHTS (goal-aware) ────────────────────────────
@ai_insights_bp.route("/ai-insights")
@login_required
def ai_insights():
    conn = get_db()
    try:
        m = _fetch_full_metrics(conn, session["user_id"])
    finally:
        _safe_close(conn)

    insights = []

    # Budget pressure
    if m["budget"] > 0 and m["budget_used_pct"] >= 85:
        cut = int((m["expense"] - m["budget"]) / max(m["days_left"], 1))
        insights.append({
            "message": f"Budget at {m['budget_used_pct']}% — ₹{int(m['expense'])} of ₹{int(m['budget'])} used. Cut ₹{cut}/day to avoid overspend.",
            "level": "high", "type": "budget"
        })
    elif m["budget"] > 0 and m["budget_used_pct"] >= 65:
        safe = int((m["budget"] - m["expense"]) / max(m["days_left"], 1))
        insights.append({
            "message": f"Budget {m['budget_used_pct']}% used. ₹{int(m['budget'] - m['expense'])} left for {m['days_left']} days — pace at ₹{safe}/day.",
            "level": "medium", "type": "budget"
        })

    # Savings rate
    if m["savings_rate"] < 5 and m["income"] > 0:
        save = int(m["expense"] * m["top_cat_pct"] / 100 * 0.15)
        insights.append({
            "message": f"Savings rate only {m['savings_rate']}%. Cutting {m['top_cat_name']} by 15% would free ₹{save} this month.",
            "level": "high", "type": "trend"
        })
    elif 5 <= m["savings_rate"] < 15 and m["income"] > 0:
        insights.append({
            "message": f"Savings at {m['savings_rate']}%. Trimming ₹{int(m['expense'] * 0.08)} from {m['top_cat_name']} could push you past 15%.",
            "level": "medium", "type": "trend"
        })

    # Expense spike — only report a percentage change when a real
    # previous-month baseline exists.
    if m["expense_change"] is not None and m["expense_change"] > 30:
        insights.append({
            "message": f"Expenses up {m['expense_change']}% vs last month (₹{int(m['p_expense'])} → ₹{int(m['expense'])}). {m['top_cat_name']} is {m['top_cat_pct']}% of spend.",
            "level": "high", "type": "category"
        })

    # ── GOAL-BASED INSIGHTS (NEW) ────────────────────────────────
    for g in m["goal_details"]:
        if g["target_amount"] <= 0:
            continue

        pct = g["progress_percent"]
        mr  = float(g["monthly_required"] or 0)

        # Goal falling behind
        if g["goal_risk"] == "high":
            insights.append({
                "message": f"Goal '{g['name']}' needs ₹{int(mr)}/mo but your surplus is only ₹{int(m['avg_monthly_surplus'])}. It may be delayed.",
                "level": "high", "type": "goal"
            })
        elif g["goal_risk"] == "medium":
            ml = g.get("months_left", "?")
            insights.append({
                "message": f"Goal '{g['name']}' is {pct}% funded with {ml} months left. Save ₹{int(mr)}/mo to stay on track.",
                "level": "medium", "type": "goal"
            })
        elif pct < 20 and m["surplus"] > 0 and mr > 0:
            months = round(g["remaining"] / m["avg_monthly_surplus"]) if m["avg_monthly_surplus"] > 0 else "?"
            insights.append({
                "message": f"Goal '{g['name']}' is {pct}% funded. At ₹{int(m['avg_monthly_surplus'])}/mo surplus, ~{months} months to go.",
                "level": "medium", "type": "goal"
            })

    # ── GOAL PRESSURE INSIGHT (NEW) ──────────────────────────────
    if m["goal_pressure"] > 70 and m["total_monthly_required"] > 0:
        insights.append({
            "message": f"Goal pressure is high ({m['goal_pressure']:.0f}/100). You need ₹{int(m['total_monthly_required'])}/mo for all goals but surplus is ₹{int(m['avg_monthly_surplus'])}.",
            "level": "high", "type": "goal"
        })

    order = {"high": 0, "medium": 1, "low": 2}
    insights = sorted(insights, key=lambda x: order.get(x["level"], 3))[:4]

    if not insights:
        insights.append({
            "message": f"Finances look healthy. Savings {m['savings_rate']}%, surplus ₹{int(m['surplus'])}. Keep it up!",
            "level": "low", "type": "trend"
        })

    return jsonify({"insights": insights})


# ── 2. RISK SCORE (goal-pressure aware) ─────────────────────────────
@ai_insights_bp.route("/risk-score")
@login_required
def risk_score():
    """Backward-compatible alias for the canonical unified health score."""
    response = unified_insights()
    payload = response.get_json() or {}
    health = payload.get("financial_health") or {}
    summary = health.get("summary") or {}
    score = health.get("score")
    risk_level = health.get("risk_level") or "insufficient_data"

    if score is None:
        return jsonify({
            "health_score": None,
            "risk_level": "insufficient_data",
            "tooltip": "Insufficient data to calculate a reliable financial health score.",
            "savings_rate": summary.get("current_savings_rate"),
            "budget_used": (health.get("budget") or {}).get("budget_usage_pct"),
            "goal_pressure": summary.get("goal_pressure"),
            "combined_risk": "insufficient_data",
            "combined_risk_score": None,
        })

    return jsonify({
        "health_score": score,
        "risk_level": risk_level,
        "tooltip": summary.get("health_status") or "Canonical financial health score.",
        "savings_rate": summary.get("current_savings_rate"),
        "budget_used": (health.get("budget") or {}).get("budget_usage_pct"),
        "goal_pressure": summary.get("goal_pressure"),
        "combined_risk": risk_level,
        "combined_risk_score": round(100 - float(score), 1),
    })


# ── 3. BADGE COUNT ───────────────────────────────────────────────────
@ai_insights_bp.route("/insight-badge")
@login_required
def insight_badge():
    conn = get_db()
    try:
        m = _fetch_full_metrics(conn, session["user_id"])
    finally:
        _safe_close(conn)

    high = medium = 0

    if m["budget"] > 0:
        if   m["budget_used_pct"] >= 85: high   += 1
        elif m["budget_used_pct"] >= 65: medium += 1

    if m["income"] > 0:
        if   m["savings_rate"] < 5:  high   += 1
        elif m["savings_rate"] < 15: medium += 1

    if m["expense_change"] is not None:
        if m["expense_change"] > 30: high += 1
        elif m["expense_change"] > 15: medium += 1

    # Goal pressure badge (NEW)
    if   m["goal_pressure"] > 70: high   += 1
    elif m["goal_pressure"] > 40: medium += 1

    color = "red" if high > 0 else ("yellow" if medium > 0 else "green")
    return jsonify({"count": high + medium, "color": color, "high": high, "medium": medium})


# ── 4. SMART NUDGE ───────────────────────────────────────────────────
@ai_insights_bp.route("/smart-nudge")
@login_required
def smart_nudge():
    conn = get_db()
    try:
        m = _fetch_full_metrics(conn, session["user_id"])
    finally:
        _safe_close(conn)

    # Show nudge if: month-end OR goal pressure is high
    show_for_goals = m["goal_pressure"] > 60 and m["total_monthly_required"] > m["avg_monthly_surplus"]
    show_for_budget = m["today_day"] > 20 and (m["budget_used_pct"] >= 75 or m["savings_rate"] < 10)

    if not show_for_goals and not show_for_budget:
        return jsonify({"nudge": None})

    days_left  = max(m["days_left"], 1)
    safe_daily = (m["budget"] - m["expense"]) / days_left if m["budget"] > 0 else 0
    reduction  = max(0, round(m["daily_burn"] - safe_daily))

    if show_for_goals:
        shortfall = max(0, int(m["total_monthly_required"] - m["avg_monthly_surplus"]))
        msg = (
            f"Goal pressure is high — you need ₹{int(m['total_monthly_required'])}/mo for your goals "
            f"but surplus is ₹{int(m['avg_monthly_surplus'])}. "
            f"Cutting {m['top_cat_name']} (₹{shortfall} gap) would help significantly."
        )
    else:
        msg = (
            f"You are at {m['budget_used_pct']}% of your budget with {days_left} days left. "
            f"Cutting ₹{reduction}/day — especially in {m['top_cat_name']} ({m['top_cat_pct']}%) — will keep you on track."
        )

    return jsonify({"nudge": {
        "message":   msg,
        "days_left": days_left,
        "reduction": reduction,
        "goal_pressure": m["goal_pressure"],
    }})


# ── 5. GOAL INTELLIGENCE ENDPOINT (NEW) ─────────────────────────────
@ai_insights_bp.route("/goal-intelligence")
@login_required
def goal_intelligence():
    """
    Returns per-goal AI insights: savings suggestions, risk level,
    monthly targets, and delay warnings. Consumed by GoalsScreen and
    the unified financial provider.
    """
    conn = get_db()
    try:
        m = _fetch_full_metrics(conn, session["user_id"])
    finally:
        _safe_close(conn)

    results = []
    for g in m["goal_details"]:
        if g["target_amount"] <= 0:
            continue

        insight_msg = None
        savings_tip = None

        mr  = float(g["monthly_required"] or 0)
        ml  = g["months_left"]
        pct = g["progress_percent"]
        rem = float(g["remaining"] or 0)

        if g["goal_risk"] == "high":
            shortfall = max(0, mr - m["avg_monthly_surplus"])
            insight_msg = (
                f"Needs ₹{int(mr)}/mo but surplus is ₹{int(m['avg_monthly_surplus'])}. "
                f"Reduce {m['top_cat_name']} by ₹{int(shortfall)} to close the gap."
            )
            savings_tip = f"Cut {m['top_cat_name']} spending by 15% to free ₹{int(m['expense'] * m['top_cat_pct'] / 100 * 0.15)}."
        elif g["goal_risk"] == "medium":
            insight_msg = f"{pct:.0f}% funded. Save ₹{int(mr)}/mo for {ml} more months."
            savings_tip = f"Automate ₹{int(mr * 0.5)} bi-weekly transfers for discipline."
        else:
            if pct >= 90:
                insight_msg = f"Almost there! Just ₹{int(rem)} remaining."
                savings_tip = "One extra contribution now closes this goal early."
            elif pct >= 50:
                insight_msg = f"Good progress at {pct:.0f}%. {ml} months remaining at current pace."
                savings_tip = "You're on track. Keep contributions consistent."
            else:
                insight_msg = f"Early stage — {pct:.0f}% funded. Consistency is key."
                savings_tip = f"Set up auto-transfer of ₹{int(mr)} on payday."

        results.append({
            **g,
            "insight":     insight_msg,
            "savings_tip": savings_tip,
        })

    return jsonify({
        "goal_intelligence": results,
        "goal_pressure":     m["goal_pressure"],
        "combined_risk":     m["combined_risk"],
        "avg_monthly_surplus": m["avg_monthly_surplus"],
        "total_monthly_required": m["total_monthly_required"],
    })


# ── 6. BEHAVIORAL PATTERNS ───────────────────────────────────────────
@ai_insights_bp.route("/behavioral-patterns")
@login_required
def behavioral_patterns():
    user_id = session["user_id"]
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT to_char(date,'YYYY-MM') AS month,
                   COALESCE(category,'Misc') AS category,
                   SUM(amount) AS total
            FROM transactions WHERE user_id=%s AND type='expense' AND status <> 'failed'
            GROUP BY month, category ORDER BY month DESC
        """, (user_id,)).fetchall()
        rate_rows = conn.execute("""
            SELECT to_char(date,'YYYY-MM') AS month,
                   COALESCE(SUM(CASE WHEN type='income' THEN amount ELSE 0 END),0) AS income,
                   COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END),0) AS expense
            FROM transactions WHERE user_id=%s GROUP BY month ORDER BY month DESC LIMIT 4
        """, (user_id,)).fetchall()
    finally:
        _safe_close(conn)

    patterns = []
    if not rows:
        return jsonify({"patterns": patterns})

    monthly = {}
    for r in rows:
        monthly.setdefault(r["month"], {})[r["category"]] = float(r["total"] or 0)
    months = sorted([m for m in monthly.keys() if m], reverse=True)
    if len(months) < 2:
        return jsonify({"patterns": patterns})

    cur_d = monthly[months[0]]
    prv_d = monthly[months[1]]

    for cat, ct in cur_d.items():
        pt = prv_d.get(cat, 0.0)
        if pt > 0:
            chg = (ct - pt) / pt * 100
            if chg > 30:
                patterns.append({
                    "title": f"Spending Spike: {cat}",
                    "description": f"{cat} up {round(chg)}% vs last month (₹{int(pt)} → ₹{int(ct)}).",
                    "severity": "high" if chg > 60 else "medium"
                })

    if cur_d and prv_d:
        ct = max(cur_d, key=cur_d.get)
        pt = max(prv_d, key=prv_d.get)
        if ct != pt:
            patterns.append({
                "title": f"New Top Category: {ct}",
                "description": f"{ct} overtook {pt} as your biggest spend this month.",
                "severity": "medium"
            })

    if len(months) >= 3:
        trd = monthly[months[2]]
        for cat in cur_d:
            if cat in prv_d and cat in trd:
                if (cur_d[cat] == max(cur_d.values()) and
                        prv_d[cat] == max(prv_d.values()) and
                        trd[cat] == max(trd.values())):
                    patterns.append({
                        "title": f"Consistent Top Spend: {cat}",
                        "description": f"{cat} has been your #1 expense for 3 straight months. Consider a sub-budget.",
                        "severity": "high"
                    })

    if len(rate_rows) >= 3:
        rates = [
            round((float(r["income"] or 0) - float(r["expense"] or 0)) / float(r["income"] or 0) * 100, 1)
            if float(r["income"] or 0) > 0 else 0.0
            for r in rate_rows
        ]
        if rates[0] < rates[1] < rates[2]:
            patterns.append({
                "title": "Savings Rate Declining",
                "description": f"Savings dropped 3 months in a row: {round(rates[2])}% → {round(rates[1])}% → {round(rates[0])}%.",
                "severity": "high"
            })

    seen, dedup = set(), []
    for p in patterns:
        if p["title"] not in seen:
            seen.add(p["title"])
            dedup.append(p)

    dedup.sort(key=lambda x: {"high": 0, "medium": 1, "low": 2}.get(x["severity"], 3))
    return jsonify({"patterns": dedup[:5]})


# ── 7. RECURRING SUGGESTIONS V2 — compatibility wrapper ─────────────
#
# HISTORICAL NOTE: this endpoint used to run its OWN independent
# recurrence classifier (_classify / _score_candidate /
# _build_recurring_candidates - naive substring keyword matching, a
# separate confidence formula, its own category gates). That duplicated
# services/recurring_service.py, the two disagreed with each other, and
# it is what let ordinary repeated merchants ("The New Mirch Masala",
# "Mahadev Grocery", etc.) get surfaced as recurring "bills" independent
# of how the new Insights dashboard classified the very same
# transactions. That entire engine has been REMOVED.
#
# services/recurring_service.py is now the single authoritative
# recurrence/subscription engine for the whole app. This route is kept
# only because existing frontend code still calls it, and is now a thin
# compatibility wrapper: it reuses the new engine's classification and
# due-window logic and reshapes the result into the original response
# shape, so it can no longer disagree with the unified dashboard.

@ai_insights_bp.route("/recurring-suggestions-v2")
@login_required
def recurring_suggestions_v2():
    user_id = session["user_id"]
    today = date.today()
    this_month = today.strftime("%Y-%m")

    recurring_data = analyze_recurring_transactions(user_id)
    items = get_recurring_transactions(user_id, recurring_data=recurring_data)

    conn = get_db()
    try:
        added = set(
            r["description"].lower().strip()
            for r in conn.execute("""
                SELECT description FROM transactions
                WHERE user_id=%s AND type='expense' AND to_char(date,'YYYY-MM')=%s
            """, (user_id, this_month)).fetchall()
        )
        # Suggestions the user has already dismissed or added for this
        # occurrence period (persisted so a page refresh / dashboard
        # revisit does not re-prompt for the same occurrence).
        handled = set(
            r["suggestion_key"]
            for r in conn.execute("""
                SELECT suggestion_key FROM recurring_suggestion_state
                WHERE user_id=%s AND occurrence_period=%s
                AND status IN ('dismissed','added')
            """, (user_id, this_month)).fetchall()
        )
    finally:
        _safe_close(conn)

    # recurrence_type: reshape the new engine's `classification` into the
    # old vocabulary the frontend expects.
    type_map = {
        "subscription": "subscription",
        "possible_subscription": "subscription",
        "recurring_bill": "bill",
        "recurring_income": "recurring_payment",
        "unknown_recurring": "recurring_payment",
    }

    out = []
    for item in items:
        if item["transaction_type"] != "expense":
            continue
        if item["classification"] == "unknown_recurring":
            continue  # not confident enough to actively suggest
        if item["payment_status"] not in ("due_soon", "upcoming", "overdue"):
            continue  # only surface bills that are actually due around now

        key = item["normalized_merchant"] or item["name"].lower().strip()
        if key in added or key in handled:
            continue

        next_expected = date.fromisoformat(item["next_expected_date"])
        out.append({
            "description":       item["name"],
            "amount":            item["expected_amount"],
            "category":          item["category"],
            "avg_day":           item.get("calendar_anchor_day", next_expected.day),
            "month_count":       item["occurrences"],
            "confidence":        item["classification_confidence"],
            "recurrence_type":   type_map.get(item["classification"], "recurring_payment"),
            "occurrence_period": this_month,
        })

    out.sort(key=lambda c: c["confidence"], reverse=True)
    return jsonify(out[:5])


# ── 8. RECURRING SUGGESTION STATE (persist handled/dismissed) ───────
@ai_insights_bp.route("/recurring-suggestions-v2/mark", methods=["POST"])
@login_required
def mark_recurring_suggestion():
    """
    Persists that a recurring-suggestion occurrence has been handled
    (added or dismissed) so it is not shown again for that occurrence
    period, across page reloads and dashboard revisits. The underlying
    recurring detection is untouched — once a new occurrence period
    arrives (e.g. next month), the suggestion becomes eligible again
    because this row only applies to the specific occurrence_period.
    """
    data = request.get_json(silent=True) or {}
    description       = (data.get("description") or "").strip()
    occurrence_period = (data.get("occurrence_period") or "").strip()
    status             = (data.get("status") or "").strip()

    if not description or not occurrence_period or status not in ("dismissed", "added"):
        return jsonify({"error": "description, occurrence_period and a valid status are required"}), 400

    suggestion_key = description.lower().strip()
    user_id = session["user_id"]

    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO recurring_suggestion_state
                (user_id, suggestion_key, occurrence_period, status, handled_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (user_id, suggestion_key, occurrence_period)
            DO UPDATE SET status = EXCLUDED.status, handled_at = NOW()
        """, (user_id, suggestion_key, occurrence_period, status))
        conn.commit()
    finally:
        _safe_close(conn)

    return jsonify({"ok": True})