"""
utils/db_migrate.py
Run startup-safe PostgreSQL migrations required by the application.
"""

from utils.db import get_db


def run_migrations():
    conn = get_db()
    try:
        _patch_transactions_status(conn)
        _patch_lender_intelligence(conn)
        _patch_loan_lifecycle(conn)
        conn.commit()
        print("[migrate] All migrations applied.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _patch_transactions_status(conn):
    """Add status column to transactions table if absent."""
    conn.execute(
        "ALTER TABLE public.transactions "
        "ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'completed'"
    )
    print("[migrate] Ensured transactions.status exists.")


def _patch_lender_intelligence(conn):
    """Ensure lender portfolio/audit schema exists in PostgreSQL/Supabase.

    This migration is intentionally idempotent because it runs at application
    startup. It repairs environments where schema.sql was not re-applied after
    the lender intelligence feature was added.
    """
    # Assessment/decision state on the authoritative loan application record.
    conn.execute(
        """
        ALTER TABLE public.loan_applications
            ADD COLUMN IF NOT EXISTS assessment_result jsonb,
            ADD COLUMN IF NOT EXISTS assessed_at timestamp with time zone,
            ADD COLUMN IF NOT EXISTS decided_at timestamp with time zone,
            ADD COLUMN IF NOT EXISTS decided_by bigint
        """
    )

    # Immutable lender governance events used by the Portfolio Risk and
    # Decision Ledger views.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS public.lender_decision_audit (
            id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
            lender_id bigint NOT NULL,
            application_id bigint NOT NULL,
            borrower_id bigint NOT NULL,
            event_type text NOT NULL CHECK (
                event_type = ANY (
                    ARRAY['ASSESSMENT'::text, 'DECISION'::text, 'SCENARIO'::text]
                )
            ),
            model_version text,
            risk_probability double precision,
            risk_level text,
            ai_decision text,
            lender_decision text,
            reason_summary jsonb NOT NULL DEFAULT '[]'::jsonb,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamp with time zone NOT NULL DEFAULT now(),
            CONSTRAINT lender_decision_audit_pkey PRIMARY KEY (id),
            CONSTRAINT lender_decision_audit_lender_id_fkey
                FOREIGN KEY (lender_id) REFERENCES public.users(id),
            CONSTRAINT lender_decision_audit_application_id_fkey
                FOREIGN KEY (application_id) REFERENCES public.loan_applications(id),
            CONSTRAINT lender_decision_audit_borrower_id_fkey
                FOREIGN KEY (borrower_id) REFERENCES public.users(id)
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_lender_audit_application_time
        ON public.lender_decision_audit (application_id, created_at DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_lender_audit_lender_time
        ON public.lender_decision_audit (lender_id, created_at DESC)
        """
    )

    print("[migrate] Ensured lender intelligence schema exists.")


def _patch_loan_lifecycle(conn):
    """Ensure the demo loan-sanction/disbursement/repayment lifecycle schema."""
    conn.execute(
        """
        ALTER TABLE public.loan_applications
            ADD COLUMN IF NOT EXISTS approved_amount double precision,
            ADD COLUMN IF NOT EXISTS interest_rate double precision,
            ADD COLUMN IF NOT EXISTS emi_amount double precision,
            ADD COLUMN IF NOT EXISTS loan_term_months integer,
            ADD COLUMN IF NOT EXISTS loan_state text NOT NULL DEFAULT 'APPLICATION',
            ADD COLUMN IF NOT EXISTS disbursed_at timestamp with time zone,
            ADD COLUMN IF NOT EXISTS outstanding_amount double precision
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS public.loan_repayments (
            id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
            application_id bigint NOT NULL,
            borrower_id bigint NOT NULL,
            amount double precision NOT NULL CHECK (amount > 0),
            remaining_amount double precision NOT NULL CHECK (remaining_amount >= 0),
            paid_at timestamp with time zone NOT NULL DEFAULT now(),
            CONSTRAINT loan_repayments_pkey PRIMARY KEY (id),
            CONSTRAINT loan_repayments_application_fkey
                FOREIGN KEY (application_id) REFERENCES public.loan_applications(id),
            CONSTRAINT loan_repayments_borrower_fkey
                FOREIGN KEY (borrower_id) REFERENCES public.users(id)
        )
        """
    )
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_loan_repayments_application_time
        ON public.loan_repayments (application_id, paid_at DESC)
    """)
    conn.execute("""
        UPDATE public.loan_applications
        SET loan_state = CASE
            WHEN status = 'APPROVED' AND loan_state = 'APPLICATION' THEN 'SANCTIONED'
            WHEN status = 'REJECTED' THEN 'REJECTED'
            WHEN status = 'WITHDRAWN' THEN 'WITHDRAWN'
            ELSE loan_state
        END
        WHERE loan_state IS NULL OR loan_state = 'APPLICATION'
    """)
    # Backfill terms for legacy APPROVED applications created before the
    # lifecycle columns existed. Keep the same demo defaults as new approvals.
    conn.execute("""
        UPDATE public.loan_applications
        SET approved_amount = COALESCE(approved_amount, NULLIF(application_data->>'credit_amount', '')::double precision),
            interest_rate = COALESCE(interest_rate, 12.0),
            loan_term_months = COALESCE(loan_term_months, NULLIF(application_data->>'duration_months', '')::integer)
        WHERE status = 'APPROVED'
          AND (approved_amount IS NULL OR interest_rate IS NULL OR loan_term_months IS NULL)
    """)
    conn.execute("""
        UPDATE public.loan_applications
        SET emi_amount = CASE
                WHEN approved_amount IS NULL OR loan_term_months IS NULL OR loan_term_months < 1 THEN NULL
                WHEN COALESCE(interest_rate, 12.0) = 0 THEN approved_amount / loan_term_months
                ELSE approved_amount * (COALESCE(interest_rate, 12.0) / 12.0 / 100.0)
                     * POWER(1.0 + (COALESCE(interest_rate, 12.0) / 12.0 / 100.0), loan_term_months)
                     / (POWER(1.0 + (COALESCE(interest_rate, 12.0) / 12.0 / 100.0), loan_term_months) - 1.0)
            END,
            outstanding_amount = COALESCE(outstanding_amount, approved_amount)
        WHERE status = 'APPROVED'
          AND loan_state = 'SANCTIONED'
    """)
    print("[migrate] Ensured loan lifecycle schema exists.")
