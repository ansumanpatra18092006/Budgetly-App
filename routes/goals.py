"""
routes/goals.py — Goals Blueprint
Budgetly finance tracker.

Endpoints:
  GET  /get-goals
  POST /add-goal
  GET  /goal-prediction/<goal_id>
  POST /update-goal-progress
  GET  /get-goals-detailed
  DEL  /delete-goal/<goal_id>
  POST /generate-roadmap          ← unified backend roadmap generator
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from flask import Blueprint, jsonify, request, session

from utils.db import get_db
from utils.decorators import login_required
from routes.ai_insights import _fetch_full_metrics
from ml.anomaly_model import detect_anomalies
from ml.forecast_model import predict_next_month

logger = logging.getLogger(__name__)

goals_bp = Blueprint("goals", __name__)


# ─────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────

def _months_between(start: datetime, end: datetime) -> float:
    """Return fractional months from *start* to *end*."""
    return (end.year - start.year) * 12 + (end.month - start.month) + (
        end.day - start.day
    ) / 30.0


def _get_monthly_cash_flow(conn, user_id: int):
    """
    Return (avg_income, avg_expense, volatility) over the last 3 months.
    Volatility = max deviation of any month's expense from the average.
    """
    rows = conn.execute("""
        SELECT
            strftime('%Y-%m', date) AS month,
            SUM(CASE WHEN type = 'income'  THEN amount ELSE 0 END) AS income,
            SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END) AS expense
        FROM   transactions
        WHERE  user_id = ?
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

