"""PostgreSQL-backed durable work queue for FraudShield asynchronous jobs.

Uses SKIP LOCKED so multiple workers can safely process jobs concurrently.
Jobs are idempotent by idempotency_key and support bounded retry/backoff.
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone


def ensure_job_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS fraudshield_jobs (
        id bigserial PRIMARY KEY,
        institution_id bigint NOT NULL REFERENCES users(id),
        job_type varchar(64) NOT NULL,
        idempotency_key varchar(200) NOT NULL,
        payload jsonb NOT NULL DEFAULT '{}'::jsonb,
        status varchar(24) NOT NULL DEFAULT 'QUEUED',
        attempts integer NOT NULL DEFAULT 0,
        max_attempts integer NOT NULL DEFAULT 5,
        available_at timestamp with time zone NOT NULL DEFAULT now(),
        locked_at timestamp with time zone,
        last_error text,
        created_at timestamp with time zone NOT NULL DEFAULT now(),
        updated_at timestamp with time zone NOT NULL DEFAULT now(),
        UNIQUE(institution_id, idempotency_key)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fraud_jobs_ready ON fraudshield_jobs(status, available_at)")


def enqueue_job(conn, institution_id: int, job_type: str, key: str, payload: dict):
    ensure_job_table(conn)
    return conn.execute("""INSERT INTO fraudshield_jobs
        (institution_id,job_type,idempotency_key,payload)
        VALUES (%s,%s,%s,%s::jsonb)
        ON CONFLICT (institution_id,idempotency_key) DO UPDATE SET updated_at=now()
        RETURNING *""", (institution_id, job_type, key, json.dumps(payload))).fetchone()


def claim_jobs(conn, limit=20):
    ensure_job_table(conn)
    return conn.execute("""WITH picked AS (
        SELECT id FROM fraudshield_jobs
        WHERE status IN ('QUEUED','RETRY') AND available_at <= now()
        ORDER BY available_at,id
        FOR UPDATE SKIP LOCKED LIMIT %s
    ) UPDATE fraudshield_jobs j SET status='PROCESSING', locked_at=now(), attempts=attempts+1, updated_at=now()
      FROM picked WHERE j.id=picked.id RETURNING j.*""", (limit,)).fetchall()


def mark_done(conn, job_id):
    conn.execute("UPDATE fraudshield_jobs SET status='DONE',locked_at=NULL,updated_at=now() WHERE id=%s", (job_id,))


def mark_failed(conn, job, error: str):
    attempts = int(job.get('attempts') or 1)
    max_attempts = int(job.get('max_attempts') or 5)
    if attempts >= max_attempts:
        conn.execute("UPDATE fraudshield_jobs SET status='DEAD',last_error=%s,locked_at=NULL,updated_at=now() WHERE id=%s", (error[:1000], job['id']))
    else:
        delay = min(300, 2 ** attempts)
        conn.execute("UPDATE fraudshield_jobs SET status='RETRY',last_error=%s,locked_at=NULL,available_at=now()+(%s * interval '1 second'),updated_at=now() WHERE id=%s", (error[:1000], delay, job['id']))


def recover_stale_jobs(conn, older_than_minutes=10):
    ensure_job_table(conn)
    row = conn.execute("""UPDATE fraudshield_jobs SET status='RETRY',locked_at=NULL,available_at=now(),updated_at=now(),
        last_error=COALESCE(last_error,'') || ' [recovered stale processing job]'
        WHERE status='PROCESSING' AND locked_at < now()-(%s * interval '1 minute') RETURNING id""", (older_than_minutes,)).fetchall()
    return len(row)


def queue_stats(conn, institution_id=None):
    ensure_job_table(conn)
    if institution_id is None:
        rows=conn.execute("SELECT status,COUNT(*) n FROM fraudshield_jobs GROUP BY status").fetchall()
    else:
        rows=conn.execute("SELECT status,COUNT(*) n FROM fraudshield_jobs WHERE institution_id=%s GROUP BY status", (institution_id,)).fetchall()
    return {r['status']: int(r['n']) for r in rows}
