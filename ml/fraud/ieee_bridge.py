"""Bridge institution-owned FraudShield transactions into the IEEE-CIS model schema.

The friend's model was trained on IEEE-CIS features. The live/demo FraudShield stream
contains institution-level fields such as account, amount, device, location, velocity,
merchant and relationship signals. This adapter maps available fields into the trained
schema and creates deterministic compatibility values for IEEE-CIS aggregate fields.

IMPORTANT: compatibility fields are a demo/POC bridge, not claims that the live stream
contains the original IEEE-CIS semantics. Model validation metrics displayed by the UI
refer to the IEEE-CIS validation set, not this bridge.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Dict


def _hash_int(*parts: Any, mod: int, offset: int = 0) -> int:
    s = "|".join(str(p if p is not None else "") for p in parts)
    h = int(hashlib.sha256(s.encode("utf-8")).hexdigest()[:12], 16)
    return offset + (h % mod)


def _timestamp_dt(txn: Dict[str, Any]) -> int:
    raw = txn.get("transaction_timestamp") or txn.get("timestamp") or txn.get("date")
    if isinstance(raw, datetime):
        return int(raw.timestamp())
    if raw:
        try:
            return int(datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp())
        except Exception:
            pass
    return 0


def to_ieee_transaction(txn: Dict[str, Any]) -> Dict[str, Any]:
    amount = float(txn.get("amount") or 0.0)
    account = txn.get("account_ref") or "UNKNOWN"
    merchant = txn.get("merchant") or "Unknown Merchant"
    channel = str(txn.get("channel") or "UPI").upper()
    device = txn.get("device_id") or "unknown-device"
    location = txn.get("location") or "unknown-location"
    browser = txn.get("browser") or ""
    os_name = txn.get("os") or ""
    hour = int(txn.get("transaction_hour") or 0)
    velocity_1h = int(txn.get("txn_count_1h") or 0)
    velocity_24h = int(txn.get("txn_count_24h") or 0)
    rels = int(txn.get("relationship_count") or 0)
    dist = float(txn.get("location_distance_km") or 0.0)
    merchant_risk = float(txn.get("merchant_risk") or 0.0)
    trust = float(txn.get("device_trust_score") or 0.0)
    age = int(txn.get("account_age_days") or 0)
    time_dev = float(txn.get("time_deviation_hours") or 0.0)
    amount_ratio = float(txn.get("amount_to_account_median") or 1.0)
    network_risk = float(txn.get("network_risk") or 0.0)
    failed_auth = int(txn.get("failed_auth_count") or 0)
    seen = bool(txn.get("merchant_seen_before"))

    product = "C" if channel == "CARD" else ("R" if channel in {"NEFT", "RTGS", "IMPS", "TRANSFER"} else "W")
    device_type = "mobile" if ("android" in str(os_name).lower() or "ios" in str(os_name).lower() or "mobile" in str(browser).lower()) else "desktop"

    x: Dict[str, Any] = {
        "TransactionAmt": amount,
        "ProductCD": product,
        "TransactionDT": _timestamp_dt(txn),
        "card1": _hash_int(account, mod=100000, offset=1000),
        "card2": _hash_int(account, device, mod=500, offset=100),
        "card3": _hash_int(channel, mod=100, offset=100),
        "card4": "visa" if channel == "CARD" else "mastercard",
        "card5": _hash_int(merchant, mod=700, offset=100),
        "card6": "credit" if channel == "CARD" else "debit",
        "addr1": _hash_int(location, mod=500, offset=1),
        "addr2": _hash_int(location, "2", mod=90, offset=1),
        "dist1": dist,
        "dist2": max(0.0, dist * (0.35 if txn.get("previous_location") else 0.0)),
        "P_emaildomain": "unknown.com",
        "R_emaildomain": "unknown.com",
        "DeviceType": device_type,
        "DeviceInfo": device,
        # IEEE-inspired aggregates mapped from institution features.
        "C1": velocity_1h,
        "C2": velocity_24h,
        "C3": rels,
        "C4": failed_auth,
        "C5": int(not seen),
        "C6": int(bool(txn.get("is_new_device"))),
        "C7": int(round(network_risk * 10)),
        "C8": int(round(merchant_risk * 10)),
        "C9": int(age < 90),
        "C10": int(hour < 6 or hour >= 23),
        "C11": int(round(trust * 10)),
        "C12": int(round(max(0.0, amount_ratio) * 10)),
        "C13": int(round(time_dev * 10)),
        "C14": int(round(dist / 100.0)),
    }

    # D1-D15: temporal/account deviation compatibility signals.
    for i in range(1, 16):
        x[f"D{i}"] = float([
            amount_ratio, time_dev, dist / 100.0, age / 365.0, velocity_1h,
            velocity_24h, rels, merchant_risk, network_risk, trust,
            failed_auth, int(txn.get("is_new_device")), int(not seen), hour, amount / 1000.0
        ][i - 1])

    # V1-V144: deterministic feature-family bridge using the available context.
    base_vals = [
        amount, amount_ratio, velocity_1h, velocity_24h, dist,
        merchant_risk, trust, age, hour, rels, network_risk,
        failed_auth, time_dev, int(txn.get("is_new_device")), int(not seen),
    ]
    for i in range(1, 145):
        seed = base_vals[(i - 1) % len(base_vals)]
        scale = 1.0 + ((i % 9) * 0.07)
        x[f"V{i}"] = float(seed * scale)

    return x
