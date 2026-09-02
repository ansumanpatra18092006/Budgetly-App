-- FinTrust transaction metadata migration
-- Run once in Supabase SQL Editor before deploying the backend changes.

ALTER TABLE public.transactions
  ADD COLUMN IF NOT EXISTS transaction_timestamp timestamp without time zone;

ALTER TABLE public.transactions
  ADD COLUMN IF NOT EXISTS reference_id text;

ALTER TABLE public.transactions
  ADD COLUMN IF NOT EXISTS utr text;

ALTER TABLE public.transactions
  ADD COLUMN IF NOT EXISTS source text;

CREATE INDEX IF NOT EXISTS idx_transactions_user_timestamp
  ON public.transactions (user_id, transaction_timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_transactions_reference_id
  ON public.transactions (user_id, reference_id)
  WHERE reference_id IS NOT NULL;

-- The old date/amount/description uniqueness rule is too strict: it rejects
-- legitimate same-amount transactions on the same day (e.g. several ₹1 payments).
DROP INDEX IF EXISTS public.idx_unique_transaction;

CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_user_reference
  ON public.transactions (user_id, reference_id)
  WHERE reference_id IS NOT NULL AND reference_id <> '';
