# ============================================================
# FILE: routes/decision_engine.py  [UNIFIED TRANSACTION PREVIEW]
# ============================================================
# Fully integrated: shows goal impact, delay warnings,
# savings rate effect, and goal-pressure-adjusted risk.
# ============================================================

from utils.db import get_db
from routes.ai_insights import _fetch_full_metrics

from ml.risk_model import predict_risk
from ml.anomaly_model import detect_anomalies
from ml.forecast_model import predict_next_month
from ml.recommender import get_recommendations

from datetime import datetime


def evaluate_transaction(user_id: int, payload: dict) -> dict:
    """
    Evaluate a prospective transaction and return full financial intelligence:
      - risk level (expense + goal pressure aware)
      - goal impact per goal (delay in months, new required saving)
      - savings rate effect
      - budget impact
      - payment suggestion
      - recommendations
    """
    amount   = float(payload.get("amount", 0))
    tx_type  = payload.get("type", "expense")
    category = payload.get("category", "Misc")

    conn = get_db()

    try:
        metrics = _fetch_full_metrics(conn, user_id)

        # Wallet balance
        w = conn.execute(
            "SELECT balance FROM wallets WHERE user_id=?", (user_id,)
        ).fetchone()
        wallet_balance = float(w["balance"]) if w else 0.0

        # Historical expense amounts
        rows = conn.execute(
            "SELECT amount FROM transactions WHERE user_id=? AND type='expense'",
            (user_id,)
        ).fetchall()
        history = [r["amount"] for r in rows]

        # Category spend this month
        month_start = datetime.today().strftime("%Y-%m-01")
        cat_row = conn.execute("""
            SELECT SUM(amount) as total FROM transactions
            WHERE user_id=? AND type='expense' AND category=? AND date>=?
        """, (user_id, category, month_start)).fetchone()
        category_spent = cat_row["total"] or 0.0

    finally:
        conn.close()

    # ════════════════════════════════════════════════════════════
    # BASE METRICS
    # ════════════════════════════════════════════════════════════
    income        = metrics["income"]
    expense       = metrics["expense"]
    budget        = metrics["budget"]
    goal_details  = metrics["goal_details"]
    goal_pressure = metrics["goal_pressure"]
    avg_surplus   = metrics["avg_monthly_surplus"]
    top_cat       = metrics["top_cat_name"]

    # ════════════════════════════════════════════════════════════
    # SIMULATE TRANSACTION
    # ════════════════════════════════════════════════════════════
    if tx_type == "expense":
        new_expense = expense + amount
        new_surplus = income - new_expense
    else:
        new_expense = expense
        new_surplus = (income + amount) - expense

    savings_rate = (new_surplus / income * 100)  if income > 0 else 0.0
    budget_usage = (new_expense / budget * 100)  if budget > 0 else 0.0

    # ════════════════════════════════════════════════════════════
    # ML RISK (base)
    # ════════════════════════════════════════════════════════════
    risk_data  = predict_risk(income, new_expense, budget)
    risk_level = risk_data.get("risk", "LOW")

    # ════════════════════════════════════════════════════════════
    # ANOMALY DETECTION
    # ════════════════════════════════════════════════════════════
    anomaly = False
    if len(history) > 5:
        anomalies = detect_anomalies(history + [amount])
        anomaly   = len(anomalies) > 0 and anomalies[-1] == len(history)

    # ════════════════════════════════════════════════════════════
    # RULE ENGINE
    # ════════════════════════════════════════════════════════════
    warnings     = []
    risk_reasons = []

    if new_surplus < 0:
        risk_level = "HIGH"
        warnings.append(f"Deficit of ₹{abs(int(new_surplus))}")
        risk_reasons.append("This transaction will put you in deficit")

    if savings_rate < 5 and income > 0:
        risk_level = "HIGH"
        warnings.append("Savings rate critically low")
        risk_reasons.append("Your savings drop below safe threshold")

    if budget_usage > 100:
        risk_level = "HIGH"
        warnings.append("Budget exceeded")
        risk_reasons.append("You are exceeding your monthly budget")
    elif budget_usage > 85:
        if risk_level != "HIGH":
            risk_level = "MEDIUM"
        warnings.append("Budget almost exhausted")
        risk_reasons.append("You are close to budget limit")

    if anomaly:
        if risk_level != "HIGH":
            risk_level = "MEDIUM"
        warnings.append("Unusual spending detected")
        risk_reasons.append("This transaction is unusual compared to past behavior")

    # ── Goal-pressure risk elevation (NEW) ──────────────────────
    if goal_pressure > 70 and tx_type == "expense":
        if risk_level == "LOW":
            risk_level = "MEDIUM"
        warnings.append(f"Goal pressure is high ({goal_pressure:.0f}/100)")
        risk_reasons.append("Your goals are already underfunded — this expense adds pressure")

    # ════════════════════════════════════════════════════════════
    # CATEGORY IMPACT
    # ════════════════════════════════════════════════════════════
    category_warning = None
    new_category_total = category_spent + amount
    if budget > 0 and new_category_total > (0.4 * budget):
        category_warning = f"{category} spending is unusually high this month"

    # ════════════════════════════════════════════════════════════
    # GOAL IMPACT (COMPREHENSIVE — NEW)
    # ════════════════════════════════════════════════════════════
    goal_impact         = []
    goal_impact_detail  = []

    for g in goal_details:
        target  = g["target_amount"]
        saved   = g["saved_amount"]
        name    = g["name"]
        mr      = g.get("monthly_required")
        ml      = g.get("months_left")

        if target <= 0:
            continue

        remaining = max(0.0, target - saved)

        if tx_type == "expense":
            # New surplus after this transaction
            effective_surplus = max(0.0, new_surplus)

            if new_surplus <= 0:
                goal_impact.append(f"'{name}' may be indefinitely delayed — no surplus after this expense")
                goal_impact_detail.append({
                    "goal_name":      name,
                    "status":         "critical",
                    "message":        "No surplus — goal contributions stop",
                    "months_delayed": None,
                })
            elif mr and effective_surplus < mr:
                # How much longer will this goal take?
                if effective_surplus > 0:
                    new_months = round(remaining / effective_surplus, 1)
                    original_months = round(remaining / avg_surplus, 1) if avg_surplus > 0 else new_months
                    delay = round(new_months - original_months, 1)
                    if delay > 0.5:
                        goal_impact.append(
                            f"'{name}' delayed by ~{delay:.0f} month(s) — ₹{int(mr - effective_surplus)} shortfall/mo"
                        )
                        goal_impact_detail.append({
                            "goal_name":        name,
                            "status":           "delayed",
                            "message":          f"Delayed by ~{delay:.0f} month(s)",
                            "monthly_shortfall": round(mr - effective_surplus, 0),
                            "months_delayed":   delay,
                        })
                    else:
                        goal_impact_detail.append({
                            "goal_name": name,
                            "status":    "on_track",
                            "message":   "Minimal impact on this goal",
                        })
                else:
                    goal_impact_detail.append({
                        "goal_name": name,
                        "status":    "critical",
                        "message":   "Goal contributions halted",
                    })
            else:
                goal_impact_detail.append({
                    "goal_name": name,
                    "status":    "on_track",
                    "message":   "No significant impact on this goal",
                })
        else:
            # Income transaction — positive impact
            boost = round((amount / target) * 100, 1)
            if boost > 1:
                goal_impact.append(f"'{name}' could receive ₹{int(amount * 0.2)} boost if 20% saved")
            goal_impact_detail.append({
                "goal_name": name,
                "status":    "improved",
                "message":   f"Surplus increases by ₹{int(amount)} — consider allocating to this goal",
            })

    # ════════════════════════════════════════════════════════════
    # FORECAST IMPACT
    # ════════════════════════════════════════════════════════════
    forecast_warning = None
    if history:
        past     = history[-4:] if len(history) >= 4 else history
        forecast = predict_next_month(past + [new_expense])
        if budget > 0 and forecast > budget:
            forecast_warning = f"Projected monthly spend ₹{int(forecast)} exceeds budget"

    # ════════════════════════════════════════════════════════════
    # RECOMMENDATIONS
    # ════════════════════════════════════════════════════════════
    recommendations = get_recommendations(user_id)[:2]
    if new_surplus < 0:
        recommendations.insert(0, "Avoid this expense or reduce the amount to stay solvent")

    # ════════════════════════════════════════════════════════════
    # PAYMENT DECISION
    # ════════════════════════════════════════════════════════════
    payment = "wallet" if wallet_balance >= amount else "upi"

    # ════════════════════════════════════════════════════════════
    # FINAL RECOMMENDATION
    # ════════════════════════════════════════════════════════════
    if risk_level == "HIGH":
        recommendation = "delay"
    elif risk_level == "MEDIUM":
        recommendation = "caution"
    else:
        recommendation = "proceed"

    return {
        "risk_level":   risk_level.lower(),
        "risk_reason":  risk_reasons,

        "warnings":          warnings,
        "category_warning":  category_warning,
        "goal_impact":       goal_impact,                # list[str] — legacy compat
        "goal_impact_detail": goal_impact_detail,        # list[dict] — NEW detailed
        "forecast_warning":  forecast_warning,

        "recommendation":  recommendation,
        "recommendations": recommendations,

        "payment_suggestion": payment,
        "wallet_balance":     wallet_balance,

        # ── Goal intelligence summary (NEW) ──────────────────
        "goal_pressure":      goal_pressure,
        "goals_at_risk":      sum(1 for g in goal_impact_detail if g.get("status") in ("delayed", "critical")),

        "impact": {
            "new_surplus":         round(new_surplus, 2),
            "budget_after":        round(budget_usage, 1),
            "savings_rate_after":  round(savings_rate, 1),
            "goal_pressure_after": round(goal_pressure, 1),   # unchanged by tx but shown for context
        },
    }