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
CREATE INDEX idx_transactions_user_timestamp
  ON public.transactions (user_id, transaction_timestamp DESC);
CREATE UNIQUE INDEX idx_transactions_user_reference
  ON public.transactions (user_id, reference_id)
  WHERE reference_id IS NOT NULL AND reference_id <> '';
