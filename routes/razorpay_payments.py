"""Razorpay Test/Live checkout integration for lender-grade verified transactions.

Security boundary:
- Razorpay key secret never leaves the Flask server.
- Flutter receives only the public key_id + Razorpay order_id.
- A mobile success callback alone never marks a transaction VERIFIED.
- FinTrust verifies Razorpay's HMAC signature and fetches the payment from
  Razorpay's API before creating lender-eligible financial evidence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import requests
from flask import Blueprint, jsonify, request, session

from utils.db import get_db
from utils.decorators import login_required

logger = logging.getLogger(__name__)
razorpay_payments_bp = Blueprint("razorpay_payments", __name__)

RAZORPAY_API = "https://api.razorpay.com/v1"


def _credentials():
    key_id = (os.environ.get("RAZORPAY_KEY_ID") or "").strip()
    key_secret = (os.environ.get("RAZORPAY_KEY_SECRET") or "").strip()
    if not key_id or not key_secret:
        return None, None
    return key_id, key_secret


def _amount_to_subunits(value) -> int:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Invalid payment amount")
    if amount <= 0:
        raise ValueError("Payment amount must be greater than zero")
    return int((amount * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _ensure_schema(conn):
    # Transaction provenance is safe to apply idempotently even if an older
    # deployment has not yet received the verified-underwriting migration.
    conn.execute(
        """
        ALTER TABLE public.transactions
            ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'completed',
            ADD COLUMN IF NOT EXISTS verification_status text NOT NULL DEFAULT 'UNVERIFIED',
            ADD COLUMN IF NOT EXISTS verification_source text,
            ADD COLUMN IF NOT EXISTS verified_at timestamp with time zone,
            ADD COLUMN IF NOT EXISTS verification_reference text
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS public.razorpay_payment_orders (
            id bigserial PRIMARY KEY,
            user_id bigint NOT NULL REFERENCES public.users(id),
            razorpay_order_id text NOT NULL UNIQUE,
            receipt text NOT NULL UNIQUE,
            amount_subunits bigint NOT NULL,
            currency text NOT NULL DEFAULT 'INR',
            description text NOT NULL,
            category text,
            transaction_date date NOT NULL,
            transaction_timestamp timestamp with time zone,
            state text NOT NULL DEFAULT 'CREATED',
            razorpay_payment_id text UNIQUE,
            transaction_id bigint REFERENCES public.transactions(id),
            created_at timestamp with time zone NOT NULL DEFAULT now(),
            updated_at timestamp with time zone NOT NULL DEFAULT now()
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_razorpay_orders_user_created
        ON public.razorpay_payment_orders(user_id, created_at DESC)
        """
    )


def _razorpay_request(method: str, path: str, *, json_body=None, timeout=12):
    key_id, key_secret = _credentials()
    if not key_id or not key_secret:
        raise RuntimeError("Razorpay is not configured on the FinTrust server")
    response = requests.request(
        method,
        f"{RAZORPAY_API}{path}",
        auth=(key_id, key_secret),
        json=json_body,
        timeout=timeout,
    )
    if response.status_code < 200 or response.status_code >= 300:
        try:
            detail = response.json()
        except Exception:
            detail = response.text[:500]
        raise RuntimeError(f"Razorpay API {response.status_code}: {detail}")
    return response.json()


def _verify_checkout_signature(order_id: str, payment_id: str, signature: str) -> bool:
    _, key_secret = _credentials()
    if not key_secret:
        return False
    message = f"{order_id}|{payment_id}".encode("utf-8")
    expected = hmac.new(key_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _insert_verified_transaction(conn, order_row, payment_id: str):
    # Idempotency: a verified Razorpay payment maps to exactly one FinTrust txn.
    existing = conn.execute(
        """
        SELECT id FROM public.transactions
        WHERE user_id=%s AND verification_reference=%s
        LIMIT 1
        """,
        (order_row["user_id"], payment_id),
    ).fetchone()
    if existing:
        return existing["id"]

    reference_id = f"RZP-{payment_id}"[:150]
    description = order_row["description"]
    amount_rupees = Decimal(order_row["amount_subunits"]) / Decimal(100)

    row = conn.execute(
        """
        INSERT INTO public.transactions (
            user_id, description, amount, type, category, date, status,
            transaction_timestamp, reference_id, source,
            verification_status, verification_source, verified_at,
            verification_reference
        )
        VALUES (
            %s,%s,%s,'expense',%s,%s,'completed',%s,%s,'RAZORPAY',
            'VERIFIED','RAZORPAY_API',now(),%s
        )
        RETURNING id
        """,
        (
            order_row["user_id"],
            description,
            amount_rupees,
            order_row["category"] or "Misc",
            order_row["transaction_date"],
            order_row["transaction_timestamp"] or datetime.now(timezone.utc),
            reference_id,
            payment_id,
        ),
    ).fetchone()
    if not row:
        raise RuntimeError("Verified transaction insert returned no row")
    return row["id"]


@razorpay_payments_bp.route("/api/payments/razorpay/config", methods=["GET"])
@login_required
def razorpay_config():
    key_id, key_secret = _credentials()
    return jsonify({
        "success": True,
        "configured": bool(key_id and key_secret),
        "mode": "test" if (key_id or "").startswith("rzp_test_") else "live" if key_id else "unconfigured",
    })


@razorpay_payments_bp.route("/api/payments/razorpay/order", methods=["POST"])
@login_required
def razorpay_create_order():
    key_id, key_secret = _credentials()
    if not key_id or not key_secret:
        return jsonify({
            "success": False,
            "message": "Verified Checkout is not configured on the server yet.",
        }), 503

    body = request.get_json(silent=True) or {}
    try:
        amount_subunits = _amount_to_subunits(body.get("amount"))
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400

    description = str(body.get("description") or "FinTrust payment").strip()[:200]
    category = str(body.get("category") or "Misc").strip()[:80]
    raw_date = str(body.get("date") or datetime.now(timezone.utc).date().isoformat())
    try:
        transaction_date = datetime.fromisoformat(raw_date).date()
    except ValueError:
        transaction_date = datetime.now(timezone.utc).date()

    transaction_timestamp = None
    raw_ts = body.get("transaction_timestamp")
    if raw_ts:
        try:
            transaction_timestamp = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
        except ValueError:
            transaction_timestamp = None

    user_id = session["user_id"]
    receipt = f"FT{user_id}{int(datetime.now(timezone.utc).timestamp() * 1000)}"[:40]

    try:
        order = _razorpay_request(
            "POST",
            "/orders",
            json_body={
                "amount": amount_subunits,
                "currency": "INR",
                "receipt": receipt,
                "notes": {
                    "fintrust_user_id": str(user_id),
                    "purpose": "verified_checkout",
                },
            },
        )
    except Exception:
        logger.exception("Razorpay order creation failed for user=%s", user_id)
        return jsonify({
            "success": False,
            "message": "Razorpay could not create the payment order.",
        }), 502

    conn = get_db()
    try:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO public.razorpay_payment_orders (
                user_id, razorpay_order_id, receipt, amount_subunits, currency,
                description, category, transaction_date, transaction_timestamp,
                state, updated_at
            )
            VALUES (%s,%s,%s,%s,'INR',%s,%s,%s,%s,'CREATED',now())
            """,
            (
                user_id,
                order["id"],
                receipt,
                amount_subunits,
                description,
                category,
                transaction_date,
                transaction_timestamp,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("Could not persist Razorpay order %s", order.get("id"))
        return jsonify({"success": False, "message": "Could not persist payment order."}), 500
    finally:
        conn.close()

    return jsonify({
        "success": True,
        "key_id": key_id,  # public checkout key only; never return key_secret
        "order_id": order["id"],
        "amount_subunits": amount_subunits,
        "currency": "INR",
        "receipt": receipt,
        "mode": "test" if key_id.startswith("rzp_test_") else "live",
    }), 201


@razorpay_payments_bp.route("/api/payments/razorpay/verify", methods=["POST"])
@login_required
def razorpay_verify_payment():
    body = request.get_json(silent=True) or {}
    payment_id = str(body.get("razorpay_payment_id") or "").strip()
    order_id = str(body.get("razorpay_order_id") or "").strip()
    signature = str(body.get("razorpay_signature") or "").strip()

    if not payment_id or not order_id or not signature:
        return jsonify({
            "success": False,
            "verified": False,
            "message": "Missing Razorpay verification fields.",
        }), 400

    if not _verify_checkout_signature(order_id, payment_id, signature):
        logger.warning("Rejected invalid Razorpay signature order=%s payment=%s", order_id, payment_id)
        return jsonify({
            "success": False,
            "verified": False,
            "status": "INVALID_SIGNATURE",
            "message": "Razorpay signature verification failed.",
        }), 400

    user_id = session["user_id"]
    conn = get_db()
    try:
        _ensure_schema(conn)
        order_row = conn.execute(
            """
            SELECT * FROM public.razorpay_payment_orders
            WHERE razorpay_order_id=%s AND user_id=%s
            FOR UPDATE
            """,
            (order_id, user_id),
        ).fetchone()
        if not order_row:
            conn.rollback()
            return jsonify({
                "success": False,
                "verified": False,
                "status": "ORDER_NOT_FOUND",
                "message": "This Razorpay order does not belong to the signed-in user.",
            }), 404

        if order_row["transaction_id"]:
            conn.commit()
            return jsonify({
                "success": True,
                "verified": True,
                "status": "VERIFIED",
                "transaction_id": order_row["transaction_id"],
                "payment_id": order_row["razorpay_payment_id"] or payment_id,
                "message": "Payment was already verified.",
            })

        # Server-to-server fetch is the authoritative payment state check.
        payment = _razorpay_request("GET", f"/payments/{payment_id}")
        if payment.get("order_id") != order_id:
            raise RuntimeError("Razorpay payment/order mismatch")
        if str(payment.get("currency") or "").upper() != "INR":
            raise RuntimeError("Unexpected Razorpay payment currency")
        if int(payment.get("amount") or 0) != int(order_row["amount_subunits"]):
            raise RuntimeError("Razorpay amount mismatch")

        status = str(payment.get("status") or "").lower()

        # Some accounts return an authorized payment before capture. Attempt a
        # server-side capture, then verify the returned state again.
        if status == "authorized":
            payment = _razorpay_request(
                "POST",
                f"/payments/{payment_id}/capture",
                json_body={
                    "amount": int(order_row["amount_subunits"]),
                    "currency": "INR",
                },
            )
            status = str(payment.get("status") or "").lower()

        if status != "captured":
            conn.execute(
                """
                UPDATE public.razorpay_payment_orders
                SET state=%s, razorpay_payment_id=%s, updated_at=now()
                WHERE id=%s
                """,
                (status.upper() or "UNKNOWN", payment_id, order_row["id"]),
            )
            conn.commit()
            return jsonify({
                "success": False,
                "verified": False,
                "status": status.upper() or "UNKNOWN",
                "message": f"Payment is {status or 'not captured'}; it is not lender-verified yet.",
            }), 409

        transaction_id = _insert_verified_transaction(conn, order_row, payment_id)
        conn.execute(
            """
            UPDATE public.razorpay_payment_orders
            SET state='VERIFIED', razorpay_payment_id=%s,
                transaction_id=%s, updated_at=now()
            WHERE id=%s
            """,
            (payment_id, transaction_id, order_row["id"]),
        )
        conn.commit()

        return jsonify({
            "success": True,
            "verified": True,
            "status": "VERIFIED",
            "transaction_id": transaction_id,
            "payment_id": payment_id,
            "verification_source": "RAZORPAY_API",
            "message": "Payment independently verified by Razorpay and saved as lender-eligible evidence.",
        })
    except Exception:
        conn.rollback()
        logger.exception("Razorpay verification failed order=%s payment=%s user=%s", order_id, payment_id, user_id)
        return jsonify({
            "success": False,
            "verified": False,
            "status": "VERIFICATION_ERROR",
            "message": "FinTrust could not independently verify this Razorpay payment.",
        }), 502
    finally:
        conn.close()


@razorpay_payments_bp.route("/api/payments/razorpay/webhook", methods=["POST"])
def razorpay_webhook():
    """Optional reconciliation webhook.

    Checkout verification already performs server-to-server validation. This
    endpoint additionally authenticates Razorpay webhooks and records final
    payment state for reconciliation without trusting arbitrary callers.
    """
    webhook_secret = (os.environ.get("RAZORPAY_WEBHOOK_SECRET") or "").strip()
    if not webhook_secret:
        return jsonify({"success": False, "message": "Webhook is not configured."}), 503

    raw = request.get_data(cache=True)
    supplied = (request.headers.get("X-Razorpay-Signature") or "").strip()
    expected = hmac.new(webhook_secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    if not supplied or not hmac.compare_digest(expected, supplied):
        return jsonify({"success": False, "message": "Invalid webhook signature."}), 401

    try:
        event = json.loads(raw.decode("utf-8"))
    except Exception:
        return jsonify({"success": False, "message": "Invalid webhook JSON."}), 400

    event_name = str(event.get("event") or "")
    payment_entity = (((event.get("payload") or {}).get("payment") or {}).get("entity") or {})
    order_id = payment_entity.get("order_id")
    payment_id = payment_entity.get("id")
    payment_status = str(payment_entity.get("status") or "").upper()

    # Keep webhook reconciliation deliberately conservative: it updates the
    # provider-order record, but creating lender-grade evidence still requires
    # the signed checkout + server fetch path above (or a future webhook flow
    # that performs the same order/amount verification).
    if order_id:
        conn = get_db()
        try:
            _ensure_schema(conn)
            conn.execute(
                """
                UPDATE public.razorpay_payment_orders
                SET state=%s,
                    razorpay_payment_id=COALESCE(%s, razorpay_payment_id),
                    updated_at=now()
                WHERE razorpay_order_id=%s AND transaction_id IS NULL
                """,
                (payment_status or event_name.upper()[:60], payment_id, order_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception("Razorpay webhook reconciliation failed event=%s", event_name)
            return jsonify({"success": False}), 500
        finally:
            conn.close()

    return jsonify({"success": True}), 200
