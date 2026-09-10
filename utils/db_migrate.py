"""
utils/db_migrate.py
Run startup-safe PostgreSQL migrations required by the application.
"""

from utils.db import get_db


def run_migrations():
    conn = get_db()
    try:
        _patch_transactions_status(conn)
        _patch_transaction_provenance(conn)
        _patch_lender_intelligence(conn)
        _patch_loan_lifecycle(conn)
        _patch_fraud_shield(conn)
        _patch_fraudshield_advanced(conn)
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



def _patch_transaction_provenance(conn):
    """Add server-controlled provenance fields used for lender-grade evidence.

    Existing rows are intentionally NOT promoted to VERIFIED automatically.
    Verification must come from a trusted connector/provider or an authorized
    institutional verification workflow.
    """
    conn.execute(
        """
        ALTER TABLE public.transactions
            ADD COLUMN IF NOT EXISTS verification_status text NOT NULL DEFAULT 'UNVERIFIED',
            ADD COLUMN IF NOT EXISTS verification_source text,
            ADD COLUMN IF NOT EXISTS verified_at timestamp with time zone,
            ADD COLUMN IF NOT EXISTS verification_reference text
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_transactions_user_verification
        ON public.transactions (user_id, verification_status, date DESC)
        """
    )
    # Normalize legacy rows without pretending that old user-entered data was
    # independently verified. Source labels are for provenance visibility only.
    conn.execute(
        """
        UPDATE public.transactions
        SET verification_status = CASE
                WHEN verification_status IS NULL OR verification_status = '' THEN 'UNVERIFIED'
                ELSE verification_status
            END,
            verification_source = COALESCE(NULLIF(verification_source, ''),
                CASE
                    WHEN LOWER(COALESCE(source,'')) LIKE '%statement%' THEN 'DOCUMENT_IMPORT'
                    WHEN LOWER(COALESCE(source,'')) LIKE '%csv%' THEN 'CSV_IMPORT'
                    WHEN LOWER(COALESCE(source,'')) LIKE '%upi%' THEN 'UPI_USER_FLOW'
                    ELSE 'LEGACY_RECORD'
                END)
        WHERE verification_source IS NULL OR verification_source = ''
        """
    )
    print("[migrate] Ensured transaction provenance schema exists.")

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


