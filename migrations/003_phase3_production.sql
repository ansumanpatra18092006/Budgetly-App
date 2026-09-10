CREATE TABLE IF NOT EXISTS fraudshield_jobs (
    id bigserial PRIMARY KEY,
    institution_id bigint NOT NULL REFERENCES users(id),
    job_type varchar(64) NOT NULL,
    idempotency_key varchar(200) NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    status varchar(24) NOT NULL DEFAULT 'QUEUED',
    attempts integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 5,
    available_at timestamptz NOT NULL DEFAULT now(),
    locked_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(institution_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_fraud_jobs_ready ON fraudshield_jobs(status,available_at);
CREATE INDEX IF NOT EXISTS idx_fraud_tx_institution_time ON fraudshield_transactions(institution_id,transaction_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_fraud_tx_account_time ON fraudshield_transactions(institution_id,account_ref,transaction_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_fraud_investigations_status ON fraudshield_investigations(institution_id,status,opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_fraud_audit_created ON fraudshield_audit_events(institution_id,created_at DESC);
