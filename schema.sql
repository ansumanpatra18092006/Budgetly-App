-- WARNING: This schema is for context only and is not meant to be run.
-- Table order and constraints may not be valid for execution.

CREATE TABLE public.users (
  id bigint NOT NULL DEFAULT nextval('users_id_seq'::regclass),
  name text,
  email text UNIQUE,
  password text,
  reset_token text,
  reset_expiry timestamp without time zone,
  role text NOT NULL DEFAULT 'consumer'::text CHECK (role = ANY (ARRAY['consumer'::text, 'lender'::text, 'admin'::text])),
  status text NOT NULL DEFAULT 'active'::text CHECK (status = ANY (ARRAY['active'::text, 'disabled'::text])),
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  invitation_token text,
  invitation_expiry timestamp with time zone,
  invitation_used boolean NOT NULL DEFAULT false,
  invitation_email_status text,
  CONSTRAINT users_pkey PRIMARY KEY (id)
);
CREATE TABLE public.transactions (
  id bigint NOT NULL DEFAULT nextval('transactions_id_seq'::regclass),
  user_id bigint,
  description text,
  amount numeric,
  type text,
  category text,
  date date,
  status text DEFAULT 'completed'::text,
  transaction_timestamp timestamp without time zone,
  reference_id text,
  utr text,
  source text,
  verification_status text NOT NULL DEFAULT 'UNVERIFIED'::text,
  verification_source text,
  verified_at timestamp with time zone,
  verification_reference text,
  CONSTRAINT transactions_pkey PRIMARY KEY (id),
  CONSTRAINT transactions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)
);
CREATE TABLE public.budgets (
  user_id bigint NOT NULL,
  amount numeric,
  CONSTRAINT budgets_pkey PRIMARY KEY (user_id),
  CONSTRAINT budgets_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)
);
CREATE TABLE public.goals (
  id bigint NOT NULL DEFAULT nextval('goals_id_seq'::regclass),
  user_id bigint,
  name text,
  target_amount numeric,
  saved_amount numeric DEFAULT 0,
  category text,
  target_date date,
  created_at timestamp without time zone,
  status text DEFAULT 'active'::text,
  CONSTRAINT goals_pkey PRIMARY KEY (id),
  CONSTRAINT goals_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)
);
CREATE TABLE public.user_category_map (
  id bigint NOT NULL DEFAULT nextval('user_category_map_id_seq'::regclass),
  user_id bigint NOT NULL,
  merchant text NOT NULL,
  category text NOT NULL,
  CONSTRAINT user_category_map_pkey PRIMARY KEY (id),
  CONSTRAINT user_category_map_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)
);
CREATE TABLE public.recurring_suggestion_state (
  id integer NOT NULL DEFAULT nextval('recurring_suggestion_state_id_seq'::regclass),
  user_id integer NOT NULL,
  suggestion_key character varying NOT NULL,
  occurrence_period character varying NOT NULL,
  status character varying NOT NULL CHECK (status::text = ANY (ARRAY['dismissed'::character varying, 'added'::character varying]::text[])),
  handled_at timestamp without time zone NOT NULL DEFAULT now(),
  CONSTRAINT recurring_suggestion_state_pkey PRIMARY KEY (id),
  CONSTRAINT recurring_suggestion_state_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)
);
CREATE TABLE public.loan_applications (
  id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
  borrower_id bigint NOT NULL,
  lender_id bigint NOT NULL,
  application_data jsonb NOT NULL,
  status text NOT NULL DEFAULT 'PENDING'::text CHECK (status = ANY (ARRAY['PENDING'::text, 'APPROVED'::text, 'REJECTED'::text, 'WITHDRAWN'::text])),
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  updated_at timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT loan_applications_pkey PRIMARY KEY (id),
  CONSTRAINT loan_applications_borrower_id_fkey FOREIGN KEY (borrower_id) REFERENCES public.users(id),
  CONSTRAINT loan_applications_lender_id_fkey FOREIGN KEY (lender_id) REFERENCES public.users(id)
);
CREATE INDEX idx_transactions_user_verification
  ON public.transactions (user_id, verification_status, date DESC);
