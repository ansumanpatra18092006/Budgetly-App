"""
utils/db.py
PostgreSQL helper for Budgetly (Supabase).

MIGRATION NOTES (SQLite -> PostgreSQL):
- Parameter placeholders: '?' becomes '%s'
- Insert IDs: 'cursor.lastrowid' is removed. Use 'RETURNING id' combined with 'cursor.fetchone()["id"]'
- Row factory: 'sqlite3.Row' is replaced by 'dict_row' (psycopg) to preserve dictionary-like access.
"""

import os
import psycopg
from psycopg.rows import dict_row
from contextlib import contextmanager

# Read from environment
DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not configured")

def get_db():
    """
    Establishes and returns a connection to Supabase PostgreSQL.
    """
    # dict_row ensures results act like sqlite3.Row (dictionary-like access).
    # autocommit=False ensures we manually commit transactions, preserving the 
    # original SQLite transaction boundaries and preventing partial data writes.
    conn = psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
        autocommit=False
    )
    return conn

def init_db():
    """
    Verifies the database connection on startup.
    Schema creation is managed externally via Supabase SQL Editor.
    """
    try:
        with get_db() as conn:
            conn.execute("SELECT 1")
            
        print("✅ Connected to Supabase PostgreSQL")
        
    except Exception:
        print("❌ Supabase connection failed")
        raise

@contextmanager
def db_cursor():
    """
    Context manager for robust database transaction handling.
    Automatically handles yielding the cursor, committing on success,
    rolling back on failure, and safely closing the connection.
    
    Usage:
        with db_cursor() as cursor:
            cursor.execute("SELECT * FROM users")
    """
    conn = get_db()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()