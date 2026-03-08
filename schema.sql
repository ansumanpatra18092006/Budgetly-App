CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    description TEXT NOT NULL,
    amount REAL NOT NULL,
    type TEXT NOT NULL,
    category TEXT,
    date TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE INDEX idx_user_date ON transactions(user_id, date);

CREATE TABLE goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    target REAL NOT NULL,
    category TEXT
);

CREATE TABLE IF NOT EXISTS goal_roadmaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER,
    month_number INTEGER,
    target_savings REAL,
    cumulative_amount REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(goal_id) REFERENCES goals(id)
);

rows = conn.execute("""
SELECT
    strftime('%Y-%m', date) AS month,
    COALESCE(category, 'Uncategorized') AS category,
    SUM(amount) AS total
FROM transactions
WHERE user_id = ?
AND type = 'expense'
AND date IS NOT NULL
GROUP BY month, category
ORDER BY month DESC
""", (user_id,)).fetchall()