# ============================================================
# FILE: ml/recommender.py  [GOAL-AWARE RECOMMENDATIONS]
# ============================================================
# Drop-in replacement. Import path unchanged.
# ============================================================

from utils.db import get_db
from datetime import datetime


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
            WHERE user_id=? AND date>=?
        """, (user_id, month_start)).fetchone()

        income  = float(row["income"]  or 0)
        expense = float(row["expense"] or 0)
        surplus = income - expense

        budget_row = conn.execute(
            "SELECT amount FROM budgets WHERE user_id=?", (user_id,)
        ).fetchone()
        budget = float(budget_row["amount"]) if budget_row else 0.0

        # ── Top spending category ────────────────────────────────
        top_cat_row = conn.execute("""
            SELECT COALESCE(category,'Misc') AS category,
                   SUM(amount) AS total
            FROM transactions
            WHERE user_id=? AND type='expense' AND date>=?
            GROUP BY category ORDER BY total DESC LIMIT 1
        """, (user_id, month_start)).fetchone()

        top_cat       = top_cat_row["category"] if top_cat_row else "discretionary spending"
        top_cat_spend = float(top_cat_row["total"]) if top_cat_row else 0.0

        # ── Goals data ───────────────────────────────────────────
        goal_rows = conn.execute("""
            SELECT name, target_amount, saved_amount, target_date
            FROM goals WHERE user_id=? ORDER BY id ASC
        """, (user_id,)).fetchall()

        # ── Average monthly cash flow (3 months) ─────────────────
        hist = conn.execute("""
            SELECT strftime('%Y-%m',date) AS month,
                   SUM(CASE WHEN type='income'  THEN amount ELSE 0 END) AS inc,
                   SUM(CASE WHEN type='expense' THEN amount ELSE 0 END) AS exp
            FROM transactions WHERE user_id=?
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