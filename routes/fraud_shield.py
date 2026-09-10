"""FraudShield institutional transaction monitoring.

Hard data boundary: this blueprint never reads ``public.transactions``,
``loan_applications`` or borrower identities. It uses only the independent
``fraudshield_transactions`` stream owned by the signed-in institution.
"""
from __future__ import annotations

import json
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, abort, jsonify, request, session, render_template

from services.fraud_risk_service import (
    generate_ai_explanation,
    default_rule_config,
    model_metadata,
    score_transaction,
    score_transaction_fast,
    train_model,
)
from utils.db import get_db
from utils.decorators import lender_required
from services.fraud_job_service import enqueue_job, queue_stats
from services.fraud_evaluation_service import evaluate_fraud_model

logger = logging.getLogger(__name__)
fraud_shield_bp = Blueprint("fraud_shield", __name__)

STATUS_ACTIONS = {"review": "REVIEWED", "escalate": "ESCALATED", "close": "CLOSED", "reopen": "OPEN"}
_SEEDED_INSTITUTIONS = set()
_SEED_LOCK = __import__("threading").Lock()
_INTEL_CACHE = {}
_INTEL_CACHE_LOCK = __import__("threading").Lock()
_INTEL_CACHE_TTL = 60
_ADVANCED_ENRICHED = set()



