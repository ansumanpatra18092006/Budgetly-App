"""FraudShield advanced intelligence services.

This module is deliberately independent from consumer/lending transactions.
It operates only on the institution-owned FraudShield stream.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from math import log1p
import hashlib
import json
import random


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def _location_distance(a, b):
    if not a or not b or a == b:
        return 0.0
    # Hackathon/demo geodesic proxy when only city labels are available.
    distances = {
        tuple(sorted(("Bhubaneswar", "Kolkata"))): 385,
        tuple(sorted(("Bhubaneswar", "Hyderabad"))): 1050,
        tuple(sorted(("Bhubaneswar", "Mumbai"))): 1370,
        tuple(sorted(("Bhubaneswar", "New Delhi"))): 1450,
        tuple(sorted(("Mumbai", "New Delhi"))): 1150,
        tuple(sorted(("Hyderabad", "Kolkata"))): 1180,
    }
    return float(distances.get(tuple(sorted((str(a), str(b)))), 250))


def behavior_profile(rows):
    rows = list(rows or [])
    if not rows:
        return {"account": None, "samples": 0}
    amounts = [_safe_float(r.get("amount")) for r in rows]
    hours = [int(r.get("transaction_hour") or 12) for r in rows]
    locations = Counter(str(r.get("location") or "Unknown") for r in rows)
    merchants = Counter(str(r.get("merchant") or "Unknown") for r in rows)
    avg = sum(amounts) / max(1, len(amounts))
    med = sorted(amounts)[len(amounts)//2]
    return {
        "account": rows[0].get("account_ref"),
        "samples": len(rows),
        "typical_amount": round(med, 2),
        "average_amount": round(avg, 2),
        "amount_range": [round(min(amounts),2), round(max(amounts),2)],
        "normal_hours": sorted(hours),
        "peak_hour": Counter(hours).most_common(1)[0][0],
        "common_locations": [x[0] for x in locations.most_common(3)],
        "common_merchants": [x[0] for x in merchants.most_common(5)],
        "avg_velocity_1h": round(sum(int(r.get("txn_count_1h") or 0) for r in rows)/len(rows),2),
        "new_device_rate": round(sum(bool(r.get("is_new_device")) for r in rows)/len(rows)*100,1),
        "risk_baseline": round(sum(_safe_float(r.get("network_risk")) for r in rows)/len(rows)*100,1),
    }


def impossible_travel(txn, previous=None):
    previous = previous or {}
    prev_location = txn.get("previous_location") or previous.get("location")
    current_location = txn.get("location")
    prev_ts = txn.get("previous_transaction_timestamp") or previous.get("timestamp")
    current_ts = txn.get("transaction_timestamp") or txn.get("timestamp")
    if not prev_location or not current_location or not prev_ts or not current_ts:
        return {"detected": False, "distance_km": 0, "minutes": None, "message": "Insufficient location/time history"}
    try:
        a = datetime.fromisoformat(str(prev_ts).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(current_ts).replace("Z", "+00:00"))
        minutes = max(0.0, (b-a).total_seconds()/60)
    except Exception:
        return {"detected": False, "distance_km": 0, "minutes": None, "message": "Invalid timestamps"}
    distance = _safe_float(txn.get("location_distance_km")) or _location_distance(prev_location, current_location)
    # 80 km/h is a conservative travel-speed proxy for a financial-event alert.
    impossible = minutes > 0 and distance > (minutes/60.0)*80.0 and distance >= 200
    return {
        "detected": bool(impossible),
        "previous_location": prev_location,
        "current_location": current_location,
        "distance_km": round(distance, 1),
        "minutes": round(minutes, 1),
        "message": "Travel is not physically plausible for the observed interval" if impossible else "Movement is plausible",
    }


def risk_triage(score):
    score = int(score or 0)
    if score <= 30:
        return {"decision": "MONITOR", "priority": "LOW", "color": "green"}
    if score <= 60:
        return {"decision": "MONITOR", "priority": "MEDIUM", "color": "amber"}
    if score <= 80:
        return {"decision": "HUMAN REVIEW", "priority": "HIGH", "color": "orange"}
    return {"decision": "INVESTIGATE", "priority": "CRITICAL", "color": "red"}


def graph_snapshot(rows, extra_edges=None):
    rows = [dict(r) for r in rows or []]
    nodes = {}
    edges = []
    by_merchant = defaultdict(list)
    by_device = defaultdict(list)
    by_beneficiary = defaultdict(list)
    for r in rows:
        a = r.get("account_ref") or "UNKNOWN"
        nodes.setdefault(a, {"id": a, "type": "account", "risk": 0})
        nodes[a]["risk"] = max(nodes[a]["risk"], int(r.get("risk_score") or 0))
        merchant = r.get("merchant") or "Unknown Merchant"
        m_id = f"merchant:{merchant}"
        nodes.setdefault(m_id, {"id": m_id, "label": merchant, "type": "merchant", "risk": round(_safe_float(r.get("merchant_risk"))*100)})
        by_merchant[merchant].append(a)
        if r.get("device_id"):
            d_id = f"device:{r['device_id']}"
            nodes.setdefault(d_id, {"id": d_id, "label": r['device_id'], "type": "device", "risk": 0})
            by_device[r['device_id']].append(a)
            edges.append({"source": a, "target": d_id, "type": "USES_DEVICE", "weight": 0.55})
        edges.append({"source": a, "target": m_id, "type": "PAYS_MERCHANT", "weight": 0.35})
        if r.get("beneficiary_ref"):
            b_id = f"beneficiary:{r['beneficiary_ref']}"
            nodes.setdefault(b_id, {"id": b_id, "label": r['beneficiary_ref'], "type": "beneficiary", "risk": 0})
            by_beneficiary[r['beneficiary_ref']].append(a)
            edges.append({"source": a, "target": b_id, "type": "PAYS_BENEFICIARY", "weight": 0.75})
    for merchant, accounts in by_merchant.items():
        uniq = list(dict.fromkeys(accounts))
        if len(uniq) >= 2:
            for i in range(len(uniq)):
                for j in range(i+1, len(uniq)):
                    edges.append({"source": uniq[i], "target": uniq[j], "type": "SHARED_MERCHANT", "weight": min(0.8, 0.2 + 0.1*len(uniq))})
    for device, accounts in by_device.items():
        uniq = list(dict.fromkeys(accounts))
        if len(uniq) >= 2:
            for i in range(len(uniq)):
                for j in range(i+1, len(uniq)):
                    edges.append({"source": uniq[i], "target": uniq[j], "type": "SHARED_DEVICE", "weight": 0.95})
    for edge in extra_edges or []:
        edges.append(dict(edge))
        for n in (edge.get("source"), edge.get("target")):
            if n and n not in nodes:
                nodes[n] = {"id": n, "type": "account", "risk": 90}
    # Unique edges and connected components.
    seen = set(); dedup=[]
    for e in edges:
        k=(e.get("source"),e.get("target"),e.get("type"))
        if k not in seen:
            seen.add(k); dedup.append(e)
    adj=defaultdict(set)
    for e in dedup:
        a,b=e.get("source"),e.get("target")
        if a and b:
            adj[a].add(b); adj[b].add(a)
    visited=set(); components=[]
    for n in nodes:
        if n in visited: continue
        stack=[n]; visited.add(n); comp=[]
        while stack:
            x=stack.pop(); comp.append(x)
            for y in adj[x]:
                if y not in visited:
                    visited.add(y); stack.append(y)
        if len(comp)>=3: components.append(comp)
    ring_scores=[]
    for comp in components:
        account_nodes=[n for n in comp if nodes[n].get("type")=="account"]
        edge_count=sum(1 for e in dedup if e.get("source") in comp and e.get("target") in comp)
        shared_device=sum(1 for e in dedup if e.get("type")=="SHARED_DEVICE" and e.get("source") in comp)
        score=min(100, int(35+12*len(account_nodes)+15*shared_device+5*edge_count))
        ring_scores.append({"accounts":account_nodes,"network_risk":score,"edge_count":edge_count,"reason":"Dense shared-device/merchant relationship"})
    return {"nodes":list(nodes.values()),"edges":dedup,"communities":ring_scores}


def attack_payload(scenario="account_takeover", seed=42):
    rng=random.Random(seed)
    base={
        "account_takeover": [
            {"account_ref":"AC-ATTACK-01","merchant":"Unknown Merchant","amount":48500,"location":"Mumbai","is_new_device":True,"merchant_risk":0.92,"txn_count_1h":7,"txn_count_24h":18,"location_distance_km":1120,"account_age_days":24,"relationship_count":7,"merchant_seen_before":False,"device_trust_score":0.06,"time_deviation_hours":7.5,"amount_to_account_median":14.2,"network_risk":0.91,"failed_auth_count":4,"channel":"UPI"},
            {"account_ref":"AC-ATTACK-01","merchant":"Gift Card Shop","amount":19600,"location":"Mumbai","is_new_device":True,"merchant_risk":0.87,"txn_count_1h":6,"txn_count_24h":16,"location_distance_km":1120,"account_age_days":24,"relationship_count":7,"merchant_seen_before":False,"device_trust_score":0.08,"time_deviation_hours":7.7,"amount_to_account_median":9.1,"network_risk":0.88,"failed_auth_count":3,"channel":"UPI"},
        ],
        "rapid_upi_burst": [
            {"account_ref":"AC-BURST-01","merchant":"UPI Wallet","amount":rng.randint(9000,16000),"location":"Bhubaneswar","is_new_device":False,"merchant_risk":0.62,"txn_count_1h":9,"txn_count_24h":14,"location_distance_km":8,"account_age_days":580,"relationship_count":2,"merchant_seen_before":True,"device_trust_score":0.92,"time_deviation_hours":0.6,"amount_to_account_median":3.8,"network_risk":0.42,"failed_auth_count":0,"channel":"UPI"} for _ in range(4)
        ],
        "impossible_travel": [
            {"account_ref":"AC-TRAVEL-01","merchant":"Online Electronics","amount":32000,"location":"Mumbai","previous_location":"Bhubaneswar","location_distance_km":1370,"is_new_device":False,"merchant_risk":0.55,"txn_count_1h":2,"txn_count_24h":5,"account_age_days":920,"relationship_count":2,"merchant_seen_before":True,"device_trust_score":0.91,"time_deviation_hours":8.5,"amount_to_account_median":4.2,"network_risk":0.31,"failed_auth_count":0,"channel":"CARD"}
        ],
        "merchant_fraud": [
            {"account_ref":"AC-MERCHANT-01","merchant":"Gift Card Shop","amount":27500,"location":"Hyderabad","is_new_device":False,"merchant_risk":0.94,"txn_count_1h":3,"txn_count_24h":7,"location_distance_km":40,"account_age_days":380,"relationship_count":4,"merchant_seen_before":False,"device_trust_score":0.71,"time_deviation_hours":3.6,"amount_to_account_median":5.9,"network_risk":0.67,"failed_auth_count":1,"channel":"CARD"}
        ],
        "fraud_ring": [
            {"account_ref":"AC-RING-01","merchant":"Cashout Hub","amount":42000,"location":"Kolkata","is_new_device":True,"merchant_risk":0.91,"txn_count_1h":3,"txn_count_24h":10,"location_distance_km":620,"account_age_days":72,"relationship_count":8,"merchant_seen_before":False,"device_trust_score":0.12,"time_deviation_hours":5.1,"amount_to_account_median":8.4,"network_risk":0.93,"failed_auth_count":1,"channel":"UPI","beneficiary_ref":"BEN-RING-1","device_id":"DEV-RING"},
            {"account_ref":"AC-RING-02","merchant":"Cashout Hub","amount":41500,"location":"Kolkata","is_new_device":False,"merchant_risk":0.91,"txn_count_1h":2,"txn_count_24h":8,"location_distance_km":610,"account_age_days":95,"relationship_count":8,"merchant_seen_before":False,"device_trust_score":0.22,"time_deviation_hours":5.3,"amount_to_account_median":7.9,"network_risk":0.93,"failed_auth_count":0,"channel":"UPI","beneficiary_ref":"BEN-RING-1","device_id":"DEV-RING"},
            {"account_ref":"AC-RING-03","merchant":"Cashout Hub","amount":40500,"location":"Kolkata","is_new_device":False,"merchant_risk":0.91,"txn_count_1h":2,"txn_count_24h":7,"location_distance_km":610,"account_age_days":110,"relationship_count":8,"merchant_seen_before":False,"device_trust_score":0.20,"time_deviation_hours":5.4,"amount_to_account_median":7.5,"network_risk":0.93,"failed_auth_count":0,"channel":"UPI","beneficiary_ref":"BEN-RING-1","device_id":"DEV-RING"},
            {"account_ref":"AC-RING-04","merchant":"Cashout Hub","amount":39800,"location":"Kolkata","is_new_device":False,"merchant_risk":0.91,"txn_count_1h":2,"txn_count_24h":6,"location_distance_km":600,"account_age_days":130,"relationship_count":8,"merchant_seen_before":False,"device_trust_score":0.18,"time_deviation_hours":5.2,"amount_to_account_median":7.3,"network_risk":0.93,"failed_auth_count":0,"channel":"UPI","beneficiary_ref":"BEN-RING-1","device_id":"DEV-RING"},
        ],
        "replay_attack": [
            {"account_ref":"AC-REPLAY-01","merchant":"Amazon","amount":5500,"location":"Bhubaneswar","is_new_device":False,"merchant_risk":0.12,"txn_count_1h":1,"txn_count_24h":3,"location_distance_km":2,"account_age_days":880,"relationship_count":1,"merchant_seen_before":True,"device_trust_score":0.96,"time_deviation_hours":0.4,"amount_to_account_median":1.1,"network_risk":0.04,"failed_auth_count":0,"channel":"UPI"}
        ],
    }
    return base.get(scenario, base["account_takeover"])


def simulator_stats(results):
    scores=[int(x.get("risk_score") or 0) for x in results]
    detected=sum(s>=61 for s in scores)
    return {
        "generated":len(results),
        "high_risk":sum(s>=81 for s in scores),
        "review":sum(61<=s<=80 for s in scores),
        "detected":detected,
        "detection_rate":round(detected/len(scores)*100,1) if scores else 0,
        "avg_risk":round(sum(scores)/len(scores),1) if scores else 0,
        "median_risk":sorted(scores)[len(scores)//2] if scores else 0,
    }


def event_fingerprint(payload):
    keys=(payload.get("transaction_ref") or "", payload.get("utr") or "", payload.get("account_ref") or "", payload.get("amount") or "", payload.get("timestamp") or payload.get("transaction_timestamp") or "")
    return hashlib.sha256("|".join(map(str, keys)).encode()).hexdigest()
