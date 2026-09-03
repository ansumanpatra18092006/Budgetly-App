"""
routes/goals.py — Goals Blueprint
FinTrust finance tracker.

Endpoints:
  GET  /get-goals
  POST /add-goal
  GET  /goal-prediction/<goal_id>
  POST /update-goal-progress
  GET  /get-goals-detailed
  DEL  /delete-goal/<goal_id>
  POST /generate-roadmap          ← unified backend roadmap generator (deterministic)
  POST /explain-roadmap-ai        ← optional, user-triggered Gemini explanation layer
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timedelta
from typing import Optional

from flask import Blueprint, jsonify, request, session

from utils.db import get_db
from utils.decorators import login_required
from routes.ai_insights import _fetch_full_metrics
from ml.anomaly_model import detect_anomalies
from ml.forecast_model import predict_next_month
from services import gemini_service

logger = logging.getLogger(__name__)

goals_bp = Blueprint("goals", __name__)

# In-memory cache for AI roadmap explanations: (user_id, goal_id, inputs_hash) -> explanation dict.
# Deliberately process-local and unbounded-TTL-by-content — a new hash is
# generated whenever any input that matters (saved amount, target date,
# capacity, etc.) changes, so stale entries just stop being looked up
# rather than needing active invalidation.
_ROADMAP_EXPLANATION_CACHE: dict[tuple, dict] = {}

_GEMINI_SYSTEM_INSTRUCTION = (
    "You are explaining an already-calculated financial savings roadmap to the user. "
    "Do not recalculate any numerical values. Do not change the target date. "
    "Do not change the required monthly savings or months required. "
    "Do not invent financial data or introduce new unsupported metrics. "
    "Treat every number in the supplied data as authoritative and final. "
    "Respond with a compact JSON object shaped exactly like: "
    '{"summary": "...", "why": "...", "priority": "...", "guidance": ["...", "...", "..."]}. '
    "Keep it concise, plain language, and specific to the numbers given."
)


# ─────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────

def _months_between(start: datetime, end: datetime) -> float:
    """Return fractional months from *start* to *end*."""
    return (end.year - start.year) * 12 + (end.month - start.month) + (
        end.day - start.day
    ) / 30.0


def _coerce_target_date(value) -> Optional[datetime]:
    """
    Normalize a goal's target_date to a datetime, regardless of what the
    DB driver handed back.

    psycopg2/PostgreSQL DATE columns come back as `datetime.date` (not
    `str`) in some driver/row-factory configurations, while other paths
    in this file (e.g. the add-goal request body) hand this a plain
    "YYYY-MM-DD" string. Calling `datetime.strptime()` unconditionally
    crashes the moment a real `date`/`datetime` object shows up — this
    normalizes both cases (and tolerates None/empty/garbage) instead of
    assuming a single wire format.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        try:
            return datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return None
    return None


def _get_monthly_cash_flow(conn, user_id: int):
    """
    Return (avg_income, avg_expense, volatility) over the last 3 months.
    Volatility = max deviation of any month's expense from the average.
    """
    rows = conn.execute("""
        SELECT
            TO_CHAR(date::date, 'YYYY-MM') AS month,
            SUM(CASE WHEN type = 'income'  THEN amount ELSE 0 END) AS income,
            SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END) AS expense
        FROM   transactions
        WHERE  user_id = %s
        GROUP  BY month
        ORDER  BY month DESC
        LIMIT  3
    """, (user_id,)).fetchall()

    if not rows:
        return 0.0, 0.0, 0.0

    incomes  = [float(r["income"]  or 0) for r in rows]
    expenses = [float(r["expense"] or 0) for r in rows]

    avg_income  = sum(incomes)  / len(incomes)
    avg_expense = sum(expenses) / len(expenses)

    volatility = 0.0
    if len(expenses) > 1:
        volatility = max(abs(e - avg_expense) for e in expenses)

    return avg_income, avg_expense, volatility