def _ensure_rules_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS fraudshield_rule_configs (
        institution_id bigint PRIMARY KEY REFERENCES users(id),
        rules jsonb NOT NULL DEFAULT '{}'::jsonb,
        updated_at timestamp with time zone NOT NULL DEFAULT now()
    )""")


def _get_rule_config(conn, institution_id):
    _ensure_rules_table(conn)
    row = conn.execute("SELECT rules FROM fraudshield_rule_configs WHERE institution_id=%s", (institution_id,)).fetchone()
    rules = dict(row["rules"] or {}) if row else {}
    merged = default_rule_config()
    for key, value in rules.items():
        if key in merged and isinstance(value, dict):
            merged[key].update({k: value[k] for k in ("enabled", "threshold", "points") if k in value})
    return merged


def _score_with_rules(conn, institution_id, txn, *, fast=False):
    enriched = dict(txn)
    enriched["_rule_config"] = _get_rule_config(conn, institution_id)
    return score_transaction_fast(enriched) if fast else score_transaction(enriched)



def _ensure_intelligence_schema(conn):
    """Bring an older FraudShield database up to the Phase-2 intelligence shape.

    This intentionally uses idempotent PostgreSQL DDL so deployments that were
    created before Phase 2 do not crash the bootstrap endpoint.
    """
    # Columns read by _fetch_transactions / investigation views.
    for ddl in (
        "ALTER TABLE fraudshield_transactions ADD COLUMN IF NOT EXISTS transaction_ref text",
        "ALTER TABLE fraudshield_transactions ADD COLUMN IF NOT EXISTS utr text",
        "ALTER TABLE fraudshield_transactions ADD COLUMN IF NOT EXISTS ip_address text",
        "ALTER TABLE fraudshield_transactions ADD COLUMN IF NOT EXISTS browser text",
        "ALTER TABLE fraudshield_transactions ADD COLUMN IF NOT EXISTS os text",
        "ALTER TABLE fraudshield_transactions ADD COLUMN IF NOT EXISTS beneficiary_ref text",
        "ALTER TABLE fraudshield_transactions ADD COLUMN IF NOT EXISTS failed_auth_count integer NOT NULL DEFAULT 0",
        "ALTER TABLE fraudshield_transactions ADD COLUMN IF NOT EXISTS previous_location text",
        "ALTER TABLE fraudshield_transactions ADD COLUMN IF NOT EXISTS previous_transaction_timestamp timestamp with time zone",
        "ALTER TABLE fraudshield_investigations ADD COLUMN IF NOT EXISTS assigned_analyst text",
        "ALTER TABLE fraudshield_investigations ADD COLUMN IF NOT EXISTS analyst_notes text",
        "ALTER TABLE fraudshield_investigations ADD COLUMN IF NOT EXISTS outcome text",
    ):
        conn.execute(ddl)

    conn.execute("""CREATE TABLE IF NOT EXISTS fraudshield_audit_events (
        id bigserial PRIMARY KEY,
        institution_id bigint NOT NULL REFERENCES users(id),
        event_type text NOT NULL, entity_type text, entity_id text,
        payload jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at timestamp with time zone NOT NULL DEFAULT now()
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS fraudshield_replay_registry (
        id bigserial PRIMARY KEY,
        institution_id bigint NOT NULL REFERENCES users(id),
        fingerprint text NOT NULL, transaction_ref text,
        first_seen timestamp with time zone NOT NULL DEFAULT now(),
        last_seen timestamp with time zone NOT NULL DEFAULT now(),
        duplicate_count integer NOT NULL DEFAULT 0,
        UNIQUE(institution_id, fingerprint)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS fraudshield_network_edges (
        id bigserial PRIMARY KEY,
        institution_id bigint NOT NULL REFERENCES users(id),
        source_ref text NOT NULL, target_ref text NOT NULL,
        relationship_type text NOT NULL, weight double precision NOT NULL DEFAULT 0.5,
        last_seen timestamp with time zone NOT NULL DEFAULT now(),
        UNIQUE(institution_id, source_ref, target_ref, relationship_type)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS fraudshield_model_runs (
        id bigserial PRIMARY KEY,
        institution_id bigint NOT NULL REFERENCES users(id),
        model_version text NOT NULL, dataset text, training_records integer,
        metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
        status text NOT NULL DEFAULT 'READY',
        created_at timestamp with time zone NOT NULL DEFAULT now()
    )""")
    _ensure_rules_table(conn)
    _ensure_response_tables(conn)
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_fraudshield_transaction_ref
        ON fraudshield_transactions(institution_id, transaction_ref)
        WHERE transaction_ref IS NOT NULL""")
    conn.commit()

def _case_number(case_id):
    return f"FS-{case_id:04d}"


def _default_response_policy():
    return {
        "monitor_from": 40,
        "step_up_from": 70,
        "hold_from": 85,
        "critical_from": 95,
        "auto_create_case": True,
        "auto_notify_soc": True,
        "analyst_sla_minutes": 5,
    }


def _ensure_response_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS fraudshield_response_configs (
        institution_id bigint PRIMARY KEY REFERENCES users(id),
        policy jsonb NOT NULL DEFAULT '{}'::jsonb,
        updated_at timestamp with time zone NOT NULL DEFAULT now()
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS fraudshield_response_events (
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
    )""")


def _get_response_policy(conn, institution_id):
    _ensure_response_tables(conn)
    row=conn.execute("SELECT policy FROM fraudshield_response_configs WHERE institution_id=%s",(institution_id,)).fetchone()
    policy=_default_response_policy()
    if row and row["policy"]:
        policy.update(dict(row["policy"]))
    return policy


def _response_action(score, policy):
    score=int(score or 0)
    if score >= int(policy.get("critical_from",95)):
        return "CRITICAL_HOLD_NOTIFY"
    if score >= int(policy.get("hold_from",85)):
        return "HOLD_INVESTIGATE"
    if score >= int(policy.get("step_up_from",70)):
        return "STEP_UP_VERIFICATION"
    if score >= int(policy.get("monitor_from",40)):
        return "MONITOR"
    return "ALLOW"


def _build_evidence_bundle(txn, risk, action, case_id=None):
    return {
        "report_type":"FraudShield Incident Evidence Bundle",
        "generated_at":datetime.now(timezone.utc).isoformat(),
        "transaction":{
            "transaction_id":txn.get("id"),
            "transaction_ref":txn.get("transaction_ref"),
            "utr":txn.get("utr"),
            "account_ref":txn.get("account_ref"),
            "merchant":txn.get("merchant"),
            "amount":float(txn.get("amount") or 0),
            "timestamp":txn.get("transaction_timestamp").isoformat() if txn.get("transaction_timestamp") else None,
            "location":txn.get("location"),
            "channel":txn.get("channel"),
            "device_id":txn.get("device_id"),
            "beneficiary_ref":txn.get("beneficiary_ref"),
        },
        "risk":{
            "score":risk.get("score"),
            "level":risk.get("level_label") or risk.get("level"),
            "model_probability":risk.get("model_probability"),
            "anomaly_score":risk.get("anomaly_score"),
            "rule_score":risk.get("rule_score"),
            "account_takeover_risk":risk.get("account_takeover_risk"),
            "reason":risk.get("reason"),
            "signals":risk.get("rule_contributors") or risk.get("contributors") or [],
        },
        "response":{
            "action":action,
            "case_number":_case_number(case_id) if case_id else None,
            "government_reporting":"HUMAN_APPROVAL_REQUIRED",
            "official_portal":"https://cybercrime.gov.in/",
            "financial_fraud_helpline":"1930",
        },
        "disclaimer":"Risk assessment is decision support, not proof of fraud. External reporting requires analyst verification.",
    }


def _notify_soc(evidence):
    """Optional internal SOC webhook. No government report is auto-submitted."""
    url=os.environ.get("FRAUDSHIELD_SOC_WEBHOOK_URL")
    if not url:
        return "QUEUED_DEMO"
    if os.environ.get("FRAUDSHIELD_ALLOW_OUTBOUND_WEBHOOKS","0") != "1":
        return "WEBHOOK_DISABLED"
    try:
        import urllib.request
        payload=json.dumps({"event":"fraudshield.critical_alert","evidence":evidence}).encode("utf-8")
        req=urllib.request.Request(url,data=payload,headers={"Content-Type":"application/json"},method="POST")
        with urllib.request.urlopen(req,timeout=2) as resp:
            return "SENT" if 200 <= int(resp.status) < 300 else f"HTTP_{resp.status}"
    except Exception:
        logger.exception("FraudShield SOC notification failed")
        return "FAILED"


def _run_autonomous_response(conn, institution_id, txn, risk=None):
    """Idempotently orchestrate an internal response for one transaction."""
    _ensure_response_tables(conn)
    existing=conn.execute("SELECT * FROM fraudshield_response_events WHERE institution_id=%s AND transaction_id=%s",(institution_id,txn["id"])).fetchone()
    if existing:
        return dict(existing)
    policy=_get_response_policy(conn,institution_id)
    risk=risk or _score_with_rules(conn,institution_id,txn,fast=True)
    action=_response_action(risk.get("score"),policy)
    case_id=None
    if action in {"HOLD_INVESTIGATE","CRITICAL_HOLD_NOTIFY"} and bool(policy.get("auto_create_case",True)):
        case=conn.execute("SELECT id FROM fraudshield_investigations WHERE institution_id=%s AND transaction_id=%s AND status!='CLOSED' ORDER BY opened_at DESC LIMIT 1",(institution_id,txn["id"])).fetchone()
        if case:
            case_id=case["id"]
        else:
            opened=conn.execute("INSERT INTO fraudshield_investigations (transaction_id,institution_id,risk_score,risk_level,history) VALUES (%s,%s,%s,%s,%s) RETURNING id",(txn["id"],institution_id,risk["score"],risk["level"],json.dumps([{"action":"AUTO_OPEN","note":f"Fraud Autopilot opened case after {risk['score']}/100 risk score"}]))).fetchone()
            case_id=opened["id"]
    evidence=_build_evidence_bundle(txn,risk,action,case_id)
    notification="NOT_REQUIRED"
    if action=="CRITICAL_HOLD_NOTIFY" and bool(policy.get("auto_notify_soc",True)):
        enqueue_job(conn, institution_id, "SOC_NOTIFY", f"soc:{txn['id']}", evidence)
        notification="QUEUED_ASYNC"
    row=conn.execute("""INSERT INTO fraudshield_response_events
        (institution_id,transaction_id,case_id,risk_score,action,state,notification_status,evidence)
        VALUES (%s,%s,%s,%s,%s,'ACTIVE',%s,%s::jsonb) RETURNING *""",
        (institution_id,txn["id"],case_id,int(risk["score"]),action,notification,json.dumps(evidence))).fetchone()
    _audit(conn,institution_id,"AUTONOMOUS_RESPONSE","transaction",txn["id"],{"risk_score":risk["score"],"action":action,"case_id":case_id,"notification_status":notification})
    return dict(row)


def _response_public(row):
    if not row:
        return None
    return {
        "action":row.get("action"),"state":row.get("state"),"risk_score":row.get("risk_score"),
        "notification_status":row.get("notification_status"),
        "case_number":_case_number(row["case_id"]) if row.get("case_id") else None,
        "created_at":row["created_at"].isoformat() if row.get("created_at") else None,
        "evidence_ready":bool(row.get("evidence")),
    }


def _seed_demo_stream(conn, institution_id):
    """Create/update only the dedicated synthetic FraudShield demo stream."""
    demo_rows = [
        # account_ref, merchant, amount, days_ago, hour, location, device, age, c1, c24, dist, new_device, merchant_risk, rels, seen, trust, time_dev, amount_ratio, network, channel
        ("AC-10421", "Amazon", 1200, 1, 14, "Bhubaneswar", "DEV-1001", 640, 1, 4, 2, False, 0.08, 1, True, 0.94, 1.0, 0.85, 0.05, "UPI"),
        ("AC-10422", "Swiggy", 520, 1, 19, "Bhubaneswar", "DEV-1002", 510, 1, 3, 3, False, 0.10, 1, True, 0.95, 0.5, 0.72, 0.04, "UPI"),
        ("AC-10423", "Spotify", 119, 2, 21, "Cuttack", "DEV-1003", 810, 1, 2, 1, False, 0.04, 1, True, 0.97, 0.4, 0.68, 0.03, "CARD"),
        ("AC-10424", "Netflix", 649, 3, 20, "Bhubaneswar", "DEV-1004", 420, 1, 4, 2, False, 0.06, 1, True, 0.95, 0.7, 0.91, 0.03, "CARD"),
        ("AC-10425", "Myntra", 13500, 0, 21, "Bhubaneswar", "DEV-1005", 280, 2, 6, 0, False, 0.63, 2, False, 0.84, 1.2, 4.2, 0.35, "UPI"),
        ("AC-10426", "Unknown Merchant", 48500, 1, 2, "New Delhi", "DEV-9999", 41, 7, 19, 1120, True, 0.91, 8, False, 0.05, 7.0, 18.0, 0.95, "UPI"),
        ("AC-10427", "Flipkart", 2100, 1, 18, "Bhubaneswar", "DEV-1007", 920, 1, 5, 4, False, 0.12, 1, True, 0.96, 1.2, 1.1, 0.06, "CARD"),
        ("AC-10428", "UPI Wallet", 7800, 0, 1, "Kolkata", "DEV-8801", 88, 4, 12, 420, True, 0.71, 5, False, 0.18, 6.0, 5.8, 0.72, "UPI"),
        ("AC-10429", "IRCTC", 2450, 4, 12, "Bhubaneswar", "DEV-1009", 740, 1, 2, 8, False, 0.05, 1, True, 0.95, 0.8, 1.0, 0.05, "UPI"),
        ("AC-10430", "BookMyShow", 1800, 5, 22, "Puri", "DEV-1010", 365, 1, 4, 65, False, 0.17, 1, True, 0.91, 1.6, 1.3, 0.08, "CARD"),
        ("AC-10431", "Gift Card Shop", 19600, 0, 0, "Hyderabad", "DEV-4004", 24, 6, 16, 780, True, 0.87, 7, False, 0.11, 6.5, 9.1, 0.88, "UPI"),
        ("AC-10432", "Amazon", 3200, 0, 15, "Bhubaneswar", "DEV-1032", 1100, 1, 3, 3, False, 0.08, 1, True, 0.97, 0.4, 1.2, 0.04, "CARD"),
        # Same high amount as AC-10426, but a mature/normal account profile.
        # Used for the false-positive comparison demo.
        ("AC-10433", "Amazon Business", 48500, 0, 14, "Bhubaneswar", "DEV-1033", 1460, 1, 3, 4, False, 0.07, 1, True, 0.98, 0.3, 1.05, 0.03, "UPI"),
    ]

    sql = """
        INSERT INTO fraudshield_transactions
        (institution_id, account_ref, merchant, amount, transaction_timestamp,
         location, device_id, account_age_days, txn_count_1h, txn_count_24h,
         location_distance_km, is_new_device, merchant_risk, transaction_hour,
         relationship_count, merchant_seen_before, device_trust_score,
         time_deviation_hours, amount_to_account_median, network_risk, channel)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """
    update_sql = """
        UPDATE fraudshield_transactions SET
            merchant=%s, amount=%s, transaction_timestamp=%s, location=%s,
            device_id=%s, account_age_days=%s, txn_count_1h=%s, txn_count_24h=%s,
            location_distance_km=%s, is_new_device=%s, merchant_risk=%s,
            transaction_hour=%s, relationship_count=%s, merchant_seen_before=%s,
            device_trust_score=%s, time_deviation_hours=%s,
            amount_to_account_median=%s, network_risk=%s, channel=%s
        WHERE institution_id=%s AND account_ref=%s
    """

    now = datetime.now(timezone.utc)
    values=[]
    for row in demo_rows:
        (account_ref, merchant, amount, days_ago, hour, location, device_id, age, c1, c24,
         dist, new_device, merchant_risk, rels, seen, trust, time_dev, amount_ratio,
         network, channel) = row
        ts = now.replace(hour=hour, minute=18, second=0, microsecond=0) - timedelta(days=days_ago)
        values.append((institution_id, account_ref, merchant, amount, ts, location, device_id, age,
                       c1, c24, dist, new_device, merchant_risk, hour, rels, seen, trust,
                       time_dev, amount_ratio, network, channel))

    # Two set-based statements replace the old per-row SELECT + INSERT/UPDATE
    # loop. This is much faster against a remote PostgreSQL/Supabase database.
    if values:
        cols = (
            'institution_id,account_ref,merchant,amount,transaction_timestamp,location,device_id,'
            'account_age_days,txn_count_1h,txn_count_24h,location_distance_km,is_new_device,merchant_risk,'
            'transaction_hour,relationship_count,merchant_seen_before,device_trust_score,time_deviation_hours,'
            'amount_to_account_median,network_risk,channel'
        )
        vp = ",".join(["(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"]*len(values))
        flat=[v for rowv in values for v in rowv]
        conn.execute(
            """WITH v(""" + cols + """) AS (VALUES """ + vp + """)
               UPDATE fraudshield_transactions AS t
               SET merchant=v.merchant, amount=v.amount, transaction_timestamp=v.transaction_timestamp,
                   location=v.location, device_id=v.device_id, account_age_days=v.account_age_days,
                   txn_count_1h=v.txn_count_1h, txn_count_24h=v.txn_count_24h,
                   location_distance_km=v.location_distance_km, is_new_device=v.is_new_device,
                   merchant_risk=v.merchant_risk, transaction_hour=v.transaction_hour,
                   relationship_count=v.relationship_count, merchant_seen_before=v.merchant_seen_before,
                   device_trust_score=v.device_trust_score, time_deviation_hours=v.time_deviation_hours,
                   amount_to_account_median=v.amount_to_account_median, network_risk=v.network_risk,
                   channel=v.channel
               FROM v
               WHERE t.institution_id=v.institution_id AND t.account_ref=v.account_ref""", flat
        )
        conn.execute(
            """WITH v(""" + cols + """) AS (VALUES """ + vp + """)
               INSERT INTO fraudshield_transactions(""" + cols + """)
               SELECT v.* FROM v
               WHERE NOT EXISTS (
                   SELECT 1 FROM fraudshield_transactions t
                   WHERE t.institution_id=v.institution_id AND t.account_ref=v.account_ref
               )""", flat
        )

    conn.commit()


def _ensure_demo_stream(conn, institution_id):
    """Seed the static FraudShield demo stream once per process/institution."""
    key=int(institution_id)
    if key in _SEEDED_INSTITUTIONS: return
    with _SEED_LOCK:
        if key in _SEEDED_INSTITUTIONS: return
        _seed_demo_stream(conn,institution_id)
        _SEEDED_INSTITUTIONS.add(key)


def _fetch_transactions(conn, institution_id, limit=200):
    rows = conn.execute(
        """
        SELECT id, account_ref, merchant, amount, transaction_timestamp, location, device_id,
               account_age_days, txn_count_1h, txn_count_24h, location_distance_km,
               is_new_device, merchant_risk, transaction_hour, relationship_count,
               merchant_seen_before, device_trust_score, time_deviation_hours,
               amount_to_account_median, network_risk, channel, transaction_ref, utr,
               ip_address, browser, os, beneficiary_ref, failed_auth_count, previous_location,
               previous_transaction_timestamp
        FROM fraudshield_transactions
        WHERE institution_id=%s
        ORDER BY transaction_timestamp DESC
        LIMIT %s
        """,
        (institution_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _get_case_map(conn, institution_id, ids):
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT id, transaction_id, status FROM fraudshield_investigations WHERE institution_id=%s AND transaction_id=ANY(%s) AND status!='CLOSED'",
        (institution_id, ids),
    ).fetchall()
    return {r["transaction_id"]: r for r in rows}


@fraud_shield_bp.route("/lender/fraudshield/model", methods=["GET"])
@lender_required
def get_model():
    return jsonify({"status": "success", "model": model_metadata()})


@fraud_shield_bp.route("/lender/fraudshield/model/retrain", methods=["POST"])
@lender_required
def retrain_model():
    institution_id=session["user_id"]
    meta=train_model(force=True)
    conn=get_db()
    try:
        conn.execute("INSERT INTO fraudshield_model_runs(institution_id,model_version,dataset,training_records,metrics,status) VALUES (%s,%s,%s,%s,%s,%s)",(institution_id,meta.get("model_version","FraudShield"),meta.get("dataset"),meta.get("training_records",0),json.dumps(meta.get("metrics") or {}),"READY"))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"status": "success", "model": meta})


@fraud_shield_bp.route("/lender/fraudshield/rules", methods=["GET", "PUT"])
@lender_required
def fraud_rules_config():
    institution_id = session["user_id"]
    conn = get_db()
    try:
        if request.method == "GET":
            return jsonify({"status": "success", "rules": _get_rule_config(conn, institution_id)})
        incoming = (request.get_json(silent=True) or {}).get("rules") or {}
        defaults = default_rule_config()
        cleaned = {}
        for key, rule in incoming.items():
            if key not in defaults or not isinstance(rule, dict):
                continue
            cleaned[key] = {
                "enabled": bool(rule.get("enabled", True)),
                "threshold": max(0, min(float(rule.get("threshold", defaults[key]["threshold"])), 10000000)),
                "points": max(0, min(int(rule.get("points", defaults[key]["points"])), 30)),
            }
        _ensure_rules_table(conn)
        conn.execute("""INSERT INTO fraudshield_rule_configs(institution_id,rules,updated_at)
                     VALUES (%s,%s::jsonb,now())
                     ON CONFLICT (institution_id) DO UPDATE SET rules=EXCLUDED.rules, updated_at=now()""",
                     (institution_id, json.dumps(cleaned)))
        _audit(conn,institution_id,"RULE_CONFIG_UPDATED","rule_config",str(institution_id),{"rule_count":len(cleaned)})
        conn.commit()
        _invalidate_intelligence_snapshot(institution_id)
        return jsonify({"status":"success","rules":_get_rule_config(conn,institution_id),"message":"Fraud detection rules updated"})
    finally:
        conn.close()


@fraud_shield_bp.route("/lender/fraudshield/response-policy", methods=["GET","PUT"])
@lender_required
def fraud_response_policy():
    institution_id=session["user_id"]
    conn=get_db()
    try:
        if request.method=="GET":
            return jsonify({"status":"success","policy":_get_response_policy(conn,institution_id),"official_reporting":{"portal":"https://cybercrime.gov.in/","helpline":"1930","mode":"human_approval_required"}})
        body=request.get_json(silent=True) or {}
        incoming=body.get("policy") or {}
        policy=_default_response_policy()
        for k in ("monitor_from","step_up_from","hold_from","critical_from","analyst_sla_minutes"):
            if k in incoming:
                policy[k]=max(0,min(int(incoming[k]),100 if k!="analyst_sla_minutes" else 1440))
        for k in ("auto_create_case","auto_notify_soc"):
            if k in incoming: policy[k]=bool(incoming[k])
        if not (0 <= policy["monitor_from"] <= policy["step_up_from"] <= policy["hold_from"] <= policy["critical_from"] <= 100):
            return jsonify({"status":"error","errors":["Thresholds must increase from monitor → step-up → hold → critical."]}),400
        _ensure_response_tables(conn)
        conn.execute("""INSERT INTO fraudshield_response_configs(institution_id,policy,updated_at) VALUES (%s,%s::jsonb,now())
                      ON CONFLICT (institution_id) DO UPDATE SET policy=EXCLUDED.policy,updated_at=now()""",(institution_id,json.dumps(policy)))
        _audit(conn,institution_id,"RESPONSE_POLICY_UPDATED","response_policy",institution_id,policy)
        conn.commit(); _invalidate_intelligence_snapshot(institution_id)
        return jsonify({"status":"success","policy":policy,"message":"Fraud Autopilot policy updated"})
    finally:
        conn.close()


@fraud_shield_bp.route("/lender/fraudshield/transactions/<int:transaction_id>/autopilot", methods=["POST"])
@lender_required
def fraud_autopilot_evaluate(transaction_id):
    institution_id=session["user_id"]; conn=get_db()
    try:
        row=conn.execute("SELECT * FROM fraudshield_transactions WHERE id=%s AND institution_id=%s",(transaction_id,institution_id)).fetchone()
        if not row: abort(404)
        tx=dict(row); response=_run_autonomous_response(conn,institution_id,tx)
        conn.commit(); _invalidate_intelligence_snapshot(institution_id)
        return jsonify({"status":"success","response":_response_public(response)})
    except Exception:
        conn.rollback(); raise
    finally: conn.close()


@fraud_shield_bp.route("/lender/fraudshield/transactions/<int:transaction_id>/evidence", methods=["GET"])
@lender_required
def fraud_evidence_report(transaction_id):
    """Render a human-readable incident report; raw JSON remains opt-in via ?raw=1."""
    import hashlib

    institution_id=session["user_id"]; conn=get_db()
    try:
        row=conn.execute("SELECT * FROM fraudshield_transactions WHERE id=%s AND institution_id=%s",(transaction_id,institution_id)).fetchone()
        if not row: abort(404)
        response=_run_autonomous_response(conn,institution_id,dict(row)); conn.commit()
        evidence=response.get("evidence") or {}

        # Keep machine-readable evidence available for integrations/debugging,
        # but make the analyst-facing endpoint a proper incident report.
        if request.args.get("raw") in {"1","true","yes"}:
            payload=json.dumps(evidence,indent=2,default=str)
            headers={"Content-Disposition":f'attachment; filename="FraudShield_Evidence_{transaction_id}.json"'}
            return Response(payload,status=200,mimetype="application/json",headers=headers)

        canonical=json.dumps(evidence,sort_keys=True,separators=(",",":"),default=str).encode("utf-8")
        evidence_hash=hashlib.sha256(canonical).hexdigest()
        txn=evidence.get("transaction") or {}
        risk=evidence.get("risk") or {}
        response_info=evidence.get("response") or {}
        signals=risk.get("signals") or []
        if not isinstance(signals,list):
            signals=[signals]

        # The report intentionally distinguishes machine assessment from confirmed fraud.
        score=int(risk.get("score") or response.get("risk_score") or 0)
        if score >= 95:
            severity="Critical"
        elif score >= 85:
            severity="High"
        elif score >= 70:
            severity="Elevated"
        elif score >= 40:
            severity="Monitor"
        else:
            severity="Low"

        action=str(response_info.get("action") or response.get("action") or "ALLOW")
        action_display=action.replace("_"," ").title()
        case_number=response_info.get("case_number") or (_case_number(response.get("case_id")) if response.get("case_id") else None)
        notification=response.get("notification_status") or "NOT_REQUIRED"
        generated_at=evidence.get("generated_at") or datetime.now(timezone.utc).isoformat()

        next_steps=[]
        if action in {"STEP_UP_VERIFICATION"}:
            next_steps.append("Complete step-up verification before allowing the activity to proceed.")
        if action in {"HOLD_INVESTIGATE","CRITICAL_HOLD_NOTIFY"}:
            next_steps.extend([
                "Review the triggered risk signals and compare them with the account's normal behaviour.",
                "Validate device, location, merchant and beneficiary context before disposition.",
                "Record the analyst decision and supporting rationale in the investigation case.",
            ])
        if action == "CRITICAL_HOLD_NOTIFY":
            next_steps.append("Confirm the internal SOC notification and assess whether external escalation is warranted.")
        if not next_steps:
            next_steps.append("Continue monitoring and retain the event for historical analysis.")

        return render_template(
            "fraudshield_incident_report.html",
            evidence=evidence,
            transaction=txn,
            risk=risk,
            response_info=response_info,
            signals=signals,
            score=score,
            severity=severity,
            action_display=action_display,
            case_number=case_number,
            notification=notification,
            generated_at=generated_at,
            evidence_hash=evidence_hash,
            next_steps=next_steps,
            transaction_id=transaction_id,
        )
    except Exception:
        conn.rollback(); raise
    finally: conn.close()


@fraud_shield_bp.route("/lender/fraudshield/autopilot/scan", methods=["POST"])
@lender_required
def fraud_autopilot_scan():
    institution_id=session["user_id"]; conn=get_db()
    try:
        _ensure_demo_stream(conn,institution_id)
        policy=_get_response_policy(conn,institution_id)
        rows=conn.execute("SELECT * FROM fraudshield_transactions WHERE institution_id=%s ORDER BY transaction_timestamp DESC LIMIT 200",(institution_id,)).fetchall()
        created=[]
        for row in rows:
            tx=dict(row); risk=_score_with_rules(conn,institution_id,tx,fast=True)
            if int(risk.get("score") or 0) < int(policy.get("step_up_from",70)):
                continue
            response=_run_autonomous_response(conn,institution_id,tx,risk)
            created.append(_response_public(response))
        conn.commit(); _invalidate_intelligence_snapshot(institution_id)
        return jsonify({"status":"success","evaluated":len(rows),"actioned":len(created),"events":created})
    except Exception:
        conn.rollback(); raise
    finally: conn.close()


@fraud_shield_bp.route("/lender/fraudshield/autopilot/events", methods=["GET"])
@lender_required
def fraud_autopilot_events():
    institution_id=session["user_id"]; conn=get_db()
    try:
        _ensure_response_tables(conn)
        rows=conn.execute("""SELECT re.*,t.account_ref,t.merchant,t.amount,t.transaction_timestamp
            FROM fraudshield_response_events re JOIN fraudshield_transactions t ON t.id=re.transaction_id
            WHERE re.institution_id=%s ORDER BY re.created_at DESC LIMIT 100""",(institution_id,)).fetchall()
        events=[]
        for r in rows:
            x=dict(r); out=_response_public(x); out.update({"transaction_id":x["transaction_id"],"account_ref":x["account_ref"],"merchant":x["merchant"],"amount":float(x["amount"] or 0),"timestamp":x["transaction_timestamp"].isoformat() if x.get("transaction_timestamp") else None}); events.append(out)
        return jsonify({"status":"success","events":events,"policy":_get_response_policy(conn,institution_id)})
    finally: conn.close()


@fraud_shield_bp.route("/lender/fraudshield/transactions", methods=["GET"])
@lender_required
def list_monitored_transactions():
    institution_id=session["user_id"]
    conn=get_db()
    try:
        snap=_get_intelligence_snapshot(conn,institution_id)
        return jsonify({"status":"success","summary":snap["summary"],"transactions":snap["transactions"],"model":snap["model"]})
    finally:
        conn.close()


@fraud_shield_bp.route("/lender/fraudshield/transactions/<int:transaction_id>", methods=["GET"])
@lender_required
def get_transaction_risk_detail(transaction_id):
    institution_id = session["user_id"]
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM fraudshield_transactions WHERE id=%s AND institution_id=%s",
            (transaction_id, institution_id),
        ).fetchone()
        if not row:
            abort(404)
        txn = dict(row)
        # Add deterministic travel/session intelligence using only FraudShield fields.
        result = _score_with_rules(conn, institution_id, txn)
        from services.fraud_intelligence_service import impossible_travel
        result["impossible_travel"] = impossible_travel(txn)
        case = conn.execute(
            "SELECT id, status, opened_at, updated_at, history FROM fraudshield_investigations WHERE institution_id=%s AND transaction_id=%s ORDER BY opened_at DESC LIMIT 1",
            (institution_id, transaction_id),
        ).fetchone()
        ai = generate_ai_explanation(result)
        _ensure_response_tables(conn)
        response_row=conn.execute("SELECT * FROM fraudshield_response_events WHERE institution_id=%s AND transaction_id=%s",(institution_id,transaction_id)).fetchone()
        return jsonify({
            "status": "success",
            "transaction": {
                "transaction_id": txn["id"],
                "account_ref": txn["account_ref"],
                "merchant": txn["merchant"],
                "amount": float(txn["amount"] or 0),
                "timestamp": txn["transaction_timestamp"].isoformat(),
                "date": txn["transaction_timestamp"].date().isoformat(),
                "location": txn.get("location"),
                "device_id": txn.get("device_id"),
                "channel": txn.get("channel"),
                "features": {
                    "account_age_days": txn.get("account_age_days"),
                    "txn_count_1h": txn.get("txn_count_1h"),
                    "txn_count_24h": txn.get("txn_count_24h"),
                    "location_distance_km": txn.get("location_distance_km"),
                    "is_new_device": bool(txn.get("is_new_device")),
                    "merchant_risk": txn.get("merchant_risk"),
                    "transaction_hour": txn.get("transaction_hour"),
                    "relationship_count": txn.get("relationship_count"),
                    "merchant_seen_before": bool(txn.get("merchant_seen_before")),
                    "device_trust_score": txn.get("device_trust_score"),
                    "time_deviation_hours": txn.get("time_deviation_hours"),
                    "amount_to_account_median": txn.get("amount_to_account_median"),
                    "network_risk": txn.get("network_risk"),
                    "failed_auth_count": txn.get("failed_auth_count"),
                    "transaction_ref": txn.get("transaction_ref"),
                    "utr": txn.get("utr"),
                    "ip_address": txn.get("ip_address"),
                    "browser": txn.get("browser"),
                    "os": txn.get("os"),
                    "beneficiary_ref": txn.get("beneficiary_ref"),
                    "failed_auth_count": txn.get("failed_auth_count"),
                    "ip_address": txn.get("ip_address"),
                    "browser": txn.get("browser"),
                    "os": txn.get("os"),
                    "transaction_ref": txn.get("transaction_ref"),
                    "utr": txn.get("utr"),
                    "previous_location": txn.get("previous_location"),
                },
            },
            "risk": result,
            "ai_explanation": ai.get("summary") if ai.get("status") == "success" else None,
            "autopilot": _response_public(dict(response_row)) if response_row else None,
            "case": ({
                "case_number": _case_number(case["id"]),
                "status": case["status"],
                "opened_at": case["opened_at"].isoformat() if case["opened_at"] else None,
                "updated_at": case["updated_at"].isoformat() if case["updated_at"] else None,
                "history": case["history"],
            } if case else None),
        })
    finally:
        conn.close()



@fraud_shield_bp.route("/lender/fraudshield/transactions/<int:transaction_id>/counterfactual", methods=["GET"])
@lender_required
def fraud_counterfactual(transaction_id):
    """Return interpretable what-if scenarios without mutating persisted data."""
    institution_id=session["user_id"]
    conn=get_db()
    try:
        row=conn.execute("SELECT * FROM fraudshield_transactions WHERE id=%s AND institution_id=%s",(transaction_id,institution_id)).fetchone()
        if not row: abort(404)
        base=dict(row)
        base_r=_score_with_rules(conn,institution_id,base)
        scenarios=[
            ("Trusted device", {"is_new_device":False,"device_trust_score":0.96}),
            ("Normal location", {"location_distance_km":25}),
            ("Known merchant", {"merchant_seen_before":True,"merchant_risk":0.10}),
            ("Normal velocity", {"txn_count_1h":1,"txn_count_24h":4}),
            ("Combined normal context", {"is_new_device":False,"device_trust_score":0.96,"location_distance_km":25,"merchant_seen_before":True,"merchant_risk":0.10,"txn_count_1h":1,"txn_count_24h":4,"network_risk":0.05,"relationship_count":1,"failed_auth_count":0}),
        ]
        results=[]
        for label, changes in scenarios:
            candidate=base.copy(); candidate.update(changes)
            r=_score_with_rules(conn,institution_id,candidate)
            results.append({"scenario":label,"risk_score":r["score"],"risk_level":r["level_label"],"delta":r["score"]-base_r["score"]})
        results.sort(key=lambda x:x["risk_score"])
        return jsonify({"status":"success","base":{"risk_score":base_r["score"],"risk_level":base_r["level_label"]},"scenarios":results,"note":"Counterfactuals are analytical simulations; no transaction data is modified."})
    finally:
        conn.close()

@fraud_shield_bp.route("/lender/fraudshield/transactions/<int:transaction_id>/similar", methods=["GET"])
@lender_required
def similar_transactions(transaction_id):
    """Return a few nearby-amount transactions for false-positive comparison.

    This endpoint only reads the institution-owned FraudShield stream and never
    joins borrower-side FinTrust tables.
    """
    institution_id = session["user_id"]
    conn = get_db()
    try:
        base = conn.execute(
            "SELECT id, amount FROM fraudshield_transactions WHERE id=%s AND institution_id=%s",
            (transaction_id, institution_id),
        ).fetchone()
        if not base:
            abort(404)
        amount = float(base["amount"] or 0)
        rows = conn.execute(
            """
            SELECT id, account_ref, merchant, amount, transaction_timestamp, location,
                   is_new_device, txn_count_1h, txn_count_24h, location_distance_km,
                   merchant_risk, transaction_hour, merchant_seen_before, device_trust_score
            FROM fraudshield_transactions
            WHERE institution_id=%s AND id<>%s
            ORDER BY ABS(amount-%s) ASC, transaction_timestamp DESC
            LIMIT 5
            """,
            (institution_id, transaction_id, amount),
        ).fetchall()
        results = []
        for r in rows:
            tx = dict(r)
            risk = _score_with_rules(conn,institution_id,tx)
            results.append({
                "transaction_id": tx["id"],
                "account_ref": tx["account_ref"],
                "merchant": tx["merchant"],
                "amount": float(tx["amount"] or 0),
                "timestamp": tx["transaction_timestamp"].isoformat() if tx.get("transaction_timestamp") else None,
                "location": tx.get("location"),
                "risk_score": risk["score"],
                "risk_level": risk["level"],
                "risk_level_label": risk["level_label"],
                "signals": {
                    "new_device": bool(tx.get("is_new_device")),
                    "velocity_1h": int(tx.get("txn_count_1h") or 0),
                    "location_distance_km": float(tx.get("location_distance_km") or 0),
                    "merchant_risk": float(tx.get("merchant_risk") or 0),
                    "merchant_seen_before": bool(tx.get("merchant_seen_before")),
                    "device_trust_score": float(tx.get("device_trust_score") or 0),
                },
            })
        return jsonify({"status":"success", "base_amount":amount, "transactions":results})
    finally:
        conn.close()


@fraud_shield_bp.route("/lender/fraudshield/transactions/<int:transaction_id>/investigate", methods=["POST"])
@lender_required
def open_investigation(transaction_id):
    institution_id = session["user_id"]
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM fraudshield_transactions WHERE id=%s AND institution_id=%s", (transaction_id, institution_id)).fetchone()
        if not row:
            abort(404)
        existing = conn.execute(
            "SELECT id, status FROM fraudshield_investigations WHERE institution_id=%s AND transaction_id=%s AND status!='CLOSED'",
            (institution_id, transaction_id),
        ).fetchone()
        if existing:
            return jsonify({"status": "success", "case": {"case_number": _case_number(existing["id"]), "status": existing["status"]}, "already_open": True})
        risk = _score_with_rules(conn,institution_id,dict(row))
        inserted = conn.execute(
            "INSERT INTO fraudshield_investigations (transaction_id, institution_id, risk_score, risk_level, history) VALUES (%s,%s,%s,%s,%s) RETURNING id,status,opened_at",
            (transaction_id, institution_id, risk["score"], risk["level"], json.dumps([{"action": "OPEN", "note": "Sent for investigation"}])),
        ).fetchone()
        conn.commit()
        _invalidate_intelligence_snapshot(institution_id)
        return jsonify({"status": "success", "case": {"case_number": _case_number(inserted["id"]), "status": inserted["status"], "opened_at": inserted["opened_at"].isoformat()}, "already_open": False})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@fraud_shield_bp.route("/lender/fraudshield/investigations", methods=["GET"])
@lender_required
def list_investigations():
    institution_id = session["user_id"]
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT fi.id, fi.transaction_id, fi.risk_score, fi.risk_level,
                   fi.status, fi.opened_at, fi.updated_at,
                   t.account_ref, t.merchant, t.amount, t.transaction_timestamp
            FROM fraudshield_investigations fi
            JOIN fraudshield_transactions t ON t.id=fi.transaction_id
            WHERE fi.institution_id=%s
            ORDER BY fi.opened_at DESC
            """,
            (institution_id,),
        ).fetchall()
        cases = [{
            "case_number": _case_number(r["id"]),
            "case_id": r["id"],
            "transaction_id": r["transaction_id"],
            "account_ref": r["account_ref"],
            "merchant": r["merchant"],
            "amount": float(r["amount"] or 0),
            "risk_score": r["risk_score"],
            "risk_level": r["risk_level"],
            "status": r["status"],
            "opened_at": r["opened_at"].isoformat() if r["opened_at"] else None,
        } for r in rows]
        return jsonify({"status": "success", "investigations": cases})
    finally:
        conn.close()


@fraud_shield_bp.route("/lender/fraudshield/investigations/<int:case_id>/status", methods=["POST"])
@lender_required
def update_investigation_status(case_id):
    institution_id = session["user_id"]
    body = request.get_json(silent=True) or {}
    action = (body.get("action") or "").strip().lower()
    if action not in STATUS_ACTIONS:
        return jsonify({"status": "error", "errors": ["Unknown investigation action."]}), 400
    conn = get_db()
    try:
        row = conn.execute("SELECT id,status,history FROM fraudshield_investigations WHERE id=%s AND institution_id=%s", (case_id, institution_id)).fetchone()
        if not row:
            abort(404)
        history = row["history"] or []
        history.append({"action": STATUS_ACTIONS[action], "note": f"Status set to {STATUS_ACTIONS[action]}"})
        updated = conn.execute(
            "UPDATE fraudshield_investigations SET status=%s,updated_at=now(),history=%s WHERE id=%s RETURNING id,status,updated_at",
            (STATUS_ACTIONS[action], json.dumps(history), case_id),
        ).fetchone()
        conn.commit()
        _invalidate_intelligence_snapshot(institution_id)
        return jsonify({"status": "success", "case": {"case_number": _case_number(updated["id"]), "status": updated["status"], "updated_at": updated["updated_at"].isoformat()}})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# ============================================================================
# POST-PHASE-1 FRAUD INTELLIGENCE API
# ============================================================================
from services.fraud_intelligence_service import (
    behavior_profile,
    impossible_travel,
    graph_snapshot,
    attack_payload,
    simulator_stats,
    event_fingerprint,
    risk_triage,
)


def _advanced_enrich_demo(conn, institution_id):
    """Add fraud-only device/session/integrity metadata to the demo stream."""
    # No borrower tables are referenced here.
    updates = [
        ("AC-10426", "TXN-AC10426-20260909-01", "UTR-1042601", "49.36.120.18", "Chrome", "Windows", "BEN-9001", 4, "Bhubaneswar"),
        ("AC-10428", "TXN-AC10428-20260910-01", "UTR-1042801", "103.54.22.19", "Safari", "iPhone", "BEN-9002", 3, "Bhubaneswar"),
        ("AC-10431", "TXN-AC10431-20260910-01", "UTR-1043101", "185.11.90.44", "Chrome", "Android", "BEN-9003", 2, "Bhubaneswar"),
        ("AC-10433", "TXN-AC10433-20260910-01", "UTR-1043301", "49.36.120.10", "Chrome", "Windows", "BEN-1001", 0, "Bhubaneswar"),
    ]
    if updates:
        values_sql=",".join(["(%s,%s,%s,%s,%s,%s,%s,%s,%s)"]*len(updates))
        flat=[v for row in updates for v in (row[0],row[1],row[2],row[3],row[4],row[5],row[6],row[7],row[8])]
        conn.execute(
            """UPDATE fraudshield_transactions AS t SET
               transaction_ref=v.txref, utr=v.utr, ip_address=v.ip, browser=v.browser, os=v.os,
               beneficiary_ref=v.beneficiary, failed_auth_count=v.failed, previous_location=v.prev_loc,
               previous_transaction_timestamp=t.transaction_timestamp-interval '27 minutes'
               FROM (VALUES """ + values_sql + """) AS v(account_ref,txref,utr,ip,browser,os,beneficiary,failed,prev_loc)
               WHERE t.institution_id=%s AND t.account_ref=v.account_ref""",
            flat+[institution_id],
        )
    # Seed a deliberately coordinated shared-device/beneficiary network.
    ring = [
        ("AC-RING-01", "BEN-RING-1", "BENEFICIARY", .98),
        ("AC-RING-02", "BEN-RING-1", "BENEFICIARY", .98),
        ("AC-RING-03", "BEN-RING-1", "BENEFICIARY", .98),
        ("AC-RING-04", "BEN-RING-1", "BENEFICIARY", .98),
        ("AC-RING-01", "AC-RING-02", "SHARED_DEVICE", .95),
        ("AC-RING-02", "AC-RING-03", "SHARED_DEVICE", .95),
        ("AC-RING-03", "AC-RING-04", "SHARED_DEVICE", .95),
        ("AC-RING-04", "AC-RING-01", "SHARED_DEVICE", .95),
    ]
    if ring:
        placeholders=",".join(["(%s,%s,%s,%s,%s)"]*len(ring))
        flat=[v for row in ring for v in (institution_id,row[0],row[1],row[2],row[3])]
        conn.execute(
            """INSERT INTO fraudshield_network_edges
               (institution_id, source_ref, target_ref, relationship_type, weight)
               VALUES """+placeholders+"""
               ON CONFLICT (institution_id,source_ref,target_ref,relationship_type)
               DO UPDATE SET weight=EXCLUDED.weight,last_seen=now()""", flat
        )
    conn.commit()


def _ensure_advanced_demo(conn, institution_id):
    key=int(institution_id)
    if key in _ADVANCED_ENRICHED: return
    _ensure_demo_stream(conn,institution_id)
    _advanced_enrich_demo(conn,institution_id)
    _ADVANCED_ENRICHED.add(key)

def _build_intelligence_snapshot(conn, institution_id, *, advanced=False):
    _ensure_demo_stream(conn,institution_id)
    if advanced:
        _ensure_advanced_demo(conn,institution_id)
    rows=[dict(r) for r in _fetch_transactions(conn,institution_id,500)]
    rule_config=_get_rule_config(conn,institution_id)
    scored=[(tx,score_transaction_fast(dict(tx, _rule_config=rule_config))) for tx in rows]
    case_map=_get_case_map(conn,institution_id,[tx["id"] for tx,_ in scored])
    tx_out=[]
    alerts=[]
    by_account=defaultdict(list)
    for tx,r in scored:
        by_account[str(tx.get("account_ref") or "UNKNOWN")].append(tx)
        c=case_map.get(tx["id"])
        tx_out.append({"transaction_id":tx["id"],"account_ref":tx["account_ref"],"merchant":tx["merchant"],"amount":float(tx["amount"] or 0),"timestamp":tx["transaction_timestamp"].isoformat() if tx.get("transaction_timestamp") else None,"date":tx["transaction_timestamp"].date().isoformat() if tx.get("transaction_timestamp") else None,"location":tx.get("location"),"channel":tx.get("channel"),"signals":{"new_device":bool(tx.get("is_new_device")),"velocity_1h":int(tx.get("txn_count_1h") or 0),"location_distance_km":float(tx.get("location_distance_km") or 0),"merchant_risk":float(tx.get("merchant_risk") or 0),"merchant_seen_before":bool(tx.get("merchant_seen_before")),"failed_auth_count":int(tx.get("failed_auth_count") or 0)},"risk_score":r["score"],"risk_level":r["level"],"risk_level_label":r["level_label"],"case":{"case_number":_case_number(c["id"]),"status":c["status"]} if c else None})
        if r["score"]>=31:
            alerts.append({"transaction_id":tx["id"],"account_ref":tx["account_ref"],"merchant":tx["merchant"],"amount":float(tx["amount"] or 0),"location":tx.get("location"),"score":r["score"],"level":r["level_label"],"triage":risk_triage(r["score"]),"signals":r.get("rule_contributors",[])[:5]})
    tx_out.sort(key=lambda x:(-x["risk_score"],x["timestamp"] or ""))
    counts={"normal":sum(x["risk_level"]=="NORMAL" for x in tx_out),"suspicious":sum(x["risk_level"]=="SUSPICIOUS" for x in tx_out),"review":sum(x["risk_level"]=="REVIEW" for x in tx_out),"high_risk":sum(x["risk_level"]=="HIGH_RISK" for x in tx_out)}
    total=len(tx_out)
    summary={"total":total,**counts,"open_investigations":sum(c["status"]!="CLOSED" for c in case_map.values()),"risk_distribution":{"normal_pct":round(counts["normal"]/total*100,1) if total else 0,"suspicious_pct":round(counts["suspicious"]/total*100,1) if total else 0,"review_pct":round(counts["review"]/total*100,1) if total else 0,"high_risk_pct":round(counts["high_risk"]/total*100,1) if total else 0}}
    buckets={}; merchants=defaultdict(lambda:{"transactions":0,"risk":0.0}); channels=Counter(); triage=Counter()
    for tx,r in scored:
        ts=tx.get("transaction_timestamp")
        if ts:
            k=ts.replace(minute=0,second=0,microsecond=0).isoformat(); buckets.setdefault(k,[]).append(r["score"])
        m=str(tx.get("merchant") or "Unknown"); merchants[m]["transactions"]+=1; merchants[m]["risk"]+=r["score"]
        channels[str(tx.get("channel") or "Unknown")]+=1; triage[r["triage"]["decision"]]+=1
    timeline=[{"hour":k,"risk":round(sum(v)/len(v),1),"transactions":len(v)} for k,v in sorted(buckets.items())][-24:]
    hotspots=[{"merchant":m,"transactions":v["transactions"],"avg_risk":round(v["risk"]/v["transactions"],1)} for m,v in merchants.items()]; hotspots.sort(key=lambda x:x["avg_risk"],reverse=True)
    edges=conn.execute("SELECT source_ref,target_ref,relationship_type,weight FROM fraudshield_network_edges WHERE institution_id=%s",(institution_id,)).fetchall()
    graph=graph_snapshot(rows,[dict(e) for e in edges])
    behaviors={acc:behavior_profile(rr) for acc,rr in by_account.items()}
    replay=conn.execute("SELECT COALESCE(SUM(duplicate_count),0) AS blocked FROM fraudshield_replay_registry WHERE institution_id=%s",(institution_id,)).fetchone(); audits=conn.execute("SELECT COUNT(*) AS n FROM fraudshield_audit_events WHERE institution_id=%s",(institution_id,)).fetchone()
    security={"controls":[{"name":"Institution data isolation","status":"ENFORCED","detail":"FraudShield reads only its dedicated transaction domain"},{"name":"Replay / duplicate protection","status":"ACTIVE","detail":f"{int(replay['blocked'] if replay else 0)} duplicate events blocked"},{"name":"Role authorization","status":"ACTIVE","detail":"Institution workspace endpoints require lender authorization"},{"name":"Auditability","status":"ACTIVE","detail":f"{int(audits['n'] if audits else 0)} FraudShield events recorded"},{"name":"Human-in-the-loop","status":"ENFORCED","detail":"Risk output is advisory; analysts control case disposition"}]}
    inv=conn.execute("""SELECT fi.id,fi.transaction_id,fi.risk_score,fi.risk_level,fi.status,fi.opened_at,t.account_ref,t.merchant,t.amount FROM fraudshield_investigations fi JOIN fraudshield_transactions t ON t.id=fi.transaction_id WHERE fi.institution_id=%s ORDER BY fi.opened_at DESC""",(institution_id,)).fetchall()
    investigations=[{"case_number":_case_number(r["id"]),"case_id":r["id"],"transaction_id":r["transaction_id"],"account_ref":r["account_ref"],"merchant":r["merchant"],"amount":float(r["amount"] or 0),"risk_score":r["risk_score"],"risk_level":r["risk_level"],"status":r["status"],"opened_at":r["opened_at"].isoformat() if r["opened_at"] else None} for r in inv]
    return {"status":"success","summary":summary,"transactions":tx_out[:200],"model":model_metadata(),"timeline":timeline,"merchant_hotspots":hotspots[:8],"channels":dict(channels),"triage":dict(triage),"alerts":sorted(alerts,key=lambda x:(-x["score"],x["transaction_id"]))[:50],"behaviors":behaviors,"graph":graph,"security":security,"investigations":investigations}

def _get_intelligence_snapshot(conn,institution_id,force=False,*,advanced=False):
    key=(int(institution_id), bool(advanced)); now=datetime.now(timezone.utc).timestamp()
    if not force:
        with _INTEL_CACHE_LOCK:
            hit=_INTEL_CACHE.get(key)
            if hit and now-hit[0]<_INTEL_CACHE_TTL: return hit[1]
    snap=_build_intelligence_snapshot(conn,institution_id,advanced=advanced)
    with _INTEL_CACHE_LOCK: _INTEL_CACHE[key]=(now,snap)
    return snap

def _invalidate_intelligence_snapshot(institution_id):
    with _INTEL_CACHE_LOCK:
        _INTEL_CACHE.pop((int(institution_id),False),None)
        _INTEL_CACHE.pop((int(institution_id),True),None)


def _audit(conn, institution_id, event_type, entity_type=None, entity_id=None, payload=None):
    conn.execute(
        "INSERT INTO fraudshield_audit_events(institution_id,event_type,entity_type,entity_id,payload) VALUES (%s,%s,%s,%s,%s)",
        (institution_id, event_type, entity_type, str(entity_id) if entity_id is not None else None, json.dumps(payload or {})),
    )


@fraud_shield_bp.route("/lender/fraudshield/intelligence/bootstrap", methods=["GET"])
@lender_required
def fraud_intelligence_bootstrap():
    institution_id=session["user_id"]
    conn=get_db()
    try:
        _ensure_intelligence_schema(conn)
        return jsonify(_get_intelligence_snapshot(conn,institution_id,force=request.args.get("refresh")=="1"))
    except Exception as exc:
        # Roll back any failed DDL/query so the connection can close cleanly and
        # log the real server-side cause instead of returning an opaque 500.
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("FraudShield intelligence bootstrap failed for institution %s", institution_id)
        return jsonify({
            "status":"error",
            "message":"FraudShield intelligence bootstrap failed",
            "detail":str(exc) if os.environ.get("FLASK_DEBUG") == "1" else None,
        }), 500
    finally:
        conn.close()


@fraud_shield_bp.route("/lender/fraudshield/intelligence/analytics", methods=["GET"])
@lender_required
def fraud_intelligence_analytics():
    institution_id=session["user_id"]; conn=get_db()
    try:
        snap=_get_intelligence_snapshot(conn,institution_id,advanced=False)
        return jsonify({"status":"success","timeline":snap["timeline"],"merchant_hotspots":snap["merchant_hotspots"],"channels":snap["channels"],"triage":snap["triage"],"note":"Analytics are derived only from the institution-owned FraudShield stream."})
    finally: conn.close()


@fraud_shield_bp.route("/lender/fraudshield/intelligence/alerts", methods=["GET"])
@lender_required
def fraud_intelligence_alerts():
    institution_id=session["user_id"]; conn=get_db()
    try: return jsonify({"status":"success","alerts":_get_intelligence_snapshot(conn,institution_id,advanced=False)["alerts"]})
    finally: conn.close()


@fraud_shield_bp.route("/lender/fraudshield/intelligence/behavior/<account_ref>", methods=["GET"])
@lender_required
def fraud_behavior(account_ref):
    institution_id=session["user_id"]; conn=get_db()
    try: return jsonify({"status":"success","profile":_get_intelligence_snapshot(conn,institution_id,advanced=False)["behaviors"].get(account_ref,{"account":account_ref,"samples":0})})
    finally: conn.close()


@fraud_shield_bp.route("/lender/fraudshield/intelligence/network", methods=["GET"])
@lender_required
def fraud_network():
    institution_id=session["user_id"]; conn=get_db()
    try: return jsonify({"status":"success","graph":_get_intelligence_snapshot(conn,institution_id,advanced=True)["graph"],"interpretation":"Graph links are evidence of relationships, not proof of coordinated fraud."})
    finally: conn.close()


@fraud_shield_bp.route("/lender/fraudshield/intelligence/simulate", methods=["POST"])
@lender_required
def fraud_simulate():
    institution_id=session["user_id"]
    body=request.get_json(silent=True) or {}
    scenario=(body.get("scenario") or "account_takeover").strip().lower()
    # Attack Simulator is a demo/investigation tool: persist generated events by
    # default so the next Autopilot scan operates on the exact attack just shown.
    # The checkbox can still explicitly disable persistence.
    persist=bool(body.get("persist", True))
    payload=attack_payload(scenario)
    results=[]
    now=datetime.now(timezone.utc)
    conn=get_db()
    try:
        if persist:
            _seed_demo_stream(conn,institution_id)
        simulation_run_id = now.strftime("%Y%m%d%H%M%S%f")
        for i,tx in enumerate(payload):
            tx=dict(tx)
            tx.setdefault("merchant_seen_before",True)
            tx.setdefault("device_id",f"SIM-{scenario[:5].upper()}-{i}")
            tx.setdefault("timestamp",(now).isoformat())
            tx["transaction_timestamp"]=now
            risk=_score_with_rules(conn,institution_id,tx)
            results.append({"transaction_ref":f"SIM-{scenario.upper()}-{simulation_run_id}-{i+1:02d}","account_ref":tx.get("account_ref"),"merchant":tx.get("merchant"),"amount":tx.get("amount"),"risk_score":risk["score"],"level":risk["level_label"],"signals":(risk.get("rule_contributors") or risk.get("contributors") or [])[:5]})
            if persist:
                txref=results[-1]["transaction_ref"]
                conn.execute("""INSERT INTO fraudshield_transactions
                    (institution_id,account_ref,merchant,amount,transaction_timestamp,location,device_id,account_age_days,txn_count_1h,txn_count_24h,location_distance_km,is_new_device,merchant_risk,transaction_hour,relationship_count,merchant_seen_before,device_trust_score,time_deviation_hours,amount_to_account_median,network_risk,channel,transaction_ref,beneficiary_ref,failed_auth_count,previous_location,previous_transaction_timestamp)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (institution_id,transaction_ref) DO NOTHING""",
                    (institution_id,tx.get("account_ref"),tx.get("merchant","Unknown"),tx.get("amount",1),now,tx.get("location"),tx.get("device_id"),tx.get("account_age_days",365),tx.get("txn_count_1h",1),tx.get("txn_count_24h",3),tx.get("location_distance_km",0),tx.get("is_new_device",False),tx.get("merchant_risk",0),now.hour,tx.get("relationship_count",1),tx.get("merchant_seen_before",True),tx.get("device_trust_score",0.9),tx.get("time_deviation_hours",1),tx.get("amount_to_account_median",1),tx.get("network_risk",0.05),tx.get("channel","UPI"),txref,tx.get("beneficiary_ref"),tx.get("failed_auth_count",0),tx.get("previous_location"),None)
                )
        replay_blocked=0
        if scenario == "replay_attack" and payload:
            # Intentionally replay the exact same event fingerprint to prove integrity controls.
            replay_event = dict(payload[0])
            replay_event["transaction_ref"] = "SIM-REPLAY-01"
            replay_event["timestamp"] = now.isoformat()
            fp = event_fingerprint(replay_event)
            existing = conn.execute("SELECT id,duplicate_count FROM fraudshield_replay_registry WHERE institution_id=%s AND fingerprint=%s",(institution_id,fp)).fetchone()
            if existing:
                replay_blocked=1
                conn.execute("UPDATE fraudshield_replay_registry SET duplicate_count=duplicate_count+1,last_seen=now() WHERE id=%s",(existing["id"],))
            else:
                conn.execute("INSERT INTO fraudshield_replay_registry(institution_id,fingerprint,transaction_ref) VALUES (%s,%s,%s)",(institution_id,fp,replay_event["transaction_ref"]))
                replay_blocked=0
        if persist:
            _audit(conn,institution_id,"SIMULATION", "scenario", scenario, {"generated":len(results),"replay_blocked":replay_blocked})
            conn.commit()
            _invalidate_intelligence_snapshot(institution_id)
        stats=simulator_stats(results)
        stats["replay_blocked"]=replay_blocked
        return jsonify({"status":"success","scenario":scenario,"results":results,"stats":stats,"persisted":persist})
    finally:
        conn.close()


def _derive_ingestion_context(conn, institution_id, body, parsed_ts):
    """Derive context from the independent FraudShield stream for a live event.

    The caller may override explicit telemetry fields (e.g. location distance or
    merchant risk), but velocity, prior device/merchant usage, amount baseline,
    and prior event context are derived from FraudShield-owned history whenever
    the event does not provide them.
    """
    account = str(body.get("account_ref") or "").strip()
    merchant = str(body.get("merchant") or "Unknown Merchant").strip()
    device_id = body.get("device_id")
    amount = float(body.get("amount") or 0)
    one_hour = conn.execute(
        "SELECT COUNT(*) AS n FROM fraudshield_transactions WHERE institution_id=%s AND account_ref=%s AND transaction_timestamp >= %s - interval '1 hour' AND transaction_timestamp <= %s",
        (institution_id, account, parsed_ts, parsed_ts),
    ).fetchone()["n"]
    day_count = conn.execute(
        "SELECT COUNT(*) AS n FROM fraudshield_transactions WHERE institution_id=%s AND account_ref=%s AND transaction_timestamp >= %s - interval '24 hours' AND transaction_timestamp <= %s",
        (institution_id, account, parsed_ts, parsed_ts),
    ).fetchone()["n"]
    previous = conn.execute(
        """SELECT location, transaction_timestamp, device_id, account_age_days,
                  device_trust_score, merchant_seen_before
           FROM fraudshield_transactions
           WHERE institution_id=%s AND account_ref=%s
           ORDER BY transaction_timestamp DESC LIMIT 1""",
        (institution_id, account),
    ).fetchone()
    baseline = conn.execute(
        "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY amount) AS median_amount, COUNT(*) AS n FROM fraudshield_transactions WHERE institution_id=%s AND account_ref=%s",
        (institution_id, account),
    ).fetchone()
    merchant_seen = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM fraudshield_transactions WHERE institution_id=%s AND account_ref=%s AND lower(merchant)=lower(%s)) AS seen",
        (institution_id, account, merchant),
    ).fetchone()["seen"]
    merchant_stats = conn.execute(
        "SELECT AVG(merchant_risk) AS risk FROM fraudshield_transactions WHERE institution_id=%s AND lower(merchant)=lower(%s)",
        (institution_id, merchant),
    ).fetchone()
    known_device = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM fraudshield_transactions WHERE institution_id=%s AND account_ref=%s AND device_id=%s) AS seen",
        (institution_id, account, device_id),
    ).fetchone()["seen"] if device_id else False
    relation_count = conn.execute(
        "SELECT COUNT(DISTINCT beneficiary_ref) AS n FROM fraudshield_transactions WHERE institution_id=%s AND account_ref=%s AND beneficiary_ref IS NOT NULL",
        (institution_id, account),
    ).fetchone()["n"]
    ctx = {
        "txn_count_1h": int(body.get("txn_count_1h") if body.get("txn_count_1h") is not None else int(one_hour) + 1),
        "txn_count_24h": int(body.get("txn_count_24h") if body.get("txn_count_24h") is not None else int(day_count) + 1),
        "merchant_seen_before": bool(body.get("merchant_seen_before") if body.get("merchant_seen_before") is not None else merchant_seen),
        "is_new_device": bool(body.get("is_new_device") if body.get("is_new_device") is not None else (not known_device and bool(device_id))),
        "account_age_days": int(body.get("account_age_days") if body.get("account_age_days") is not None else (previous["account_age_days"] if previous else 365)),
        "device_trust_score": float(body.get("device_trust_score") if body.get("device_trust_score") is not None else (previous["device_trust_score"] if previous and previous["device_id"] == device_id else 0.12 if device_id else 0.8)),
        "merchant_risk": float(body.get("merchant_risk") if body.get("merchant_risk") is not None else (merchant_stats["risk"] if merchant_stats and merchant_stats["risk"] is not None else 0.50)),
        "amount_to_account_median": float(body.get("amount_to_account_median") if body.get("amount_to_account_median") is not None else (amount / float(baseline["median_amount"] or amount or 1.0))),
        "relationship_count": int(body.get("relationship_count") if body.get("relationship_count") is not None else relation_count),
        "previous_location": body.get("previous_location") if body.get("previous_location") is not None else (previous["location"] if previous else None),
        "previous_transaction_timestamp": body.get("previous_transaction_timestamp") if body.get("previous_transaction_timestamp") is not None else (previous["transaction_timestamp"] if previous else None),
    }
    # Never overwrite explicitly supplied telemetry; only derive missing values.
    for k, v in body.items():
        if k in {"txn_count_1h","txn_count_24h","merchant_seen_before","is_new_device","account_age_days","device_trust_score","merchant_risk","amount_to_account_median","relationship_count","previous_location","previous_transaction_timestamp"} and v is not None:
            ctx[k]=v
    # If no location distance was supplied, leave it unknown rather than inventing kilometers.
    ctx["location_distance_km"] = float(body.get("location_distance_km") or 0)
    ctx["time_deviation_hours"] = float(body.get("time_deviation_hours") or 0)
    ctx["network_risk"] = float(body.get("network_risk") if body.get("network_risk") is not None else 0.05)
    ctx["failed_auth_count"] = int(body.get("failed_auth_count") or 0)
    return ctx