CREATE INDEX idx_transactions_user_timestamp
  ON public.transactions (user_id, transaction_timestamp DESC);
CREATE UNIQUE INDEX idx_transactions_user_reference
  ON public.transactions (user_id, reference_id)
  WHERE reference_id IS NOT NULL AND reference_id <> '';


-- Lender intelligence / governance extensions
ALTER TABLE public.loan_applications
  ADD COLUMN IF NOT EXISTS assessment_result jsonb,
  ADD COLUMN IF NOT EXISTS assessed_at timestamp with time zone,
  ADD COLUMN IF NOT EXISTS decided_at timestamp with time zone,
  ADD COLUMN IF NOT EXISTS decided_by bigint;
ALTER TABLE public.loan_applications
  ADD COLUMN IF NOT EXISTS approved_amount double precision,
  ADD COLUMN IF NOT EXISTS interest_rate double precision,
  ADD COLUMN IF NOT EXISTS emi_amount double precision,
  ADD COLUMN IF NOT EXISTS loan_term_months integer,
  ADD COLUMN IF NOT EXISTS loan_state text NOT NULL DEFAULT 'APPLICATION',
  ADD COLUMN IF NOT EXISTS disbursed_at timestamp with time zone,
  ADD COLUMN IF NOT EXISTS outstanding_amount double precision;

CREATE TABLE IF NOT EXISTS public.loan_repayments (
  id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
  application_id bigint NOT NULL,
  borrower_id bigint NOT NULL,
  amount double precision NOT NULL CHECK (amount > 0),
  remaining_amount double precision NOT NULL CHECK (remaining_amount >= 0),
  paid_at timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT loan_repayments_pkey PRIMARY KEY (id),
  CONSTRAINT loan_repayments_application_fkey FOREIGN KEY (application_id) REFERENCES public.loan_applications(id),
  CONSTRAINT loan_repayments_borrower_fkey FOREIGN KEY (borrower_id) REFERENCES public.users(id)
);
CREATE INDEX IF NOT EXISTS idx_loan_repayments_application_time
  ON public.loan_repayments (application_id, paid_at DESC);

