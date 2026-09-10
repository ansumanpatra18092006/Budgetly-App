-- FraudShield Phase 2: autonomous response policy + idempotent response ledger.
CREATE TABLE IF NOT EXISTS fraudshield_response_configs (
    institution_id bigint PRIMARY KEY REFERENCES users(id),
    policy jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamp with time zone NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fraudshield_response_events (
    id bigserial PRIMARY KEY,
    institution_id bigint NOT NULL REFERENCES users(id),
    transaction_id bigint NOT NULL REFERENCES fraudshield_transactions(id),
    case_id bigint NULL REFERENCES fraudshield_investigations(id),
    risk_score integer NOT NULL,
    action varchar(64) NOT NULL,
    state varchar(32) NOT NULL DEFAULT 'ACTIVE',
    notification_status varchar(64) NOT NULL DEFAULT 'NOT_REQUIRED',
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    UNIQUE(institution_id, transaction_id)
);

CREATE INDEX IF NOT EXISTS idx_fraudshield_response_events_institution_created
    ON fraudshield_response_events(institution_id, created_at DESC);
