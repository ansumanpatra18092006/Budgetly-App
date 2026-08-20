"""
utils/db_migrate.py
Run once (or on every startup) to add new tables and columns
introduced by the payment upgrade.  All operations are idempotent.

Called from app.py:  from utils.db_migrate import run_migrations
                     run_migrations()

Migrated to PostgreSQL / Supabase (psycopg, dict_row).
"""

from utils.db import get_db


def run_migrations():
    conn = get_db()
    try:
        _create_wallets(conn)
        _create_wallet_transactions(conn)
        _patch_transactions_status(conn)
        conn.commit()
        print("[migrate] All migrations applied.")
    finally:
        conn.close()


# ── table creators ────────────────────────────────────────────────

def _create_wallets(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wallets (
            id         SERIAL PRIMARY KEY,
            user_id    INTEGER UNIQUE NOT NULL,
            balance    NUMERIC NOT NULL DEFAULT 0,
            updated_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)


def _create_wallet_transactions(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wallet_transactions (
            id          SERIAL PRIMARY KEY,
            sender_id   INTEGER,
            receiver_id INTEGER,
            amount      NUMERIC NOT NULL,
            note        TEXT,
            status      TEXT    NOT NULL DEFAULT 'completed',
            created_at  TEXT    NOT NULL,
            FOREIGN KEY(sender_id)   REFERENCES users(id),
            FOREIGN KEY(receiver_id) REFERENCES users(id)
        )
    """)
    # Index for fast lookups per user
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_wtx_sender
        ON wallet_transactions(sender_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_wtx_receiver
        ON wallet_transactions(receiver_id)
    """)


def _patch_transactions_status(conn):
    """Add status column to transactions table if absent."""
    # PostgreSQL supports IF NOT EXISTS on ADD COLUMN (9.6+), so no
    # try/except-on-duplicate-column dance (sqlite3 needed) is required.
    conn.execute(
        "ALTER TABLE transactions "
        "ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'completed'"
    )
    print("[migrate] Ensured status column exists on transactions.")