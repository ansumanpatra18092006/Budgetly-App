"""Run with PROVENANCE_POSTGRES_TESTS=1 for isolated PostgreSQL integration tests.
All database objects are temporary and every test rolls back. No app startup.
"""
import os
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/unused")
from services.transaction_provenance_service import summarize_provenance


class CoverageTests(unittest.TestCase):
    def test_empty_is_unknown_not_verified(self):
        result = summarize_provenance({})
        self.assertIsNone(result["unverified_percentage"])
        self.assertFalse(result["suspicious_income_burst"])

    def test_percentages_and_burst(self):
        result = summarize_provenance(dict(total_count=4, unverified_count=3,
            total_amount=1000, unverified_amount=900, recent_income_count=3,
            recent_income_amount=900, baseline_income_amount=0))
        self.assertEqual(result["unverified_percentage"], 75)
        self.assertEqual(result["unverified_amount_percentage"], 90)
        self.assertTrue(result["suspicious_income_burst"])

    def test_regular_income_and_unknown_legacy(self):
        result = summarize_provenance(dict(total_count=5, unverified_count=5,
            recent_income_count=3, recent_income_amount=300,
            baseline_income_amount=1200, unknown_recorded_count=2))
        self.assertFalse(result["suspicious_income_burst"])
        self.assertTrue(any("incomplete" in text for text in result["warnings"]))


class BorrowedConnection:
    def __init__(self, conn): self.conn = conn
    def execute(self, *args, **kwargs): return self.conn.execute(*args, **kwargs)
    def close(self): pass
    def commit(self): pass
    def rollback(self): pass


@unittest.skipUnless(os.getenv("PROVENANCE_POSTGRES_TESTS") == "1", "PostgreSQL tests are opt-in")
class PostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        from dotenv import dotenv_values
        url = os.getenv("PROVENANCE_TEST_DATABASE_URL") or dotenv_values(".env")["DATABASE_URL"]
        self.conn = psycopg.connect(url, row_factory=dict_row, connect_timeout=10)
        self.addCleanup(self.conn.close)
        self.addCleanup(self.conn.rollback)
        self.conn.execute("CREATE TEMP TABLE transactions (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, user_id bigint, description text, amount numeric, type text, category text, date date, transaction_timestamp timestamp, reference_id text, utr text, source text)")
        self.conn.execute("CREATE TEMP TABLE loan_applications (id bigint)")
        migration = Path("migrations/003_transaction_provenance.sql").read_text().replace("public.", "pg_temp.")
        self.conn.execute(migration)
        self.conn.execute(migration)  # startup reruns are safe
        borrowed = BorrowedConnection(self.conn)
        for module in ("financial_behavior_service", "recurring_service", "transactions_services", "transaction_provenance_service"):
            self.enterContext(patch("services." + module + ".get_db", return_value=borrowed))

    def insert(self, kind="income", amount=1000, provenance="SELF_REPORTED", day="2026-08-01", recorded=None, user=1):
        return self.conn.execute("""INSERT INTO transactions
          (user_id,description,amount,type,category,date,provenance,verified_at,verification_reference,recorded_at)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
          (user, "salary" if kind == "income" else "rent", amount, kind,
           "Salary" if kind == "income" else "Rent", day, provenance,
           datetime.now(timezone.utc) if provenance == "VERIFIED" else None,
           "independent-bank-record" if provenance == "VERIFIED" else None,
           recorded)).fetchone()["id"]

    def test_manual_income_cannot_improve_capacity_or_behavior(self):
        from services.affordability_service import calculate_affordability
        from services.financial_behavior_service import get_financial_behavior_profile
        from services.recurring_service import analyze_recurring_transactions
        from services.transactions_services import fetch_transactions
        for month in (6, 7, 8):
            self.insert(provenance="VERIFIED", day=f"2026-{month:02d}-01")
            self.insert(kind="expense", amount=300, provenance="VERIFIED", day=f"2026-{month:02d}-02")
        loan = dict(credit_amount=1200, duration_months=12)
        before = calculate_affordability(1, loan)
        behavior = get_financial_behavior_profile(1)
        for month in (6, 7, 8):
            self.insert(amount=1000000, day=f"2026-{month:02d}-01")
            self.insert(kind="expense", amount=900000, day=f"2026-{month:02d}-02")
        after = calculate_affordability(1, loan)
        for key in ("financial_capacity", "affordability", "data_coverage"):
            self.assertEqual(before[key], after[key])
        updated = get_financial_behavior_profile(1)
        for key in ("income", "spending", "recurring", "cash_flow", "behavioral_flags", "summary"):
            self.assertEqual(behavior[key], updated[key])
        self.assertEqual(len(fetch_transactions(1)), 12)
        self.assertEqual(after["transaction_provenance"]["unverified_percentage"], 50)
        self.assertNotEqual(analyze_recurring_transactions(1), analyze_recurring_transactions(1, verified_only=True))

    def test_self_reported_only_is_insufficient(self):
        from services.affordability_service import calculate_affordability
        for month in (6, 7, 8):
            self.insert(day=f"2026-{month:02d}-01")
            self.insert(kind="expense", day=f"2026-{month:02d}-02")
        result = calculate_affordability(1, dict(credit_amount=1, duration_months=100, income=999999999))
        self.assertEqual(result["affordability"]["status"], "insufficient_data")
        self.assertIsNone(result["financial_capacity"]["monthly_income"])

    def test_manual_mutations_cannot_remove_verified_expenses(self):
        from services.transactions_services import update_transaction, delete_transaction, clear_all_transactions, create_transaction
        tid = self.insert(kind="expense", provenance="VERIFIED")
        data = dict(description="salary", amount=999999, category="Salary", type="income", date="2026-08-01", provenance="VERIFIED")
        update_transaction(1, tid, data)
        delete_transaction(1, tid)
        manual = create_transaction(1, "salary", 1000, "income", "Salary", "2026-08-01", source="Verified Bank")
        row = self.conn.execute("SELECT * FROM transactions WHERE id=%s", (manual,)).fetchone()
        self.assertEqual(row["provenance"], "SELF_REPORTED")
        self.assertIsNotNone(row["recorded_at"])
        clear_all_transactions(1)
        rows = self.conn.execute("SELECT * FROM transactions").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["type"], "expense")

    def test_burst_uses_server_entry_time_and_borrower_scope(self):
        from services.transaction_provenance_service import get_transaction_provenance
        anchor = datetime(2026, 9, 10, tzinfo=timezone.utc)
        for offset in (1, 2, 7):
            self.insert(day="2020-01-01", recorded=anchor-timedelta(days=offset))
        self.insert(recorded=anchor+timedelta(seconds=1))
        self.insert(recorded=anchor-timedelta(days=1), user=2)
        result = get_transaction_provenance(1, anchor)
        self.assertTrue(result["suspicious_income_burst"])
        self.assertEqual(result["recent_income_count"], 3)
        self.assertEqual(result["total_count"], 4)

    def test_verification_requires_evidence(self):
        import psycopg
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.conn.execute("INSERT INTO transactions(provenance) VALUES ('VERIFIED')")


if __name__ == "__main__":
    unittest.main()
