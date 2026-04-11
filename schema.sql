CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            email TEXT UNIQUE,
            password TEXT
        , reset_token TEXT, reset_expiry DATETIME);
CREATE TABLE sqlite_sequence(name,seq);
CREATE TABLE transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            description TEXT,
            amount REAL,
            typD5Ne TEXT,
            category TEXT,
            date TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
CREATE TABLE budgets (
            user_id INTEGER PRIMARY KEY,
            amount REAL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
CREATE TABLE goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT,
            target_amount REAL,
            saved_amount REAL DEFAULT 0,
            category TEXT, target_date TEXT, created_at TEXT, status TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
CREATE INDEX idx_goals_user ON goals(user_id);
CREATE INDEX idx_transactions_user_date ON transactions(user_id, date);
CREATE UNIQUE INDEX idx_unique_transaction
ON transactions (user_id, amount, date, description);
CREATE TABLE user_category_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    merchant TEXT NOT NULL,
    category TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id),
    UNIQUE(user_id, merchant)
);