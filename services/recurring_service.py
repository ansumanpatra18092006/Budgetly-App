from utils.db import get_db
from datetime import datetime
import calendar

def get_recurring_suggestions(user_id):
    conn = get_db()
    today = datetime.today()
    current_month = today.strftime("%Y-%m")

    # Get transactions grouped by description + amount
    rows = conn.execute("""
        SELECT description, amount, COUNT(*) as cnt,
               AVG(strftime('%d', date)) as avg_day
        FROM transactions
        WHERE user_id=? AND type='expense'
        GROUP BY description, amount
        HAVING cnt >= 2
    """, (user_id,)).fetchall()

    suggestions = []

    for r in rows:
        description = r["description"]
        amount = r["amount"]
        expected_day = int(float(r["avg_day"]))

        # Check if already added this month
        exists = conn.execute("""
            SELECT 1 FROM transactions
            WHERE user_id=? AND type='expense'
            AND description=? AND amount=?
            AND strftime('%Y-%m', date)=?
        """, (user_id, description, amount, current_month)).fetchone()

        if exists:
            continue

        # If today is within ±3 days of expected date
        if abs(today.day - expected_day) <= 3:
            suggestions.append({
                "description": description,
                "amount": amount,
                "expected_day": expected_day
            })

    conn.close()
    return suggestions