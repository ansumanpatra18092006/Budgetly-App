-- Existing/imported data has no independent verification: fail closed.
ALTER TABLE public.transactions ADD COLUMN IF NOT EXISTS provenance text NOT NULL DEFAULT 'SELF_REPORTED';
-- Leave legacy insertion times unknown; never invent historical evidence.
ALTER TABLE public.transactions ADD COLUMN IF NOT EXISTS recorded_at timestamptz;
ALTER TABLE public.transactions ALTER COLUMN recorded_at SET DEFAULT now();
ALTER TABLE public.transactions ADD COLUMN IF NOT EXISTS verified_at timestamptz;
ALTER TABLE public.transactions ADD COLUMN IF NOT EXISTS verification_reference text;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='transactions_provenance_check' AND conrelid='public.transactions'::regclass) THEN
        ALTER TABLE public.transactions ADD CONSTRAINT transactions_provenance_check
        CHECK (provenance IN ('SELF_REPORTED', 'VERIFIED'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='transactions_verification_check' AND conrelid='public.transactions'::regclass) THEN
        ALTER TABLE public.transactions ADD CONSTRAINT transactions_verification_check
        CHECK (provenance <> 'VERIFIED' OR (verified_at IS NOT NULL AND length(trim(verification_reference)) > 0 AND verification_reference IS NOT NULL));
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_transactions_underwriting ON public.transactions(user_id, provenance, date);

ALTER TABLE public.loan_applications ADD COLUMN IF NOT EXISTS submission_provenance jsonb;
