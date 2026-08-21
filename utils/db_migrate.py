"""
utils/db_migrate.py
Run once (or on every startup) to add required database changes.
Migrated to PostgreSQL / Supabase (psycopg, dict_row).
"""

from utils.db import get_db


def run_migrations():
    conn = get_db()
    try:
        _patch_transactions_status(conn)
        conn.commit()
        print("[migrate] All migrations applied.")
    finally:
        conn.close()


def _patch_transactions_status(conn):
    """Add status column to transactions table if absent."""
    conn.execute(
        "ALTER TABLE transactions "
        "ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'completed'"
    )
    print("[migrate] Ensured status column exists on transactions.")