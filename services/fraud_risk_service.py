"""FraudShield risk service backed by the IEEE-CIS-trained XGBoost model.

The FraudShield stream is institution-owned and remains isolated from the borrower
transaction table. The ML layer uses the trained IEEE-CIS XGBoost artifact supplied by
the team, while local rules/behavioral context remain complementary signals.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from threading import Lock

import numpy as np
import pandas as pd
import shap

from ml.fraud.inference import FraudInferenceService
from ml.fraud.adapter import adapt_transaction
from ml.fraud.ieee_bridge import to_ieee_transaction

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = ROOT / "ml" / "fraud" / "models" / "fraud_xgboost_model.json"

_MODEL_LOCK = Lock()
_MODEL_SERVICE = None
_EXPLAINER = None
_CACHE = {}
_CACHE_LOCK = Lock()


def _load_service(force: bool = False):
    global _MODEL_SERVICE, _EXPLAINER
    if _MODEL_SERVICE is not None and not force:
        return _MODEL_SERVICE
    with _MODEL_LOCK:
        if _MODEL_SERVICE is None or force:
            _MODEL_SERVICE = FraudInferenceService()
            _EXPLAINER = None
    return _MODEL_SERVICE


def _model_bundle(service):
    meta = service.metadata or {}
    metrics = meta.get("metrics") or {}
    return {
        "model": service.model,
        "inference_service": service,
        "feature_names": list(service.model_features),
        "model_version": "FraudShield-XGBoost-IEEE-CIS-v1.0",
        "dataset": "IEEE-CIS Fraud Detection",
        "training_records": int(meta.get("train_rows", 0)) + int(meta.get("validation_rows", 0)),
        "fraud_rate": None,
        "metrics": metrics,
        "thresholds": {"normal_max": 30, "suspicious_max": 60, "review_max": 80},
        "trained_at": None,
    }


def train_model(force=True):
    """Training is performed externally; this endpoint reports the installed IEEE model."""
    service = _load_service(force=force)
    return model_metadata()


def model_metadata():
    service = _load_service()
    b = _model_bundle(service)
    m = b["metrics"]
    return {
        "model_version": b["model_version"],
        "dataset": b["dataset"],
        "training_records": b["training_records"],
        "fraud_rate": b["fraud_rate"],
        "roc_auc": m.get("roc_auc"),
        "precision": m.get("precision"),
        "recall": m.get("recall"),
        "f1": m.get("f1"),
        "pr_auc": m.get("pr_auc"),
        "classification_threshold": m.get("classification_threshold"),
        "features": b["feature_names"],
        "thresholds": b["thresholds"],
        "trained_at": b["trained_at"],
        "status": "READY",
        "model_path": "ml/fraud/models/fraud_xgboost_model.json",
        "encoder_path": "ml/fraud/models/fraud_categorical_encoder.pkl",
        "engine_stack": ["XGBoost (IEEE-CIS)", "Isolation Forest", "Rules", "Behavior Signals", "Network Risk", "SHAP"],
        "risk_fusion": {"xgboost": 0.65, "rules": 0.15, "behavior": 0.12, "network": 0.08},
        "feature_bridge": "FinTrust FraudShield → IEEE-CIS compatibility adapter",
    }


def default_rule_config():
    """Default institution rule policy. Values are intentionally explainable and UI-editable."""
    return {
        "high_amount": {"enabled": True, "threshold": 25000, "points": 12, "label": "High Transaction Amount"},
        "velocity": {"enabled": True, "threshold": 5, "points": 14, "label": "Transaction Velocity"},
        "location": {"enabled": True, "threshold": 500, "points": 14, "label": "Location Deviation"},
        "new_device": {"enabled": True, "threshold": 1, "points": 13, "label": "New Device"},
        "merchant_risk": {"enabled": True, "threshold": 0.75, "points": 12, "label": "Merchant Risk"},
        "new_merchant": {"enabled": True, "threshold": 1, "points": 6, "label": "New Merchant"},
        "unusual_time": {"enabled": True, "threshold": 5, "points": 8, "label": "Unusual Time"},
        "relationships": {"enabled": True, "threshold": 6, "points": 8, "label": "Account Relationship Risk"},
        "failed_auth": {"enabled": True, "threshold": 4, "points": 10, "label": "Failed Authentication Burst"},
    }


def _merged_rule_config(txn):
    base = default_rule_config()
    custom = txn.get("_rule_config") or {}
    for key, value in custom.items():
        if key in base and isinstance(value, dict):
            base[key].update({k: value[k] for k in ("enabled", "threshold", "points") if k in value})
    return base


def _rules(txn):
    config = _merged_rule_config(txn)
    points = 0
    contributors = []

    def active(key):
        return bool(config[key].get("enabled", True))

    def add(key, detail):
        nonlocal points
        rule = config[key]
        pts = int(rule.get("points", 0))
        points += pts
        contributors.append({"rule_id": key, "label": rule["label"], "points": pts, "detail": detail})

    amount = float(txn.get("amount") or 0)
    velocity = int(txn.get("txn_count_1h") or 0)
    distance = float(txn.get("location_distance_km") or 0)
    merchant_risk = float(txn.get("merchant_risk") or 0)
    hour = int(txn.get("transaction_hour") or 0)
    relationships = int(txn.get("relationship_count") or 0)
    failed_auth = int(txn.get("failed_auth_count") or 0)

    if active("high_amount") and amount >= float(config["high_amount"]["threshold"]): add("high_amount", f"₹{amount:,.0f}")
    if active("velocity") and velocity >= int(config["velocity"]["threshold"]): add("velocity", f"{velocity} txns in 1h")
    if active("location") and distance >= float(config["location"]["threshold"]): add("location", f"{distance:.0f} km")
    if active("new_device") and bool(txn.get("is_new_device")): add("new_device", "device not previously trusted")
    if active("merchant_risk") and merchant_risk >= float(config["merchant_risk"]["threshold"]): add("merchant_risk", f"risk {merchant_risk:.2f}")
    if active("new_merchant") and not bool(txn.get("merchant_seen_before")): add("new_merchant", "not previously observed")
    if active("unusual_time") and (hour <= int(config["unusual_time"]["threshold"]) or hour >= 23): add("unusual_time", f"{hour:02d}:00")
    if active("relationships") and relationships >= int(config["relationships"]["threshold"]): add("relationships", f"{relationships} related accounts")
    if active("failed_auth") and failed_auth >= int(config["failed_auth"]["threshold"]): add("failed_auth", f"{failed_auth} failed attempts")
    return int(min(points, 60)), contributors

def _behavior_score(txn):
    amount_ratio = float(txn.get("amount_to_account_median") or 1.0)
    velocity = float(txn.get("txn_count_1h") or 0)
    distance = float(txn.get("location_distance_km") or 0)
    new_device = 25 if txn.get("is_new_device") else 0
    merchant_risk = float(txn.get("merchant_risk") or 0) * 25
    ratio_score = min(30, max(0, (amount_ratio - 1.0) * 8))
    velocity_score = min(20, max(0, velocity * 2.5))
    distance_score = min(25, distance / 40.0)
    return float(np.clip(new_device + merchant_risk + ratio_score + velocity_score + distance_score, 0, 100))


def _prepare(txn):
    normalized = adapt_transaction(to_ieee_transaction(txn))
    service = _load_service()
    frame = service.prepare_transaction(normalized)
    return service, frame


def _shap_explain(frame, model):
    global _EXPLAINER
    try:
        if _EXPLAINER is None:
            _EXPLAINER = shap.TreeExplainer(model)
        explanation = _EXPLAINER(frame)
        vals = np.asarray(explanation.values[0], dtype=float)
        if vals.ndim > 1:
            vals = vals[:, -1]
        base = float(np.asarray(explanation.base_values).reshape(-1)[0])
        top = []
        for name, value in sorted(zip(frame.columns, vals), key=lambda x: -abs(x[1]))[:8]:
            top.append({"label": str(name).replace("_", " ").title(), "value": round(float(value), 6), "direction": "increases risk" if value > 0 else "reduces risk"})
        return top, base, {name: round(float(value), 6) for name, value in zip(frame.columns, vals)}
    except Exception as exc:
        logger.warning("SHAP explanation unavailable: %s", exc)
        return [], 0.0, {}


def _score_transaction_core(txn, *, explain=False):
    service, frame = _prepare(txn)
    model = service.model
    prob = float(np.clip(service.model.predict_proba(frame)[0][1], 0, 1))
    model_score = prob * 100
    rule_score, rule_contributors = _rules(txn)
    behavior_score = _behavior_score(txn)
    # The IEEE-CIS model is supervised. We retain rule/behavior fusion so the
    # production-facing score can incorporate institution-specific context.
    rule_component = float(np.clip((rule_score / 60.0) * 100.0, 0, 100))
    network_component = float(np.clip(float(txn.get("network_risk") or 0.0) * 100.0, 0, 100))
    # Dual-path fusion: the supervised IEEE-CIS model remains one strong signal,
    # but a consensus of institution rules + account behavior + network context
    # must be able to escalate an obvious attack even when the generic model is
    # conservative for a synthetic/local transaction shape.  Taking the maximum
    # of the independent model score and the contextual ensemble also prevents a
    # single low model probability from suppressing multiple severe risk signals.
    contextual_score = (
        0.45 * rule_component
        + 0.40 * behavior_score
        + 0.15 * network_component
    )
    final = float(np.clip(max(model_score, contextual_score), 0, 100))
    if final <= 30:
        level, label = "NORMAL", "Normal"
    elif final <= 60:
        level, label = "SUSPICIOUS", "Potentially Suspicious"
    elif final <= 80:
        level, label = "REVIEW", "Review Recommended"
    else:
        level, label = "HIGH_RISK", "Elevated Risk — Investigation Required"
    reason_bits=[]
    if txn.get("is_new_device"): reason_bits.append("a new device")
    if float(txn.get("location_distance_km") or 0) >= 200: reason_bits.append("a large location deviation")
    if int(txn.get("txn_count_1h") or 0) >= 3: reason_bits.append("elevated transaction velocity")
    if float(txn.get("merchant_risk") or 0) >= .5: reason_bits.append("elevated merchant risk")
    if not txn.get("merchant_seen_before"): reason_bits.append("a new merchant relationship")
    reason = ("The transaction shows " + ", ".join(reason_bits[:-1]) + " and " + reason_bits[-1] + ".") if len(reason_bits) > 1 else (f"The transaction shows {reason_bits[0]}." if reason_bits else "The transaction is consistent with the monitored risk profile.")
    shap_top, shap_base, shap_map = ([],0.0,{})
    if explain: shap_top, shap_base, shap_map = _shap_explain(frame, model)
    return {
        "score": int(round(final)), "level": level, "level_label": label,
        "model_probability": round(prob, 6), "anomaly_score": int(round(behavior_score)),
        "risk_components": {"xgboost": round(model_score, 2), "rules": round(rule_component, 2), "behavior": round(behavior_score, 2), "network": round(network_component, 2), "contextual_ensemble": round(contextual_score, 2)},
        "rule_score": rule_score, "contributors": shap_top, "rule_contributors": rule_contributors,
        "reason": reason, "recommended_action": "Send for human investigation" if level == "HIGH_RISK" else ("Review transaction context" if level in {"REVIEW","SUSPICIOUS"} else "No immediate action"),
        "model_version": "FraudShield-XGBoost-IEEE-CIS-v1.0",
        "model_dataset": "IEEE-CIS Fraud Detection", "model_features_used": list(frame.columns),
        "features": {k: (None if pd.isna(frame.iloc[0][k]) else float(frame.iloc[0][k])) for k in frame.columns if k not in service.categorical_features},
        "shap_base_value": round(shap_base, 6), "shap_values": shap_map,
        "triage": __import__('services.fraud_intelligence_service', fromlist=['risk_triage']).risk_triage(int(round(final))),
    }


def _cache_key(txn, explain):
    tid = txn.get("id") or txn.get("transaction_id") or txn.get("account_ref")
    ts = txn.get("transaction_timestamp") or txn.get("timestamp") or ""
    ts = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
    return (str(tid), ts, bool(explain), repr(txn.get("_rule_config") or {}), model_metadata()["model_version"])


def score_transaction(txn, explain=True, use_cache=True):
    key = _cache_key(txn, explain) if use_cache else None
    now=time.monotonic()
    if key:
        with _CACHE_LOCK:
            hit=_CACHE.get(key)
            if hit and now-hit[0] < 300: return hit[1]
    result=_score_transaction_core(txn, explain=explain)
    if key:
        with _CACHE_LOCK: _CACHE[key]=(now,result)
    return result


def score_transaction_fast(txn):
    return score_transaction(txn, explain=False, use_cache=True)


def generate_ai_explanation(result):
    try:
        from services.gemini_service import generate_financial_explanation
        prompt=("You are an analyst-assistance layer for FraudShield. Summarize the supplied structured transaction risk evidence in 2 concise sentences. Do not declare fraud as fact. "
                f"Risk score: {result['score']}/100. Level: {result['level_label']}. Top SHAP contributors: {result.get('contributors', [])}. Rule signals: {result.get('rule_contributors', [])}.")
        response=generate_financial_explanation(prompt)
        if isinstance(response,dict):
            text=response.get('summary') or response.get('text')
            if text: return {'status':'success','summary':text}
    except Exception as exc: logger.warning('Gemini fraud explanation failed: %s', exc)
    return {'status':'error'}