@fraud_shield_bp.route("/lender/fraudshield/intelligence/ingest", methods=["POST"])
@lender_required
def fraud_ingest():
    """Ingest one simulated/live event with replay protection and scoring."""
    institution_id=session["user_id"]
    body=request.get_json(silent=True) or {}
    if not body.get("account_ref") or not body.get("amount"):
        return jsonify({"status":"error","errors":["account_ref and amount are required"]}),400
    fp=event_fingerprint(body)
    conn=get_db()
    try:
        existing=conn.execute("SELECT id,duplicate_count FROM fraudshield_replay_registry WHERE institution_id=%s AND fingerprint=%s",(institution_id,fp)).fetchone()
        if existing:
            conn.execute("UPDATE fraudshield_replay_registry SET duplicate_count=duplicate_count+1,last_seen=now() WHERE id=%s",(existing["id"],))
            _audit(conn,institution_id,"REPLAY_BLOCKED","transaction",body.get("transaction_ref"),{"fingerprint":fp})
            conn.commit()
            return jsonify({"status":"success","duplicate":True,"message":"Duplicate/replay event blocked","duplicate_count":int(existing["duplicate_count"])+1})
        ts=body.get("timestamp") or datetime.now(timezone.utc).isoformat()
        try: parsed_ts=datetime.fromisoformat(str(ts).replace("Z","+00:00"))
        except Exception: parsed_ts=datetime.now(timezone.utc)
        derived=_derive_ingestion_context(conn,institution_id,body,parsed_ts)
        tx={
            "account_ref":body.get("account_ref"),"merchant":body.get("merchant","Unknown Merchant"),"amount":float(body.get("amount")),"transaction_timestamp":parsed_ts,
            "location":body.get("location"),"device_id":body.get("device_id"),"account_age_days":derived["account_age_days"],"txn_count_1h":derived["txn_count_1h"],"txn_count_24h":derived["txn_count_24h"],"location_distance_km":derived["location_distance_km"],"is_new_device":derived["is_new_device"],"merchant_risk":derived["merchant_risk"],"transaction_hour":parsed_ts.hour,"relationship_count":derived["relationship_count"],"merchant_seen_before":derived["merchant_seen_before"],"device_trust_score":derived["device_trust_score"],"time_deviation_hours":derived["time_deviation_hours"],"amount_to_account_median":derived["amount_to_account_median"],"network_risk":derived["network_risk"],"channel":body.get("channel","UPI"),"failed_auth_count":derived["failed_auth_count"],"previous_location":derived.get("previous_location"),"previous_transaction_timestamp":derived.get("previous_transaction_timestamp")
        }
        risk=_score_with_rules(conn,institution_id,tx)
        txref=body.get("transaction_ref") or f"FS-LIVE-{int(datetime.now(timezone.utc).timestamp()*1000)}"
        utr=body.get("utr")
        inserted_tx=conn.execute("""INSERT INTO fraudshield_transactions
          (institution_id,account_ref,merchant,amount,transaction_timestamp,location,device_id,account_age_days,txn_count_1h,txn_count_24h,location_distance_km,is_new_device,merchant_risk,transaction_hour,relationship_count,merchant_seen_before,device_trust_score,time_deviation_hours,amount_to_account_median,network_risk,channel,transaction_ref,utr,failed_auth_count,previous_location,previous_transaction_timestamp)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
          (institution_id,tx["account_ref"],tx["merchant"],tx["amount"],parsed_ts,tx.get("location"),tx.get("device_id"),tx["account_age_days"],tx["txn_count_1h"],tx["txn_count_24h"],tx["location_distance_km"],tx["is_new_device"],tx["merchant_risk"],tx["transaction_hour"],tx["relationship_count"],tx["merchant_seen_before"],tx["device_trust_score"],tx["time_deviation_hours"],tx["amount_to_account_median"],tx["network_risk"],tx["channel"],txref,utr,tx["failed_auth_count"],tx.get("previous_location"),None))
        conn.execute("INSERT INTO fraudshield_replay_registry(institution_id,fingerprint,transaction_ref) VALUES (%s,%s,%s)",(institution_id,fp,txref))
        tx["id"]=inserted_tx["id"]; tx["transaction_ref"]=txref; tx["utr"]=utr
        response=_run_autonomous_response(conn,institution_id,tx,risk)
        _audit(conn,institution_id,"TRANSACTION_INGESTED","transaction",txref,{"risk_score":risk["score"],"autopilot_action":response.get("action")})
        conn.commit()
        _invalidate_intelligence_snapshot(institution_id)
        return jsonify({"status":"success","duplicate":False,"transaction_ref":txref,"risk":risk,"triage":risk["triage"],"autopilot":_response_public(response)})
    finally:
        conn.close()


@fraud_shield_bp.route("/api/fraudshield/webhooks/payment", methods=["POST"])
def fraud_payment_webhook():
    """Sandbox/payment-provider event ingress.

    Authentication uses a shared webhook secret so external payment events do not
    require an interactive lender session. The payload is normalized into the same
    FraudShield ingestion path. Set FRAUDSHIELD_WEBHOOK_KEY in the server env.
    """
    secret = os.environ.get("FRAUDSHIELD_WEBHOOK_KEY")
    supplied = request.headers.get("X-FraudShield-Key") or request.headers.get("X-Webhook-Key")
    if secret and supplied != secret:
        return jsonify({"status":"error","message":"Invalid webhook credentials"}), 401
    body=request.get_json(silent=True) or {}
    institution_id=body.get("institution_id")
    if not institution_id:
        return jsonify({"status":"error","message":"institution_id is required for sandbox routing"}),400
    # Reuse the authenticated ingestion implementation in a small, isolated service-style flow.
    payload=dict(body); payload["_source"]="payment_webhook"
    conn=get_db()
    try:
        if not payload.get("account_ref") or payload.get("amount") is None:
            return jsonify({"status":"error","message":"account_ref and amount are required"}),400
        fp=event_fingerprint(payload)
        existing=conn.execute("SELECT id,duplicate_count FROM fraudshield_replay_registry WHERE institution_id=%s AND fingerprint=%s",(institution_id,fp)).fetchone()
        if existing:
            conn.execute("UPDATE fraudshield_replay_registry SET duplicate_count=duplicate_count+1,last_seen=now() WHERE id=%s",(existing["id"],))
            conn.commit(); return jsonify({"status":"success","duplicate":True,"message":"Duplicate/replay event blocked","duplicate_count":int(existing["duplicate_count"])+1}),200
        raw_ts=payload.get("timestamp") or payload.get("transaction_timestamp") or datetime.now(timezone.utc).isoformat()
        try: parsed_ts=datetime.fromisoformat(str(raw_ts).replace("Z","+00:00"))
        except Exception: parsed_ts=datetime.now(timezone.utc)
        derived=_derive_ingestion_context(conn,institution_id,payload,parsed_ts)
        tx={"account_ref":payload["account_ref"],"merchant":payload.get("merchant","Unknown Merchant"),"amount":float(payload["amount"]),"transaction_timestamp":parsed_ts,"location":payload.get("location"),"device_id":payload.get("device_id"),"account_age_days":derived["account_age_days"],"txn_count_1h":derived["txn_count_1h"],"txn_count_24h":derived["txn_count_24h"],"location_distance_km":derived["location_distance_km"],"is_new_device":derived["is_new_device"],"merchant_risk":derived["merchant_risk"],"transaction_hour":parsed_ts.hour,"relationship_count":derived["relationship_count"],"merchant_seen_before":derived["merchant_seen_before"],"device_trust_score":derived["device_trust_score"],"time_deviation_hours":derived["time_deviation_hours"],"amount_to_account_median":derived["amount_to_account_median"],"network_risk":derived["network_risk"],"channel":payload.get("channel","UPI"),"failed_auth_count":derived["failed_auth_count"],"previous_location":derived.get("previous_location"),"previous_transaction_timestamp":derived.get("previous_transaction_timestamp")}
        risk=_score_with_rules(conn,institution_id,tx,fast=True)
        txref=payload.get("transaction_ref") or f"FS-WEBHOOK-{int(datetime.now(timezone.utc).timestamp()*1000)}"
        inserted_tx=conn.execute("""INSERT INTO fraudshield_transactions(institution_id,account_ref,merchant,amount,transaction_timestamp,location,device_id,account_age_days,txn_count_1h,txn_count_24h,location_distance_km,is_new_device,merchant_risk,transaction_hour,relationship_count,merchant_seen_before,device_trust_score,time_deviation_hours,amount_to_account_median,network_risk,channel,transaction_ref,utr,failed_auth_count,previous_location,previous_transaction_timestamp) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",(institution_id,tx["account_ref"],tx["merchant"],tx["amount"],parsed_ts,tx.get("location"),tx.get("device_id"),tx["account_age_days"],tx["txn_count_1h"],tx["txn_count_24h"],tx["location_distance_km"],tx["is_new_device"],tx["merchant_risk"],tx["transaction_hour"],tx["relationship_count"],tx["merchant_seen_before"],tx["device_trust_score"],tx["time_deviation_hours"],tx["amount_to_account_median"],tx["network_risk"],tx["channel"],txref,payload.get("utr"),tx["failed_auth_count"],tx.get("previous_location"),tx.get("previous_transaction_timestamp")))
        tx["id"]=inserted_tx["id"]; tx["transaction_ref"]=txref; tx["utr"]=payload.get("utr")
        conn.execute("INSERT INTO fraudshield_replay_registry(institution_id,fingerprint,transaction_ref) VALUES (%s,%s,%s)",(institution_id,fp,txref))
        response=_run_autonomous_response(conn,institution_id,tx,risk)
        _audit(conn,institution_id,"PAYMENT_WEBHOOK_RECEIVED","transaction",txref,{"risk_score":risk["score"],"autopilot_action":response.get("action")}); conn.commit(); _invalidate_intelligence_snapshot(institution_id)
        return jsonify({"status":"success","duplicate":False,"transaction_ref":txref,"risk":risk,"triage":risk["triage"],"autopilot":_response_public(response)}),201
    except Exception:
        conn.rollback(); logger.exception("FraudShield payment webhook failed"); return jsonify({"status":"error","message":"Payment event could not be processed"}),500
    finally:
        conn.close()


@fraud_shield_bp.route("/lender/fraudshield/intelligence/evaluation", methods=["GET"])
@lender_required
def fraud_model_evaluation():
    return jsonify({"status":"success","evaluation":evaluate_fraud_model()})


@fraud_shield_bp.route("/lender/fraudshield/intelligence/operations", methods=["GET"])
@lender_required
def fraud_operations():
    institution_id=session["user_id"]
    conn=get_db()
    try:
        stats=queue_stats(conn,institution_id)
        replay=conn.execute("SELECT COALESCE(SUM(duplicate_count),0) blocked FROM fraudshield_replay_registry WHERE institution_id=%s",(institution_id,)).fetchone()
        audit=conn.execute("SELECT COUNT(*) n FROM fraudshield_audit_events WHERE institution_id=%s",(institution_id,)).fetchone()
        return jsonify({"status":"success","async_queue":stats,"replays_blocked":int(replay["blocked"] or 0),"audit_events":int(audit["n"] or 0)})
    finally: conn.close()


@fraud_shield_bp.route("/lender/fraudshield/intelligence/copilot", methods=["POST"])
@lender_required
def fraud_copilot():
    institution_id=session["user_id"]
    body=request.get_json(silent=True) or {}
    question=(body.get("question") or "").strip()
    transaction_id=body.get("transaction_id")
    conn=get_db()
    try:
        context=[]
        selected=None
        if transaction_id:
            row=conn.execute("SELECT * FROM fraudshield_transactions WHERE id=%s AND institution_id=%s",(transaction_id,institution_id)).fetchone()
            if row:
                selected=dict(row); r=_score_with_rules(conn,institution_id,selected); context.append({"transaction":selected,"risk":r})
        if not context:
            rows=conn.execute("SELECT * FROM fraudshield_transactions WHERE institution_id=%s ORDER BY transaction_timestamp DESC LIMIT 8",(institution_id,)).fetchall()
            for row in rows:
                d=dict(row); context.append({"transaction":d,"risk":_score_with_rules(conn,institution_id,d)})
        structured=[]
        for item in context[:8]:
            t=item["transaction"]; r=item["risk"]
            structured.append({"account":t.get("account_ref"),"merchant":t.get("merchant"),"amount":float(t.get("amount") or 0),"risk":r["score"],"level":r["level_label"],"takeover_risk":r.get("account_takeover_risk"),"reason":r.get("reason")})
        # Safe, deterministic fallback if no Gemini key/service is available.
        answer=None
        try:
            from services.gemini_service import generate_financial_explanation
            prompt=("You are FraudShield Copilot. Answer using ONLY the supplied structured evidence. "
                    "Do not invent transactions, people, locations, or evidence. Do not declare fraud as fact. "
                    f"Question: {question}\nStructured evidence: {json.dumps(structured)}")
            resp=generate_financial_explanation(prompt)
            if isinstance(resp,dict): answer=resp.get("summary") or resp.get("text")
        except Exception:
            answer=None
        if not answer:
            if selected:
                r=_score_with_rules(conn,institution_id,selected)
                answer=(f"{selected.get('account_ref')} has a {r['score']}/100 {r['level_label']} event. "
                        f"The strongest evidence includes: {r['reason']} Review the structured evidence before disposition.")
            else:
                high=[x for x in structured if int(x["risk"])>=61]
                answer=(f"I found {len(high)} transaction(s) at review level or above in the recent FraudShield stream. "
                        "Open a transaction for detailed evidence and investigation actions.")
        _audit(conn,institution_id,"COPILOT_QUERY","transaction",transaction_id,{"question":question})
        conn.commit()
        return jsonify({"status":"success","answer":answer,"evidence":structured})
    finally:
        conn.close()


@fraud_shield_bp.route("/lender/fraudshield/intelligence/security", methods=["GET"])
@lender_required
def fraud_security_health():
    institution_id=session["user_id"]; conn=get_db()
    try: return jsonify({"status":"success",**_get_intelligence_snapshot(conn,institution_id,advanced=False)["security"]})
    finally: conn.close()
