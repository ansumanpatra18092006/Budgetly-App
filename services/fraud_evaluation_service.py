"""Deterministic FraudShield benchmark/evaluation harness."""
from __future__ import annotations
import time
from statistics import mean
from services.fraud_intelligence_service import attack_payload
from services.fraud_risk_service import score_transaction


def _normal(i):
    return {"account_ref":f"BENCH-N-{i}","merchant":"Known Merchant","amount":800+i*20,"transaction_hour":14,
            "txn_count_1h":1,"txn_count_24h":3,"location_distance_km":2,"is_new_device":False,
            "merchant_risk":0.05,"relationship_count":1,"merchant_seen_before":True,"device_trust_score":0.95,
            "time_deviation_hours":0.4,"amount_to_account_median":1.0,"network_risk":0.03,"failed_auth_count":0}

def evaluate_fraud_model():
    samples=[]
    for i in range(12): samples.append((_normal(i),0,"normal"))
    for scenario in ("account_takeover","rapid_upi_burst","impossible_travel","fraud_ring"):
        p=attack_payload(scenario,seed=42)
        rows=p if isinstance(p,list) else [p]
        for r in rows[:6]: samples.append((dict(r),1,scenario))
    tp=tn=fp=fn=0; lat=[]; explainable=0
    for tx,label,_ in samples:
        start=time.perf_counter(); out=score_transaction(tx,explain=False,use_cache=False); lat.append((time.perf_counter()-start)*1000)
        pred=1 if int(out['score'])>=31 else 0
        if pred and label: tp+=1
        elif pred and not label: fp+=1
        elif not pred and label: fn+=1
        else: tn+=1
        if out.get('reason') and (out.get('rule_contributors') is not None): explainable+=1
    n=max(len(samples),1)
    precision=tp/max(tp+fp,1); recall=tp/max(tp+fn,1)
    return {
        "benchmark":"synthetic_red_team_v1","samples":len(samples),"threshold":31,
        "accuracy":round((tp+tn)/n,4),"precision":round(precision,4),"recall":round(recall,4),
        "false_positive_rate":round(fp/max(fp+tn,1),4),"false_negative_rate":round(fn/max(fn+tp,1),4),
        "confusion":{"tp":tp,"tn":tn,"fp":fp,"fn":fn},
        "latency_ms":{"avg":round(mean(lat),2),"max":round(max(lat),2)},
        "explainability_coverage":round(explainable/n,4),
        "hallucination_control":"Deterministic benchmark uses structured scoring only; Copilot is separately evidence-grounded.",
        "financial_consistency":"Amounts and risk components are numeric and bounded before fusion.",
        "safety":"Risk is advisory; external reporting remains human-gated.",
        "ai_cost_usd":0.0,
        "note":"This is an internal synthetic red-team benchmark, not a production validation study."
    }