CREATE TABLE IF NOT EXISTS public.lender_decision_audit (
  id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
  lender_id bigint NOT NULL,
  application_id bigint NOT NULL,
  borrower_id bigint NOT NULL,
  event_type text NOT NULL CHECK (event_type = ANY (ARRAY['ASSESSMENT'::text, 'DECISION'::text, 'SCENARIO'::text])),
  model_version text,
  risk_probability double precision,
  risk_level text,
  ai_decision text,
  lender_decision text,
  reason_summary jsonb NOT NULL DEFAULT '[]'::jsonb,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT lender_decision_audit_pkey PRIMARY KEY (id),
  CONSTRAINT lender_decision_audit_lender_id_fkey FOREIGN KEY (lender_id) REFERENCES public.users(id),
  CONSTRAINT lender_decision_audit_application_id_fkey FOREIGN KEY (application_id) REFERENCES public.loan_applications(id),
  CONSTRAINT lender_decision_audit_borrower_id_fkey FOREIGN KEY (borrower_id) REFERENCES public.users(id)
);
CREATE INDEX IF NOT EXISTS idx_lender_audit_application_time
  ON public.lender_decision_audit (application_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_lender_audit_lender_time
  ON public.lender_decision_audit (lender_id, created_at DESC);

-- FraudShield — independent institution-owned transaction intelligence domain
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
);
CREATE INDEX IF NOT EXISTS idx_fraudshield_transactions_institution_time
  ON public.fraudshield_transactions (institution_id, transaction_timestamp DESC);

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
);
CREATE INDEX IF NOT EXISTS idx_fraudshield_investigations_institution_time
  ON public.fraudshield_investigations (institution_id, opened_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fraudshield_open_transaction
  ON public.fraudshield_investigations (transaction_id)
  WHERE status != 'CLOSED';


-- ============================================================
-- FraudShield post-Phase-1 intelligence / security extensions
-- ============================================================
ALTER TABLE public.fraudshield_transactions ADD COLUMN IF NOT EXISTS transaction_ref text;
ALTER TABLE public.fraudshield_transactions ADD COLUMN IF NOT EXISTS utr text;
ALTER TABLE public.fraudshield_transactions ADD COLUMN IF NOT EXISTS ip_address text;
ALTER TABLE public.fraudshield_transactions ADD COLUMN IF NOT EXISTS browser text;
ALTER TABLE public.fraudshield_transactions ADD COLUMN IF NOT EXISTS os text;
ALTER TABLE public.fraudshield_transactions ADD COLUMN IF NOT EXISTS beneficiary_ref text;
ALTER TABLE public.fraudshield_transactions ADD COLUMN IF NOT EXISTS failed_auth_count integer NOT NULL DEFAULT 0;
ALTER TABLE public.fraudshield_transactions ADD COLUMN IF NOT EXISTS previous_location text;
ALTER TABLE public.fraudshield_transactions ADD COLUMN IF NOT EXISTS previous_transaction_timestamp timestamp with time zone;
ALTER TABLE public.fraudshield_investigations ADD COLUMN IF NOT EXISTS assigned_analyst text;
ALTER TABLE public.fraudshield_investigations ADD COLUMN IF NOT EXISTS analyst_notes text;
ALTER TABLE public.fraudshield_investigations ADD COLUMN IF NOT EXISTS outcome text;

CREATE UNIQUE INDEX IF NOT EXISTS idx_fraudshield_transaction_ref
ON public.fraudshield_transactions(institution_id, transaction_ref)
WHERE transaction_ref IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.fraudshield_audit_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES public.users(id),
  event_type text NOT NULL,
  entity_type text,
  entity_id text,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamp with time zone NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.fraudshield_replay_registry (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES public.users(id),
  fingerprint text NOT NULL,
  transaction_ref text,
  first_seen timestamp with time zone NOT NULL DEFAULT now(),
  last_seen timestamp with time zone NOT NULL DEFAULT now(),
  duplicate_count integer NOT NULL DEFAULT 0,
  UNIQUE(institution_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS public.fraudshield_network_edges (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES public.users(id),
  source_ref text NOT NULL,
  target_ref text NOT NULL,
  relationship_type text NOT NULL,
  weight double precision NOT NULL DEFAULT 0.5,
  last_seen timestamp with time zone NOT NULL DEFAULT now(),
  UNIQUE(institution_id, source_ref, target_ref, relationship_type)
);

CREATE TABLE IF NOT EXISTS public.fraudshield_model_runs (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES public.users(id),
  model_version text NOT NULL,
  dataset text,
  training_records integer,
  metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'READY',
  created_at timestamp with time zone NOT NULL DEFAULT now()
);

-- Phase 2: per-institution configurable fraud detection policy
CREATE TABLE IF NOT EXISTS public.fraudshield_rule_configs (
  institution_id bigint PRIMARY KEY REFERENCES public.users(id),
  rules jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamp with time zone NOT NULL DEFAULT now()
);

-- FraudShield Phase 2 autonomous response
CREATE TABLE IF NOT EXISTS public.fraudshield_response_configs (
  institution_id bigint PRIMARY KEY REFERENCES public.users(id),
  policy jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamp with time zone NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.fraudshield_response_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES public.users(id),
  transaction_id bigint NOT NULL REFERENCES public.fraudshield_transactions(id),
  case_id bigint NULL REFERENCES public.fraudshield_investigations(id),
  risk_score integer NOT NULL,
  action text NOT NULL,
  state text NOT NULL DEFAULT 'ACTIVE',
  notification_status text NOT NULL DEFAULT 'NOT_REQUIRED',
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  updated_at timestamp with time zone NOT NULL DEFAULT now(),
  UNIQUE(institution_id, transaction_id)
);
