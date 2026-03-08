from utils.db import get_db
from datetime import datetime, timedelta
import calendar


def get_recommendations(user_id):
    conn = get_db()

    today = datetime.today()
    current_month = today.strftime("%Y-%m")
    last_month = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")

    # =========================
    # CURRENT MONTH DATA
    # =========================
    current_expense = conn.execute("""
        SELECT SUM(amount)
        FROM transactions
        WHERE user_id=? AND type='expense'
        AND strftime('%Y-%m', date)=?
    """, (user_id, current_month)).fetchone()[0] or 0

    last_expense = conn.execute("""
        SELECT SUM(amount)
        FROM transactions
        WHERE user_id=? AND type='expense'
        AND strftime('%Y-%m', date)=?
    """, (user_id, last_month)).fetchone()[0] or 0

    income = conn.execute("""
        SELECT SUM(amount)
        FROM transactions
        WHERE user_id=? AND type='income'
        AND strftime('%Y-%m', date)=?
    """, (user_id, current_month)).fetchone()[0] or 0

    # Budget
    row = conn.execute(
        "SELECT amount FROM budgets WHERE user_id=?",
        (user_id,)
    ).fetchone()
    budget = row["amount"] if row else 0

    # Category totals
    categories = conn.execute("""
        SELECT category, SUM(amount) as total
        FROM transactions
        WHERE user_id=? AND type='expense'
        AND strftime('%Y-%m', date)=?
        GROUP BY category
        ORDER BY total DESC
    """, (user_id, current_month)).fetchall()

    # Today's spending (real-time)
    today_expense = conn.execute("""
        SELECT SUM(amount)
        FROM transactions
        WHERE user_id=? AND type='expense'
        AND date=?
    """, (user_id, today.strftime("%Y-%m-%d"))).fetchone()[0] or 0

    conn.close()

    recommendations = []

    # =================================================
    # 1. Real-time daily burn warning
    # =================================================
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    days_left = days_in_month - today.day

    if today.day > 0:
        daily_avg = current_expense / today.day
        projected = daily_avg * days_in_month

        if budget > 0 and projected > budget:
            recommendations.append(
                f"At your current spending pace, you may exceed your budget by ₹{round(projected - budget)}."
            )

    # =================================================
    # 2. Today's spike detection (real-time feel)
    # =================================================
    if today_expense > 0:
        if today_expense > (current_expense / max(today.day, 1)) * 1.8:
            recommendations.append(
                f"You spent ₹{round(today_expense)} today, which is higher than your usual daily average."
            )

    # =================================================
    # 3. Budget usage status
    # =================================================
    if budget > 0:
        usage = (current_expense / budget) * 100

        if usage > 90:
            recommendations.append(
                f"You’ve used {round(usage)}% of your budget with {days_left} days remaining."
            )
        elif usage > 70:
            recommendations.append(
                f"You’ve spent {round(usage)}% of your monthly budget."
            )

    # =================================================
    # 4. Month-over-month trend
    # =================================================
    if last_expense > 0:
        change = ((current_expense - last_expense) / last_expense) * 100

        if change > 30:
            recommendations.append(
                f"Your spending increased by {round(change)}% compared to last month."
            )
        elif change < -20:
            recommendations.append(
                "Great job! Your spending has reduced significantly from last month."
            )

    # =================================================
    # 5. Category dominance
    # =================================================
    if categories and current_expense > 0:
        top = categories[0]
        percent = (top["total"] / current_expense) * 100

        if percent > 40:
            recommendations.append(
                f"{top['category']} makes up {round(percent)}% of your expenses. Consider optimizing this category."
            )

    # =================================================
    # 6. Savings health
    # =================================================
    if income > 0:
        savings_rate = ((income - current_expense) / income) * 100

        if savings_rate < 10:
            recommendations.append(
                "Your savings rate is low this month. Try reducing discretionary expenses."
            )
        elif savings_rate > 30:
            recommendations.append(
                "Excellent savings discipline this month!"
            )

    # =================================================
    # Default
    # =================================================
    if not recommendations:
        recommendations.append("Your spending pattern looks healthy. Keep it up!")

    return recommendations