def _patch_fraud_shield(conn):
    """Ensure the isolated FraudShield transaction + investigation domain exists.

    FraudShield deliberately does not reference borrower-side transactions or
    loan applications. The institution sees only rows owned by its own
    FraudShield stream.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS public.fraudshield_transactions (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            institution_id bigint NOT NULL REFERENCES public.users(id),
            account_ref text NOT NULL,
            merchant text NOT NULL,
            amount numeric(14,2) NOT NULL CHECK (amount > 0),
            transaction_timestamp timestamp with time zone NOT NULL,
            location text,
            device_id text,
            account_age_days integer NOT NULL DEFAULT 365,
            txn_count_1h integer NOT NULL DEFAULT 1,
            txn_count_24h integer NOT NULL DEFAULT 3,
            location_distance_km double precision NOT NULL DEFAULT 0,
            is_new_device boolean NOT NULL DEFAULT false,
            merchant_risk double precision NOT NULL DEFAULT 0,
            transaction_hour integer NOT NULL DEFAULT 12,
            relationship_count integer NOT NULL DEFAULT 1,
            merchant_seen_before boolean NOT NULL DEFAULT true,
            device_trust_score double precision NOT NULL DEFAULT 0.9,
            time_deviation_hours double precision NOT NULL DEFAULT 1.0,
            amount_to_account_median double precision NOT NULL DEFAULT 1.0,
            network_risk double precision NOT NULL DEFAULT 0.05,
            channel text NOT NULL DEFAULT 'UPI',
            created_at timestamp with time zone NOT NULL DEFAULT now()
        )
    """)

    # Upgrade an already-created table without touching any borrower-side data.
    for statement in [
        "ALTER TABLE public.fraudshield_transactions ADD COLUMN IF NOT EXISTS merchant_seen_before boolean NOT NULL DEFAULT true",
        "ALTER TABLE public.fraudshield_transactions ADD COLUMN IF NOT EXISTS device_trust_score double precision NOT NULL DEFAULT 0.9",
        "ALTER TABLE public.fraudshield_transactions ADD COLUMN IF NOT EXISTS time_deviation_hours double precision NOT NULL DEFAULT 1.0",
        "ALTER TABLE public.fraudshield_transactions ADD COLUMN IF NOT EXISTS amount_to_account_median double precision NOT NULL DEFAULT 1.0",
        "ALTER TABLE public.fraudshield_transactions ADD COLUMN IF NOT EXISTS network_risk double precision NOT NULL DEFAULT 0.05",
        "ALTER TABLE public.fraudshield_transactions ADD COLUMN IF NOT EXISTS channel text NOT NULL DEFAULT 'UPI'",
    ]:
        conn.execute(statement)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_fraudshield_transactions_institution_time
        ON public.fraudshield_transactions (institution_id, transaction_timestamp DESC)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS public.fraudshield_investigations (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            transaction_id bigint NOT NULL REFERENCES public.fraudshield_transactions(id),
            institution_id bigint NOT NULL REFERENCES public.users(id),
            risk_score integer NOT NULL,
            risk_level text NOT NULL,
            status text NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','REVIEWED','ESCALATED','CLOSED')),
            opened_at timestamp with time zone NOT NULL DEFAULT now(),
            updated_at timestamp with time zone NOT NULL DEFAULT now(),
            history jsonb NOT NULL DEFAULT '[]'::jsonb
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_fraudshield_investigations_institution_time
        ON public.fraudshield_investigations (institution_id, opened_at DESC)
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_fraudshield_open_transaction
        ON public.fraudshield_investigations (transaction_id)
        WHERE status != 'CLOSED'
    """)
    print("[migrate] Ensured independent FraudShield transaction + investigation schema exists.")



def _patch_fraudshield_advanced(conn):
    """Add post-Phase-1 FraudShield intelligence, integrity, and observability schema."""
    statements = [
        "ALTER TABLE public.fraudshield_transactions ADD COLUMN IF NOT EXISTS transaction_ref text",
        "ALTER TABLE public.fraudshield_transactions ADD COLUMN IF NOT EXISTS utr text",
        "ALTER TABLE public.fraudshield_transactions ADD COLUMN IF NOT EXISTS ip_address text",
        "ALTER TABLE public.fraudshield_transactions ADD COLUMN IF NOT EXISTS browser text",
        "ALTER TABLE public.fraudshield_transactions ADD COLUMN IF NOT EXISTS os text",
        "ALTER TABLE public.fraudshield_transactions ADD COLUMN IF NOT EXISTS beneficiary_ref text",
        "ALTER TABLE public.fraudshield_transactions ADD COLUMN IF NOT EXISTS failed_auth_count integer NOT NULL DEFAULT 0",
        "ALTER TABLE public.fraudshield_transactions ADD COLUMN IF NOT EXISTS previous_location text",
        "ALTER TABLE public.fraudshield_transactions ADD COLUMN IF NOT EXISTS previous_transaction_timestamp timestamp with time zone",
        "ALTER TABLE public.fraudshield_investigations ADD COLUMN IF NOT EXISTS assigned_analyst text",
        "ALTER TABLE public.fraudshield_investigations ADD COLUMN IF NOT EXISTS analyst_notes text",
        "ALTER TABLE public.fraudshield_investigations ADD COLUMN IF NOT EXISTS outcome text",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_fraudshield_transaction_ref ON public.fraudshield_transactions(institution_id, transaction_ref) WHERE transaction_ref IS NOT NULL",
    ]
    for stmt in statements:
        conn.execute(stmt)
    conn.execute("UPDATE public.fraudshield_transactions SET transaction_ref = COALESCE(transaction_ref, 'FS-TXN-' || id::text) WHERE transaction_ref IS NULL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS public.fraudshield_audit_events (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            institution_id bigint NOT NULL REFERENCES public.users(id),
            event_type text NOT NULL,
            entity_type text,
            entity_id text,
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamp with time zone NOT NULL DEFAULT now()
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fraudshield_audit_time ON public.fraudshield_audit_events(institution_id, created_at DESC)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS public.fraudshield_replay_registry (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            institution_id bigint NOT NULL REFERENCES public.users(id),
            fingerprint text NOT NULL,
            transaction_ref text,
            first_seen timestamp with time zone NOT NULL DEFAULT now(),
            last_seen timestamp with time zone NOT NULL DEFAULT now(),
            duplicate_count integer NOT NULL DEFAULT 0,
            UNIQUE(institution_id, fingerprint)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS public.fraudshield_network_edges (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            institution_id bigint NOT NULL REFERENCES public.users(id),
            source_ref text NOT NULL,
            target_ref text NOT NULL,
            relationship_type text NOT NULL,
            weight double precision NOT NULL DEFAULT 0.5,
            last_seen timestamp with time zone NOT NULL DEFAULT now(),
            UNIQUE(institution_id, source_ref, target_ref, relationship_type)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fraudshield_network_inst ON public.fraudshield_network_edges(institution_id, last_seen DESC)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS public.fraudshield_model_runs (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            institution_id bigint NOT NULL REFERENCES public.users(id),
            model_version text NOT NULL,
            dataset text,
            training_records integer,
            metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
            status text NOT NULL DEFAULT 'READY',
            created_at timestamp with time zone NOT NULL DEFAULT now()
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fraudshield_model_runs_inst ON public.fraudshield_model_runs(institution_id, created_at DESC)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS public.fraudshield_consumer_signals (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            institution_id bigint REFERENCES public.users(id),
            consumer_id bigint NOT NULL REFERENCES public.users(id),
            source_transaction_id bigint,
            event_type text NOT NULL DEFAULT 'USER_REPORTED_SUSPICIOUS'
                CHECK (event_type IN ('USER_REPORTED_SUSPICIOUS','USER_CONFIRMED_FRAUD','USER_CONFIRMED_LEGIT')),
            amount numeric(14,2),
            merchant text,
            category text,
            transaction_timestamp timestamp with time zone,
            transaction_reference text,
            note text,
            status text NOT NULL DEFAULT 'NEW'
                CHECK (status IN ('NEW','ACKNOWLEDGED','RESOLVED')),
            consent_scope text NOT NULL DEFAULT 'EXPLICIT_USER_REPORT',
            created_at timestamp with time zone NOT NULL DEFAULT now()
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_fraudshield_consumer_signal_unique
        ON public.fraudshield_consumer_signals(consumer_id, source_transaction_id, event_type)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_fraudshield_consumer_signals_inst_time
        ON public.fraudshield_consumer_signals(institution_id, created_at DESC)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_fraudshield_consumer_signals_consumer_time
        ON public.fraudshield_consumer_signals(consumer_id, created_at DESC)
    """)
    print("[migrate] Ensured consumer-to-FraudShield security signal schema exists.")
