"""
utils/db.py
SQLite helper for Budgetly.

init_db() is safe to call on every startup:
  - Creates tables that don't exist yet.
  - Migrates the goals table to add target_date / created_at if absent.
"""

import sqlite3

DATABASE = "budget.db"


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    # ── Core tables ──────────────────────────────────────────────

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            name     TEXT,
            email    TEXT UNIQUE,
            password TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            description TEXT,
            amount      REAL,
            type        TEXT,
            category    TEXT,
            date        TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS budgets (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            amount  REAL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS goals (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER,
            name          TEXT,
            target_amount REAL,
            saved_amount  REAL DEFAULT 0,
            category      TEXT,
            target_date   TEXT,
            created_at    TEXT
        )
    """)

    conn.commit()

    # ── Runtime migrations ────────────────────────────────────────
    # Adds columns to existing databases that were created before these
    # fields existed.  ALTER TABLE ADD COLUMN is idempotent-safe when
    # wrapped in a try/except (SQLite raises OperationalError if the
    # column already exists).

    _add_column_if_missing(conn, "goals", "target_date", "TEXT")
    _add_column_if_missing(conn, "goals", "created_at",  "TEXT")

    conn.commit()
    conn.close()


def _add_column_if_missing(conn, table: str, column: str, col_type: str) -> None:
    """Add *column* to *table* if it does not already exist."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    except Exception:
        # Column already present — nothing to do.
        pass