from utils.db import get_db
from datetime import datetime
import calendar

def get_recurring_suggestions(user_id):
    conn = get_db()
    today = datetime.today()
    current_month = today.strftime("%Y-%m")

    # Get transactions grouped by description + amount
    # NOTE: strftime('%d', date) -> EXTRACT(DAY FROM date::date)
    rows = conn.execute("""
        SELECT description, amount, COUNT(*) as cnt,
               AVG(EXTRACT(DAY FROM date::date)) as avg_day
        FROM transactions
        WHERE user_id=%s AND type='expense'
        GROUP BY description, amount
        HAVING COUNT(*) >= 2
    """, (user_id,)).fetchall()

    suggestions = []

    for r in rows:
        description = r["description"]
        amount = r["amount"]
        expected_day = int(float(r["avg_day"]))

        # Check if already added this month
        # NOTE: strftime('%Y-%m', date) -> to_char(date::date, 'YYYY-MM')
        exists = conn.execute("""
            SELECT 1 FROM transactions
            WHERE user_id=%s AND type='expense'
            AND description=%s AND amount=%s
            AND to_char(date::date, 'YYYY-MM')=%s
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