def _build_prediction(
    saved: float,
    target: float,
    monthly_saving: float,
    target_date: Optional[str],
    volatility: float = 0.0,
) -> dict:
    """Shared prediction helper used by goal_prediction and get_goals_detailed."""

    remaining = max(0.0, target - saved)

    # Apply a 15 % safety buffer so estimates are realistic
    adjusted_saving = monthly_saving * 0.85

    months_to_goal = None
    if adjusted_saving > 0:
        months_to_goal = round(remaining / adjusted_saving, 1)

    predicted_completion = None
    if months_to_goal:
        predicted_completion = (
            datetime.today() + timedelta(days=months_to_goal * 30)
        ).strftime("%Y-%m-%d")

    required_per_month = None
    months_left        = None
    deadline_months     = None  # target-date-driven timeline, kept separate from months_to_goal (capacity-driven)

    if target_date:
        td = _coerce_target_date(target_date)
        if td is not None:
            months_left = _months_between(datetime.today(), td)
            deadline_months = max(months_left, 0.0)
            if months_left > 0:
                required_per_month = round(remaining / months_left, 2)
            else:
                required_per_month = remaining

    volatility_penalty = 0
    if volatility > monthly_saving * 0.5:
        volatility_penalty = 10
    elif volatility > monthly_saving * 0.3:
        volatility_penalty = 5

    success_probability = 20
    if required_per_month and required_per_month > 0:
        ratio = adjusted_saving / required_per_month
        if   ratio >= 1.3: success_probability = 85
        elif ratio >= 1.0: success_probability = 70
        elif ratio >= 0.75: success_probability = 50
        else:               success_probability = 30
    elif adjusted_saving > 0:
        success_probability = 60

    success_probability = max(10, success_probability - volatility_penalty)

    if saved >= target:
        status = "completed"
    elif adjusted_saving <= 0:
        status = "critical"
    elif required_per_month and adjusted_saving < required_per_month:
        status = "at_risk"
    else:
        status = "on_track"

    return {
        # ── Capacity-driven estimate: "how long at your current pace" ──
        "months_to_goal":       months_to_goal,
        "monthly_saving":       round(adjusted_saving, 2),
        # ── Deadline-driven requirement: "what the target date demands" ──
        "deadline_months":      round(deadline_months, 1) if deadline_months is not None else None,
        "required_per_month":   required_per_month,
        # ── Shared ──
        "remaining_amount":     round(remaining, 2),
        "predicted_completion": predicted_completion,
        "success_probability":  success_probability,
        "volatility":           round(volatility, 2),
        "status":               status,
    }


# ─────────────────────────────────────────────────────────────────
# GET /get-goals
# ─────────────────────────────────────────────────────────────────