# ════════════════════════════════════════════════════════════════
# POST /generate-roadmap
# ════════════════════════════════════════════════════════════════
def generate_roadmap_handler():
    """
    Unified intelligent roadmap generator.
 
    Integrates: risk analysis, goal progress, anomalies, ML forecast.
 
    Request body:
        { "goal_id": <int> }
 
    Response adds (on top of existing fields):
        - risk_summary        : current financial risk context
        - anomaly_warning     : if irregular spending detected
        - forecast_note       : ML-based next-month expense forecast
        - goal_urgency        : "critical" | "urgent" | "on_track"
        - monthly_plan        : per-month savings breakdown
        - behavioral_notes    : context-aware advice from behavioral data
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
        goal_row = conn.execute("""
            SELECT id, name, target_amount, saved_amount, category, target_date
            FROM goals WHERE id=? AND user_id=?
        """, (goal_id, user_id)).fetchone()
 
        if not goal_row:
            return jsonify({"error": "Goal not found"}), 404
 
        # ── Unified metrics (includes goal_pressure, combined_risk) ──
        metrics = _fetch_full_metrics(conn, user_id)
 
        # ── Monthly cash flow ─────────────────────────────────────
        avg_income, avg_expense, volatility = _get_monthly_cash_flow(conn, user_id)
 
        # ── All expense history for anomaly detection + forecast ──
        expense_hist = conn.execute("""
            SELECT strftime('%Y-%m', date) AS month, SUM(amount) AS total
            FROM transactions WHERE user_id=? AND type='expense'
            GROUP BY month ORDER BY month ASC
        """, (user_id,)).fetchall()
 
        # ── Anomaly check (individual transactions) ───────────────
        tx_amounts = conn.execute("""
            SELECT amount FROM transactions
            WHERE user_id=? AND type='expense' ORDER BY date ASC
        """, (user_id,)).fetchall()
 
    finally:
        conn.close()
 
    goal = dict(goal_row)
 
    saved     = float(goal["saved_amount"]  or 0)
    target    = float(goal["target_amount"] or 0)
    remaining = max(target - saved, 0.0)
    pct_done  = round(saved / target * 100, 1) if target > 0 else 0.0
 
    # ════════════════════════════════════════════════════════════
    # ANOMALY DETECTION
    # ════════════════════════════════════════════════════════════
    anomaly_warning = None
    try:
        from ml.anomaly_model import detect_anomalies
        amounts   = [float(r["amount"]) for r in tx_amounts]
        anomalies = detect_anomalies(amounts) if len(amounts) > 5 else []
        if len(anomalies) >= 2:
            anomaly_warning = (
                f"Irregular spending detected in {len(anomalies)} recent transactions. "
                "Consider stabilising expenses before aggressive goal contributions."
            )
    except Exception:
        pass
 
    # ════════════════════════════════════════════════════════════
    # ML FORECAST
    # ════════════════════════════════════════════════════════════
    forecast_note     = None
    forecast_expense  = None
    try:
        from ml.forecast_model import predict_next_month
        monthly_expenses = [float(r["total"]) for r in expense_hist if r["total"]]
        if len(monthly_expenses) >= 3:
            forecast_expense = predict_next_month(monthly_expenses)
            budget = metrics.get("budget", 0)
            if budget > 0 and forecast_expense > budget:
                forecast_note = (
                    f"Next month's expenses are forecast at ₹{int(forecast_expense)}, "
                    f"which exceeds your budget of ₹{int(budget)}. "
                    "Plan your goal contributions conservatively."
                )
            elif forecast_expense > avg_expense * 1.2:
                forecast_note = (
                    f"Expenses are trending up (forecast: ₹{int(forecast_expense)}). "
                    "Building a buffer now protects your goal contributions."
                )
    except Exception:
        pass
 
    # ════════════════════════════════════════════════════════════
    # MONTHLY CAPACITY
    # ════════════════════════════════════════════════════════════
    surplus_live     = float(metrics.get("surplus", 0))
    avg_monthly_flow = max(0.0, avg_income - avg_expense)
    monthly_capacity = (
        surplus_live if surplus_live > 0
        else avg_monthly_flow if avg_monthly_flow > 0
        else max(target * 0.05, 1000.0)
    )
 
    # ════════════════════════════════════════════════════════════
    # RISK & DIFFICULTY (now uses combined_risk + goal_pressure)
    # ════════════════════════════════════════════════════════════
    savings_rate     = float(metrics.get("savings_rate",    0))
    budget_used_pct  = float(metrics.get("budget_used_pct", 0))
    expense_change   = float(metrics.get("expense_change",  0))
    goal_pressure    = float(metrics.get("goal_pressure",   0))
    combined_risk    = metrics.get("combined_risk", "low")
    top_cat          = metrics.get("top_cat_name", "spending")
    top_pct          = float(metrics.get("top_cat_pct", 0))
    income           = float(metrics.get("income", 0))
    expense          = float(metrics.get("expense", 0))
    daily_burn       = float(metrics.get("daily_burn", 0))
 
    # Difficulty incorporates combined risk and goal pressure
    if combined_risk == "high" or monthly_capacity <= 0 or savings_rate < 5:
        difficulty = "Hard"
    elif combined_risk == "medium" or goal_pressure > 60 or budget_used_pct > 70:
        difficulty = "Medium"
    else:
        difficulty = "Easy"
 
    # ════════════════════════════════════════════════════════════
    # GOAL URGENCY (NEW)
    # ════════════════════════════════════════════════════════════
    target_date     = goal.get("target_date")
    months_required = None
    required_monthly = None
 
    if target_date:
        try:
            target_dt       = datetime.strptime(target_date, "%Y-%m-%d")
            today           = datetime.today()
            deadline_months = max(1, (target_dt.year - today.year) * 12 + (target_dt.month - today.month))
            months_required  = deadline_months
            required_monthly = round(remaining / deadline_months, 2) if deadline_months > 0 else None
        except (ValueError, TypeError):
            pass
 
    if months_required is None:
        if monthly_capacity > 0 and remaining > 0:
            months_required  = round(remaining / monthly_capacity, 1)
            required_monthly = monthly_capacity
 
    monthly_savings_needed = required_monthly if required_monthly is not None else monthly_capacity
 
    # Urgency classification
    if months_required and months_required <= 2:
        goal_urgency = "critical"
    elif goal_pressure > 70 or (required_monthly and required_monthly > monthly_capacity * 1.2):
        goal_urgency = "urgent"
    else:
        goal_urgency = "on_track"
 
    # ════════════════════════════════════════════════════════════
    # MONTHLY PLAN (detailed per-month breakdown — NEW)
    # ════════════════════════════════════════════════════════════
    num_phases = min(int(months_required or 6), 12)
    num_phases = max(num_phases, 1)
 
    save_per_phase = remaining / num_phases if num_phases else remaining
    today_dt = datetime.today()
 
    monthly_plan = []
    cumulative_saved = saved
    for i in range(num_phases):
        month_dt = today_dt + timedelta(days=30 * (i + 1))
        month_label = month_dt.strftime("%b %Y")
        cumulative_saved = min(cumulative_saved + save_per_phase, target)
        progress = round(cumulative_saved / target * 100, 1) if target > 0 else 0
 
        monthly_plan.append({
            "month":           month_label,
            "month_number":    i + 1,
            "save_target":     round(monthly_savings_needed, 2),
            "cumulative_saved": round(cumulative_saved, 2),
            "progress_pct":    progress,
            "is_milestone":    (i + 1) in [
                num_phases // 4, num_phases // 2, num_phases * 3 // 4, num_phases
            ],
        })
 
    # ════════════════════════════════════════════════════════════
    # PHASES (action steps, enhanced with risk & goal context)
    # ════════════════════════════════════════════════════════════
    _ACTION_POOL = [
        [
            f"Review {top_cat} transactions — currently {top_pct:.0f}% of spend",
            f"Set daily cap of ₹{int(daily_burn * 0.9):,} (10% below burn rate)",
            f"Automate ₹{int(monthly_savings_needed):,}/month to this goal",
            "Pause unused subscriptions for 90 days",
        ],
        [
            f"Reduce {top_cat} by 10–15% to free ₹{int(expense * top_pct / 100 * 0.12):,}",
            "Batch purchases to eliminate impulse spending",
            f"Confirm ₹{int(monthly_savings_needed):,} transferred this month",
        ],
        [
            "Redirect cashback, rewards, and windfalls to this goal",
            f"Stretch save to ₹{int(monthly_savings_needed * 1.1):,} by cutting one luxury",
            "Renegotiate recurring bills for better rates",
        ],
        [
            "Maintain pace — discipline compounds in final months",
            "If ahead: top up early to close the gap",
            "If behind: pause discretionary spending for 2 weeks",
        ],
    ]
 
    _TIPS = [
        "Open a dedicated savings account to ring-fence this goal.",
        f"A 15% cut in {top_cat} alone could accelerate your timeline significantly.",
        "Windfalls (bonuses, gifts, refunds) should go straight to the goal.",
        "Visualising the completed goal daily sharpens motivation.",
        "Keep the habit alive after this goal — momentum is the real prize.",
    ]
 
    phases = []
    for i in range(num_phases):
        action_set = _ACTION_POOL[i % len(_ACTION_POOL)]
        tip        = _TIPS[i % len(_TIPS)]
        plan_entry = monthly_plan[i] if i < len(monthly_plan) else {}
 
        phases.append({
            "title":          f"Month {i + 1}",
            "month_label":    plan_entry.get("month", f"Month {i + 1}"),
            "target_savings": round(monthly_savings_needed, 2),
            "milestone":      plan_entry.get("cumulative_saved", 0),
            "progress_pct":   plan_entry.get("progress_pct", 0),
            "actions":        action_set,
            "tip":            tip,
            "is_milestone":   plan_entry.get("is_milestone", False),
        })
 
    # ════════════════════════════════════════════════════════════
    # QUICK WINS
    # ════════════════════════════════════════════════════════════
    quick_wins = [
        f"Cut {top_cat} spending by 10% — saves ~₹{int(expense * top_pct / 100 * 0.10):,} this month",
        f"Automate ₹{int(monthly_savings_needed):,}/month transfer today",
        "Track every expense for 7 days to surface hidden leaks",
    ]
    if savings_rate < 15:
        quick_wins.append(f"Raise savings rate from {savings_rate:.0f}% to 20%")
    if budget_used_pct > 75:
        quick_wins.append("Cancel any subscription unused in the last 30 days")
    if anomaly_warning:
        quick_wins.append("Investigate and eliminate the irregular spending transactions")
 
    # ════════════════════════════════════════════════════════════
    # RISKS (enriched with goal pressure + forecast)
    # ════════════════════════════════════════════════════════════
    risks = []
 
    if combined_risk == "high":
        risks.append(f"Overall financial risk is HIGH — goal contributions are at risk")
    if monthly_capacity <= 0:
        risks.append("Current expenses exceed income — no savings headroom without cuts")
    if savings_rate < 10:
        risks.append(f"Savings rate {savings_rate:.0f}% — below the 10% minimum")
    if budget_used_pct > 80:
        risks.append(f"Budget at {budget_used_pct:.0f}% — overspend risk is elevated")
    if expense_change > 20:
        risks.append(f"Expenses rose {expense_change:.0f}% vs last month")
    if required_monthly and monthly_capacity > 0 and required_monthly > monthly_capacity:
        risks.append(
            f"Needed ₹{int(required_monthly):,}/mo exceeds capacity ₹{int(monthly_capacity):,} — deadline may slip"
        )
    if goal_pressure > 70:
        risks.append(f"High goal pressure ({goal_pressure:.0f}/100) — multiple goals competing for limited surplus")
    if volatility > monthly_capacity * 0.4:
        risks.append("Irregular spending detected — build a buffer before aggressive saving")
    if forecast_expense and expense > 0 and forecast_expense > expense * 1.15:
        risks.append(f"Expenses forecast to rise to ₹{int(forecast_expense):,} next month")
 
    if not risks:
        risks.append("No major risks detected — maintain current discipline")
 
    # ════════════════════════════════════════════════════════════
    # BEHAVIORAL NOTES (NEW — context-aware from patterns)
    # ════════════════════════════════════════════════════════════
    behavioral_notes = []
    if top_pct > 40:
        behavioral_notes.append(
            f"{top_cat} dominates your spending at {top_pct:.0f}%. "
            "Creating a dedicated sub-budget for this category will protect goal contributions."
        )
    if expense_change > 20:
        behavioral_notes.append(
            f"Your spending increased {expense_change:.0f}% last month. "
            "Review what changed and correct before it becomes a habit."
        )
    if goal_urgency == "critical":
        behavioral_notes.append(
            "Goal deadline is very close. Treat goal contributions as a non-negotiable expense — pay yourself first."
        )
 
    # ════════════════════════════════════════════════════════════
    # MOTIVATION + SUMMARY
    # ════════════════════════════════════════════════════════════
    months_display = (
        f"{months_required:.0f} month{'s' if months_required != 1 else ''}"
        if months_required else "some time"
    )
 
    motivation = (
        f"You are ₹{int(remaining):,} away from '{goal['name']}'. "
        f"Saving ₹{int(monthly_savings_needed):,}/month will reach it in {months_display}. "
        f"You've already saved {pct_done:.0f}% — keep going!"
    )
 
    summary = (
        f"Save ₹{int(monthly_savings_needed):,}/month to reach "
        f"'{goal['name']}' (₹{int(target):,}) in about {months_display}."
    )
 
    # ── Risk summary (NEW) ───────────────────────────────────────
    risk_summary = {
        "combined_risk":  combined_risk,
        "goal_pressure":  goal_pressure,
        "savings_rate":   savings_rate,
        "budget_used":    budget_used_pct,
        "label": (
            "Your finances are under pressure — prioritise stabilisation before this goal."
            if combined_risk == "high" else
            "Manageable risk. Stay disciplined with the monthly plan."
            if combined_risk == "medium" else
            "Healthy financial position — ideal conditions to reach this goal."
        )
    }
 
    return jsonify({
        # ── Core (Flutter-compatible) ─────────────────────────
        "difficulty":             difficulty,
        "months_required":        months_required,
        "monthly_savings_needed": round(monthly_savings_needed, 2),
        "remaining_amount":       round(remaining, 2),
        "phases":                 phases,
        "quick_wins":             quick_wins,
        "risks":                  risks,
        "motivation":             motivation,
        "summary":                summary,
        "strategy": (
            "aggressive"   if required_monthly and required_monthly > monthly_capacity else
            "conservative" if budget_used_pct > 80 else
            "balanced"
        ),
        # ── Enhanced intelligence (NEW) ───────────────────────
        "goal_urgency":      goal_urgency,
        "risk_summary":      risk_summary,
        "monthly_plan":      monthly_plan,
        "behavioral_notes":  behavioral_notes,
        "anomaly_warning":   anomaly_warning,
        "forecast_note":     forecast_note,
        "goal_pressure":     goal_pressure,
        "pct_done":          pct_done,
    })

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

    if target_date:
        try:
            td         = datetime.strptime(target_date, "%Y-%m-%d")
            months_left = _months_between(datetime.today(), td)
            if months_left > 0:
                required_per_month = round(remaining / months_left, 2)
            else:
                required_per_month = remaining
        except ValueError:
            pass

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
        "months_to_goal":       months_to_goal,
        "monthly_saving":       round(adjusted_saving, 2),
        "remaining_amount":     round(remaining, 2),
        "required_per_month":   required_per_month,
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
            "SELECT * FROM goals WHERE user_id = ? ORDER BY id DESC",
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
            VALUES (?, ?, ?, 0, ?, ?, ?)
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
            WHERE  id = ? AND user_id = ?
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
            "SELECT id, saved_amount, target_amount FROM goals WHERE id = ? AND user_id = ?",
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
            "UPDATE goals SET saved_amount = ? WHERE id = ? AND user_id = ?",
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
            WHERE  user_id = ?
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
            "DELETE FROM goals WHERE id = ? AND user_id = ?",
            (goal_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()

    if result.rowcount == 0:
        return jsonify({"error": "Goal not found"}), 404

    return jsonify({"success": True})


# ─────────────────────────────────────────────────────────────────
# POST /generate-roadmap
# Unified backend roadmap generator used by both the Flutter app
# and the web dashboard.  No external AI API required.
# ─────────────────────────────────────────────────────────────────

@goals_bp.route("/generate-roadmap", methods=["POST"])
@login_required
def generate_roadmap():
    """
    Generate a personalised savings roadmap for a single goal.

    Request body:
        { "goal_id": <int> }

    Response shape (compatible with roadmap_screen.dart + roadmap.js):
        {
            "difficulty":             "Easy" | "Medium" | "Hard",
            "months_required":        <number | null>,
            "monthly_savings_needed": <number>,
            "remaining_amount":       <number>,
            "phases": [
                {
                    "title":          "Month N",
                    "target_savings": <number>,
                    "actions":        [<string>, ...],
                    "tip":            <string>
                },
                ...
            ],
            "quick_wins":  [<string>, ...],
            "risks":       [<string>, ...],
            "motivation":  <string>,
            "summary":     <string>
        }
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

    # ── 1. Fetch goal row ─────────────────────────────────────────
    conn = get_db()
    try:
        goal_row = conn.execute(
            """
            SELECT id, name, target_amount, saved_amount, category, target_date
            FROM   goals
            WHERE  id = ? AND user_id = ?
            """,
            (goal_id, user_id),
        ).fetchone()

        if not goal_row:
            return jsonify({"error": "Goal not found"}), 404

        # ── 2. Fetch live financial metrics ───────────────────────
        metrics = _fetch_full_metrics(conn, user_id)

        # ── 3. Monthly cash flow (last 3 months average) ──────────
        avg_income, avg_expense, volatility = _get_monthly_cash_flow(conn, user_id)
    finally:
        conn.close()

    goal = dict(goal_row)

    saved     = float(goal["saved_amount"]  or 0)
    target    = float(goal["target_amount"] or 0)
    remaining = max(target - saved, 0.0)
    pct_done  = round(saved / target * 100, 1) if target > 0 else 0.0

    # ── 4. Monthly saving capacity ────────────────────────────────
    # Primary: actual surplus from live metrics (current month).
    # Fallback: 3-month average cash flow.
    # Last resort: 5 % of target (minimum ₹1 000) so we never divide by zero.
    surplus_live     = float(metrics.get("surplus", 0))
    avg_monthly_flow = max(0.0, avg_income - avg_expense)
    monthly_capacity = (
        surplus_live if surplus_live > 0
        else avg_monthly_flow if avg_monthly_flow > 0
        else max(target * 0.05, 1000.0)
    )

    # ── 5. Difficulty ─────────────────────────────────────────────
    savings_rate    = float(metrics.get("savings_rate",    0))
    budget_used_pct = float(metrics.get("budget_used_pct", 0))
    expense_change  = float(metrics.get("expense_change",  0))
    top_cat         = metrics.get("top_cat_name", "discretionary spending")
    top_pct         = float(metrics.get("top_cat_pct",     0))
    income          = float(metrics.get("income",          0))
    expense         = float(metrics.get("expense",         0))
    daily_burn      = float(metrics.get("daily_burn",      0))

    if monthly_capacity <= 0 or savings_rate < 5:
        difficulty = "Hard"
    elif savings_rate >= 20 and budget_used_pct < 60:
        difficulty = "Easy"
    else:
        difficulty = "Medium"

    # ── 6. Months required ────────────────────────────────────────
    # If the user has a target_date, honour it; otherwise calculate.
    months_required   = None
    required_monthly  = None
    target_date       = goal.get("target_date")

    if target_date:
        try:
            target_dt        = datetime.strptime(target_date, "%Y-%m-%d")
            today            = datetime.today()
            deadline_months  = max(
                1,
                (target_dt.year  - today.year)  * 12 +
                (target_dt.month - today.month)
            )
            months_required  = deadline_months
            required_monthly = round(remaining / deadline_months, 2) if deadline_months > 0 else None
        except (ValueError, TypeError):
            pass

    if months_required is None:
        if monthly_capacity > 0:
            months_required  = round(remaining / monthly_capacity, 1) if remaining > 0 else 0
            required_monthly = monthly_capacity
        # else: leave as None (Hard path)

    monthly_savings_needed = required_monthly if required_monthly is not None else monthly_capacity

    # ── 7. Build phases (one per month, max 12) ───────────────────
    #
    # Each phase maps to exactly one calendar month so the titles
    # read "Month 1", "Month 2", … as required.
    num_phases = min(int(months_required or 6), 12)
    num_phases = max(num_phases, 1)     # at least one phase always

    save_per_phase = remaining / num_phases if num_phases else remaining

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
    for i in range(num_phases):
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
    if expense_change > 20:
        risks.append(
            f"Expenses rose {expense_change:.0f}% vs last month — review {top_cat} category"
        )
    if required_monthly and monthly_capacity > 0 and required_monthly > monthly_capacity:
        risks.append(
            f"Required ₹{int(required_monthly):,}/month exceeds current capacity "
            f"₹{int(monthly_capacity):,} — deadline may be missed without lifestyle cuts"
        )
    if volatility > monthly_capacity * 0.4:
        risks.append("Irregular monthly expenses detected — build a buffer before aggressive saving")

    if not risks:
        risks.append("No major financial risks detected — maintain current discipline")

    # ── 10. Motivation + summary ──────────────────────────────────
    months_display = (
        f"{months_required:.0f} month{'s' if months_required != 1 else ''}"
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

    return jsonify({
        # ── Required fields (Flutter + web) ──────────────────────
        "difficulty":             difficulty,
        "months_required":        months_required,
        "monthly_savings_needed": round(monthly_savings_needed, 2),
        "remaining_amount":       round(remaining, 2),
        "phases":                 phases,
        "quick_wins":             quick_wins,
        "risks":                  risks,
        "motivation":             motivation,
        # ── Extra fields used by roadmap_screen.dart ─────────────
        "summary":                summary,
        "strategy":               (
            "aggressive"  if required_monthly and required_monthly > monthly_capacity
            else "conservative" if budget_used_pct > 80
            else "balanced"
        ),
    })