@goals_bp.route("/get-goals")
@login_required
def get_goals():
    user_id = session["user_id"]
    conn    = get_db()

    try:
        rows = conn.execute(
            "SELECT * FROM goals WHERE user_id = %s ORDER BY id DESC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    goals = []
    for row in rows:
        g      = dict(row)
        saved  = float(g.get("saved_amount",  0) or 0)
        target = float(g.get("target_amount", 0) or 0)
        pct    = min(round((saved / target) * 100, 1), 100.0) if target > 0 else 0.0

        if pct >= 100:
            status = "completed"
        elif saved <= 0:
            status = "no_savings"
        else:
            status = "in_progress"

        g["progress_percent"] = pct
        g["remaining_amount"] = round(max(0.0, target - saved), 2)
        g["status"]           = status
        goals.append(g)

    return jsonify({"goals": goals})


# ─────────────────────────────────────────────────────────────────
# POST /add-goal
# ─────────────────────────────────────────────────────────────────

@goals_bp.route("/add-goal", methods=["POST"])
@login_required
def add_goal():
    user_id = session["user_id"]
    data    = request.get_json(silent=True) or {}

    name        = (data.get("name")     or "").strip()
    target_raw  = data.get("target")
    category    = (data.get("category") or "Savings").strip()
    target_date = (data.get("target_date") or "").strip() or None

    if not name or target_raw is None:
        return jsonify({"error": "name and target are required"}), 400

    try:
        target = float(target_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "target must be a number"}), 400

    if target < 0:
        return jsonify({"error": "target_amount must be non-negative"}), 400

    if target_date:
        try:
            datetime.strptime(target_date, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "target_date must be YYYY-MM-DD"}), 400

    created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO goals
                (user_id, name, target_amount, saved_amount, category,
                 target_date, created_at)
            VALUES (%s, %s, %s, 0, %s, %s, %s)
            """,
            (user_id, name, target, category, target_date, created_at),
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify({"success": True}), 201


# ─────────────────────────────────────────────────────────────────
# GET /goal-prediction/<goal_id>
# ─────────────────────────────────────────────────────────────────

@goals_bp.route("/goal-prediction/<int:goal_id>")
@login_required
def goal_prediction(goal_id: int):
    user_id = session["user_id"]
    conn    = get_db()

    try:
        goal = conn.execute(
            """
            SELECT target_amount, saved_amount,
                   target_date   -- column added by db migration
            FROM   goals
            WHERE  id = %s AND user_id = %s
            """,
            (goal_id, user_id),
        ).fetchone()

        if not goal:
            return jsonify({"error": "Goal not found"}), 404

        income, expense, volatility = _get_monthly_cash_flow(conn, user_id)
    finally:
        conn.close()

    monthly_saving = max(0.0, income - expense)
    prediction     = _build_prediction(
        saved=float(goal["saved_amount"]  or 0),
        target=float(goal["target_amount"] or 0),
        monthly_saving=monthly_saving,
        target_date=goal["target_date"] if "target_date" in goal.keys() else None,
    )

    return jsonify(prediction)


# ─────────────────────────────────────────────────────────────────
# POST /update-goal-progress
# ─────────────────────────────────────────────────────────────────

@goals_bp.route("/update-goal-progress", methods=["POST"])
@login_required
def update_goal_progress():
    user_id = session["user_id"]
    data    = request.get_json(silent=True) or {}

    goal_id_raw = data.get("goal_id")
    amount_raw  = data.get("amount")
    action      = (data.get("action") or "").strip().lower()

    if goal_id_raw is None or amount_raw is None or action not in ("add", "withdraw"):
        return jsonify({"error": "goal_id, amount, and action ('add'|'withdraw') are required"}), 400

    try:
        goal_id = int(goal_id_raw)
        amount  = float(amount_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "goal_id must be an integer and amount must be a number"}), 400

    if amount <= 0:
        return jsonify({"error": "amount must be greater than zero"}), 400

    conn = get_db()
    try:
        goal = conn.execute(
            "SELECT id, saved_amount, target_amount FROM goals WHERE id = %s AND user_id = %s",
            (goal_id, user_id),
        ).fetchone()

        if not goal:
            return jsonify({"error": "Goal not found"}), 404

        current_saved = float(goal["saved_amount"] or 0)
        target        = float(goal["target_amount"] or 0)

        if action == "add":
            new_saved = current_saved + amount
        else:
            new_saved = current_saved - amount
            if new_saved < 0:
                return jsonify({
                    "error": "Withdrawal exceeds saved amount",
                    "saved_amount": current_saved,
                }), 422

        if target > 0:
            new_saved = min(new_saved, target)

        conn.execute(
            "UPDATE goals SET saved_amount = %s WHERE id = %s AND user_id = %s",
            (round(new_saved, 2), goal_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify({
        "success":      True,
        "saved_amount": round(new_saved, 2),
        "status":       "completed" if new_saved >= target > 0 else "in_progress",
    })


# ─────────────────────────────────────────────────────────────────
# GET /get-goals-detailed
# ─────────────────────────────────────────────────────────────────

@goals_bp.route("/get-goals-detailed")
@login_required
def get_goals_detailed():
    user_id = session["user_id"]
    conn    = get_db()

    try:
        rows = conn.execute(
            """
            SELECT id, name, target_amount, saved_amount, category,
                   target_date, created_at
            FROM   goals
            WHERE  user_id = %s
            ORDER  BY id DESC
            """,
            (user_id,),
        ).fetchall()

        income, expense, volatility = _get_monthly_cash_flow(conn, user_id)
    finally:
        conn.close()

    monthly_saving = max(0.0, income - expense)

    detailed_goals = []
    for row in rows:
        g      = dict(row)
        saved  = float(g["saved_amount"]  or 0)
        target = float(g["target_amount"] or 0)
        pct    = min(round((saved / target) * 100, 1), 100.0) if target > 0 else 0.0

        # Safely read optional columns (added via migration)
        target_date = g.get("target_date")
        created_at  = g.get("created_at")

        prediction = _build_prediction(
            saved=saved,
            target=target,
            monthly_saving=monthly_saving,
            target_date=target_date,
            volatility=volatility,
        )

        detailed_goals.append({
            "id":            g["id"],
            "name":          g["name"],
            "target_amount": target,
            "saved_amount":  saved,
            "category":      g["category"],
            "target_date":   target_date,
            "created_at":    created_at,
            "progress_percent": pct,
            **prediction,
        })

    return jsonify({"goals": detailed_goals})


# ─────────────────────────────────────────────────────────────────
# DELETE /delete-goal/<goal_id>
# ─────────────────────────────────────────────────────────────────

@goals_bp.route("/delete-goal/<int:goal_id>", methods=["DELETE"])
@login_required
def delete_goal(goal_id: int):
    user_id = session["user_id"]
    conn    = get_db()

    try:
        result = conn.execute(
            "DELETE FROM goals WHERE id = %s AND user_id = %s",
            (goal_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()

    if result.rowcount == 0:
        return jsonify({"error": "Goal not found"}), 404

    return jsonify({"success": True})


# ─────────────────────────────────────────────────────────────────
# Shared deterministic roadmap computation
#
# Used by /generate-roadmap directly, and reused by /explain-roadmap-ai
# so the numbers Gemini is asked to explain are guaranteed to be the
# exact same numbers the user is looking at — never client-supplied,
# never recalculated by the AI.
# ─────────────────────────────────────────────────────────────────

# Safety valve only — protects against pathological inputs (e.g. a
# 50-year target) generating thousands of phase objects. It NEVER
# changes months_required/monthly_savings_needed; if the true timeline
# exceeds this, phases_truncated=True is returned so the UI can be
# honest about it instead of silently lying via a hidden cap.
_MAX_GENERATED_PHASES = 120


def _group_phases_quarterly(phases: list[dict]) -> list[dict]:
    """Roll monthly phases into quarter-sized groups for long-timeline UX.

    Purely a presentation aid — the underlying `phases` list (and the
    months_required it represents) is never altered by this.
    """
    groups = []
    for i in range(0, len(phases), 3):
        chunk = phases[i:i + 3]
        groups.append({
            "label":               f"Q{i // 3 + 1}",
            "phase_indexes":       list(range(i, i + len(chunk))),
            "months":              [p["title"] for p in chunk],
            "target_savings_total": round(sum(p["target_savings"] for p in chunk), 2),
            "milestone":           chunk[-1]["milestone"],
        })
    return groups


def _compute_roadmap(goal: dict, metrics: dict, avg_income: float,
                      avg_expense: float, volatility: float) -> dict:
    """
    Generate a personalised savings roadmap for a single goal.

    Response shape (compatible with roadmap_screen.dart + roadmap.js):
        {
            "difficulty":               "Easy" | "Medium" | "Hard",
            "plan_type":                "deadline" | "capacity",
            "deadline_status":          "on_track" | "at_risk" | "no_deadline",
            "months_required":          <number | null>,
            "monthly_savings_needed":   <number>,
            "current_monthly_capacity": <number>,
            "capacity_gap":             <number | null>,
            "remaining_amount":         <number>,
            "phases": [
                {
                    "title":          "Month N",
                    "target_savings": <number>,
                    "milestone":      <number>,
                    "actions":        [<string>, ...],
                    "tip":            <string>
                },
                ...
            ],
            "phases_truncated":  <bool>,
            "phase_view": { "mode": "monthly" | "quarterly", "groups": [...] | null },
            "quick_wins":  [<string>, ...],
            "risks":       [<string>, ...],
            "motivation":  <string>,
            "summary":     <string>,
            "strategy":    "aggressive" | "conservative" | "balanced",
        }
    """
    saved     = float(goal["saved_amount"]  or 0)
    target    = float(goal["target_amount"] or 0)
    remaining = max(target - saved, 0.0)
    pct_done  = round(saved / target * 100, 1) if target > 0 else 0.0

    # ── 4. Monthly saving capacity ────────────────────────────────
    # Primary: actual surplus from live metrics (current month).
    # Fallback: 3-month average cash flow.
    # Last resort: 5 % of target (minimum ₹1 000) so we never divide by zero.
    #
    # The unified metrics layer intentionally returns None for some fields when
    # there is insufficient history (for example expense_change when the previous
    # month has no spend). Roadmap generation must preserve that semantic state
    # instead of calling float(None).
    def _metric_float(key: str, default: float = 0.0) -> float:
        value = metrics.get(key)
        if value is None or value == "":
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            logger.warning("Invalid roadmap metric %s=%r; using %s", key, value, default)
            return default

    surplus_live     = _metric_float("surplus")
    avg_monthly_flow = max(0.0, avg_income - avg_expense)
    monthly_capacity = (
        surplus_live if surplus_live > 0
        else avg_monthly_flow if avg_monthly_flow > 0
        else max(target * 0.05, 1000.0)
    )

    # ── 5. Difficulty ─────────────────────────────────────────────
    savings_rate    = _metric_float("savings_rate")
    budget_used_pct = _metric_float("budget_used_pct")
    # None means there is no previous-month baseline. Keep it distinct from
    # zero internally; use 0 only for the roadmap's optional comparison text.
    expense_change_raw = metrics.get("expense_change")
    expense_change = None if expense_change_raw is None else _metric_float("expense_change")
    top_cat         = metrics.get("top_cat_name") or "discretionary spending"
    top_pct         = _metric_float("top_cat_pct")
    income          = _metric_float("income")
    expense         = _metric_float("expense")
    daily_burn      = _metric_float("daily_burn")

    if monthly_capacity <= 0 or savings_rate < 5:
        difficulty = "Hard"
    elif savings_rate >= 20 and budget_used_pct < 60:
        difficulty = "Easy"
    else:
        difficulty = "Medium"

    # ── 6. Months required ──────────────────────────────────────────
    # TWO DISTINCT CONCEPTS, kept separate end to end:
    #   A) deadline-driven required timeline (target_date is authoritative)
    #   B) capacity-driven affordable estimate (only used when no target_date)
    # A goal with a target_date NEVER has its timeline silently stretched
    # to match current capacity — instead capacity_gap/deadline_status
    # surface the shortfall.
    months_required   = None
    required_monthly  = None
    target_date       = goal.get("target_date")
    plan_type         = None

    if target_date:
        target_dt = _coerce_target_date(target_date)
        if target_dt is not None:
            today           = datetime.today()
            deadline_months = max(
                1,
                (target_dt.year  - today.year)  * 12 +
                (target_dt.month - today.month)
            )
            months_required  = deadline_months
            required_monthly = round(remaining / deadline_months, 2) if deadline_months > 0 else remaining
            plan_type        = "deadline"

    if months_required is None:
        plan_type = "capacity"
        if monthly_capacity > 0:
            months_required  = round(remaining / monthly_capacity, 1) if remaining > 0 else 0
            required_monthly = monthly_capacity
        # else: leave as None (Hard path, no capacity to project from)

    monthly_savings_needed = required_monthly if required_monthly is not None else monthly_capacity

    capacity_gap    = None
    deadline_status = "no_deadline"
    if plan_type == "deadline":
        capacity_gap    = round(required_monthly - monthly_capacity, 2)
        deadline_status = "at_risk" if capacity_gap > 0 else "on_track"

    # ── 7. Build phases — one per REQUIRED month, no artificial cap ──
    # Each phase maps to exactly one calendar month so the titles
    # read "Month 1", "Month 2", … and the count always matches
    # months_required (the true timeline, deadline- or capacity-driven).
    num_phases       = max(int(round(months_required)) if months_required else 6, 1)
    phases_truncated = num_phases > _MAX_GENERATED_PHASES
    num_phases_gen   = min(num_phases, _MAX_GENERATED_PHASES)

    save_per_phase = remaining / num_phases_gen if num_phases_gen else remaining

    # Action templates — rotate through them to avoid repetition
    _ACTION_POOL = [
        [
            f"Review all {top_cat} transactions — currently {top_pct:.0f}% of spend",
            f"Set a daily spending cap of ₹{int(daily_burn * 0.9):,} (10% below current burn)",
            f"Automate ₹{int(monthly_savings_needed):,}/month transfer to this goal",
            "Cancel or pause unused subscriptions",
            "Track every expense for the first two weeks",
        ],
        [
            f"Reduce {top_cat} expenses by 10–15%",
            "Batch grocery shopping and meal-prep to cut impulse spend",
            f"Weekly check: confirm ₹{int(monthly_savings_needed):,} is on track",
            "Apply a 48-hour rule before any purchase over ₹2,000",
        ],
        [
            f"Stretch monthly saving to ₹{int(monthly_savings_needed * 1.1):,} by trimming one luxury",
            "Redirect all cashback, rewards, and windfalls to this goal",
            f"Milestone check: verify ₹{int(saved + save_per_phase * 2):,} saved by now",
            "Renegotiate recurring bills (internet, insurance) for better rates",
        ],
        [
            "Maintain saving pace — discipline compounds in the final months",
            "If ahead of schedule, top up early to reach the target sooner",
            "If behind, temporarily suspend non-essential subscriptions",
            "Review goal completion criteria and prepare for fund release",
        ],
    ]

    _TIPS = [
        "Open a dedicated savings account so the money stays ring-fenced.",
        f"A 15% cut in {top_cat} alone could meaningfully accelerate your timeline.",
        "Windfalls — bonuses, gifts, tax refunds — should go straight to the goal.",
        "Visualise the completed goal daily; it sharpens motivation when fatigue sets in.",
        "Keep the saving habit alive after this goal — momentum is the real prize.",
    ]

    phases = []
    for i in range(num_phases_gen):
        milestone = min(saved + save_per_phase * (i + 1), target)
        action_set = _ACTION_POOL[i % len(_ACTION_POOL)]
        tip        = _TIPS[i % len(_TIPS)]

        phases.append({
            "title":          f"Month {i + 1}",
            "target_savings": round(monthly_savings_needed, 2),
            "milestone":      round(milestone, 2),
            "actions":        action_set,
            "tip":            tip,
        })

    # Quarterly grouping only kicks in for long timelines — never
    # replaces `phases`, just gives the UI a coarser view to render.
    phase_view_mode = "quarterly" if len(phases) > 12 else "monthly"
    phase_view = {
        "mode":   phase_view_mode,
        "groups": _group_phases_quarterly(phases) if phase_view_mode == "quarterly" else None,
    }

    # ── 8. Quick wins ─────────────────────────────────────────────
    quick_wins = [
        f"Cut {top_cat} spending by 10% — saves ~₹{int(expense * top_pct / 100 * 0.10):,} this month",
        f"Automate ₹{int(monthly_savings_needed):,}/month transfer to this goal today",
        "Track every expense for 7 days to surface hidden spending leaks",
        "Automate savings on payday so the money never hits your spending account",
    ]
    if savings_rate < 15:
        quick_wins.append(
            f"Raise savings rate from {savings_rate:.0f}% to 20% by cutting one category"
        )
    if budget_used_pct > 75:
        quick_wins.append("Review subscriptions — cancel anything unused for 30+ days")

    # ── 9. Risks ──────────────────────────────────────────────────
    risks = []

    if monthly_capacity <= 0:
        risks.append("Current expenses exceed income — no savings headroom without cuts")
    if savings_rate < 10:
        risks.append(f"Savings rate is only {savings_rate:.0f}% — below the recommended 10% minimum")
    if budget_used_pct > 80:
        risks.append(f"Budget usage at {budget_used_pct:.0f}% — overspend risk is elevated")
    if expense_change is not None and expense_change > 20:
        risks.append(
            f"Expenses rose {expense_change:.0f}% vs last month — review {top_cat} category"
        )
    if volatility > monthly_capacity * 0.4:
        risks.append("Irregular monthly expenses detected — build a buffer before aggressive saving")

    if deadline_status == "at_risk":
        risks.append(
            f"Target timeline: {num_phases} months — required ₹{int(required_monthly):,}/month "
            f"exceeds current capacity ₹{int(monthly_capacity):,}/month (gap ₹{int(capacity_gap):,}). "
            "Deadline is at risk — the timeline is NOT being auto-extended to match capacity."
        )
    if volatility > monthly_capacity * 0.4:
        risks.append("Irregular monthly expenses detected — build a buffer before aggressive saving")

    if not risks:
        risks.append("No major financial risks detected — maintain current discipline")

    # ── 10. Motivation + summary ──────────────────────────────────
    months_display = (
        f"{num_phases} month{'s' if num_phases != 1 else ''}"
        if months_required else "some time"
    )

    motivation = (
        f"You are ₹{int(remaining):,} away from '{goal['name']}'. "
        f"Saving ₹{int(monthly_savings_needed):,} monthly will reach it in {months_display}. "
        f"You've already saved {pct_done:.0f}% — keep going!"
    )

    summary = (
        f"Save ₹{int(monthly_savings_needed):,}/month to reach "
        f"'{goal['name']}' (₹{int(target):,}) in about {months_display}."
    )

    return {
        # ── Required fields (Flutter + web) ──────────────────────
        "difficulty":               difficulty,
        "months_required":          months_required,
        "monthly_savings_needed":   round(monthly_savings_needed, 2),
        "remaining_amount":         round(remaining, 2),
        "phases":                   phases,
        "phases_truncated":         phases_truncated,
        "phase_view":               phase_view,
        "quick_wins":               quick_wins,
        "risks":                    risks,
        "motivation":               motivation,
        # ── Extra fields used by roadmap_screen.dart ─────────────
        "summary":                  summary,
        "strategy": (
            "aggressive"   if deadline_status == "at_risk"
            else "conservative" if budget_used_pct > 80
            else "balanced"
        ),
        # ── New: deadline vs. capacity, kept explicitly separate ──
        "plan_type":                plan_type,             # "deadline" | "capacity"
        "deadline_status":          deadline_status,        # "on_track" | "at_risk" | "no_deadline"
        "current_monthly_capacity": round(monthly_capacity, 2),
        "capacity_gap":             capacity_gap,
    }


# ─────────────────────────────────────────────────────────────────
# POST /generate-roadmap
# Unified backend roadmap generator used by both the Flutter app
# and the web dashboard.  No external AI API required.
# ─────────────────────────────────────────────────────────────────

@goals_bp.route("/generate-roadmap", methods=["POST"])
@login_required
def generate_roadmap():
    """Generate a personalised savings roadmap for a single goal.

    Request body: { "goal_id": <int> }
    See `_compute_roadmap` for the full response shape.
    """
    user_id = session["user_id"]
    data    = request.get_json(silent=True) or {}

    goal_id_raw = data.get("goal_id")
    if goal_id_raw is None:
        return jsonify({"error": "goal_id is required"}), 400

    try:
        goal_id = int(goal_id_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "goal_id must be an integer"}), 400

    conn = get_db()
    try:
        goal_row = conn.execute(
            """
            SELECT id, name, target_amount, saved_amount, category, target_date
            FROM   goals
            WHERE  id = %s AND user_id = %s
            """,
            (goal_id, user_id),
        ).fetchone()

        if not goal_row:
            return jsonify({"error": "Goal not found"}), 404

        metrics = _fetch_full_metrics(conn, user_id)
        avg_income, avg_expense, volatility = _get_monthly_cash_flow(conn, user_id)
    finally:
        conn.close()

    roadmap = _compute_roadmap(dict(goal_row), metrics, avg_income, avg_expense, volatility)
    return jsonify(roadmap)


# ─────────────────────────────────────────────────────────────────
# POST /explain-roadmap-ai
#
# OPTIONAL, user-triggered only (an "Explain with AI" button — never
# called automatically on page load/refresh/goal-change). Recomputes
# the deterministic roadmap itself (never trusts client-sent numbers),
# sends Gemini only the derived summary fields, and asks it to explain
# — not recalculate — the plan. If Gemini is unavailable or returns
# something malformed, the roadmap keeps working; only this optional
# explanation is affected.
# ─────────────────────────────────────────────────────────────────

@goals_bp.route("/explain-roadmap-ai", methods=["POST"])
@login_required
def explain_roadmap_ai():
    user_id = session["user_id"]
    data    = request.get_json(silent=True) or {}

    goal_id_raw = data.get("goal_id")
    if goal_id_raw is None:
        return jsonify({"error": "goal_id is required"}), 400

    try:
        goal_id = int(goal_id_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "goal_id must be an integer"}), 400

    conn = get_db()
    try:
        goal_row = conn.execute(
            """
            SELECT id, name, target_amount, saved_amount, category, target_date
            FROM   goals
            WHERE  id = %s AND user_id = %s
            """,
            (goal_id, user_id),
        ).fetchone()

        if not goal_row:
            return jsonify({"error": "Goal not found"}), 404

        metrics = _fetch_full_metrics(conn, user_id)
        avg_income, avg_expense, volatility = _get_monthly_cash_flow(conn, user_id)

        tx_amounts = conn.execute(
            "SELECT amount FROM transactions WHERE user_id=%s AND type='expense' ORDER BY date ASC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    goal    = dict(goal_row)
    roadmap = _compute_roadmap(goal, metrics, avg_income, avg_expense, volatility)

    anomaly_warning = None
    try:
        amounts   = [float(r["amount"]) for r in tx_amounts]
        anomalies = detect_anomalies(amounts) if len(amounts) > 5 else []
        if len(anomalies) >= 2:
            anomaly_warning = f"Irregular spending detected in {len(anomalies)} recent transactions."
    except Exception:
        pass

    # ── Minimal payload sent to Gemini — derived fields only ──────
    explain_payload = {
        "goal_name":                goal["name"],
        "target_amount":            float(goal["target_amount"] or 0),
        "saved_amount":             float(goal["saved_amount"] or 0),
        "remaining_amount":         roadmap["remaining_amount"],
        "target_date":              (goal.get("target_date").isoformat()
                                      if hasattr(goal.get("target_date"), "isoformat")
                                      else goal.get("target_date")),
        "plan_type":                roadmap["plan_type"],
        "months_required":          roadmap["months_required"],
        "required_monthly_saving":  roadmap["monthly_savings_needed"],
        "current_monthly_capacity": roadmap["current_monthly_capacity"],
        "capacity_gap":             roadmap["capacity_gap"],
        "difficulty":               roadmap["difficulty"],
        "deadline_status":          roadmap["deadline_status"],
        "savings_rate":             metrics.get("savings_rate"),
        "budget_used_pct":          metrics.get("budget_used_pct"),
        "top_spending_category":    metrics.get("top_cat_name"),
        "anomaly_warning":          anomaly_warning,
        "existing_roadmap_actions": [a for p in roadmap["phases"][:3] for a in p["actions"]],
    }

    cache_key = (
        user_id, goal_id,
        hashlib.sha256(json.dumps(explain_payload, sort_keys=True, default=str).encode()).hexdigest(),
    )

    cached = _ROADMAP_EXPLANATION_CACHE.get(cache_key)
    if cached is not None:
        return jsonify({**cached, "cached": True})

    try:
        explanation = gemini_service.generate_json(_GEMINI_SYSTEM_INSTRUCTION, explain_payload)

        # Defensive: never let AI-returned numbers override backend truth —
        # only the qualitative fields are used, no numeric field is accepted.
        result = {
            "summary":  str(explanation.get("summary",  ""))[:1000],
            "why":      str(explanation.get("why",      ""))[:1000],
            "priority": str(explanation.get("priority", ""))[:300],
            "guidance": [str(g)[:300] for g in (explanation.get("guidance") or [])][:6],
            "available": True,
        }
    except ValueError as exc:
        logger.exception(
            "AI roadmap explanation failed for goal %s",
            goal_id,
        )
        return jsonify({
            "available": False,
            "message": str(exc),
        }), 500

    _ROADMAP_EXPLANATION_CACHE[cache_key] = result
    return jsonify({**result, "cached": False})