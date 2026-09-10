"""Lightweight production guardrails for FinTrust/FraudShield.

No external dependency is required. For multi-instance deployments, replace the
in-memory limiter/metrics store with Redis/OpenTelemetry while keeping the same
HTTP contract.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import defaultdict, deque
from flask import g, jsonify, request

_lock = threading.Lock()
_windows: dict[str, deque[float]] = defaultdict(deque)
_metrics = {
    "requests_total": 0,
    "errors_total": 0,
    "latency_ms_sum": 0.0,
    "fraud_requests_total": 0,
    "rate_limited_total": 0,
}


def _client_key() -> str:
    # Do not trust arbitrary X-Forwarded-For unless the deployment proxy is
    # configured to sanitize it. Remote address is the conservative default.
    return request.remote_addr or "unknown"


def _limit_for_path() -> tuple[int, int]:
    if request.path.startswith("/api/fraudshield/webhooks/"):
        return int(os.getenv("FRAUD_WEBHOOK_RATE_LIMIT", "120")), 60
    if request.path.startswith("/lender/fraudshield"):
        return int(os.getenv("FRAUD_UI_RATE_LIMIT", "240")), 60
    return int(os.getenv("GLOBAL_RATE_LIMIT", "600")), 60


def _allow(key: str, limit: int, period: int) -> bool:
    now = time.monotonic()
    cutoff = now - period
    with _lock:
        q = _windows[key]
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def install_runtime_guard(app):
    log = logging.getLogger("fintrust.http")

    @app.before_request
    def _before():
        g.request_started = time.perf_counter()
        g.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:20]
        limit, period = _limit_for_path()
        limiter_key = f"{_client_key()}:{request.path}:{request.method}"
        if not _allow(limiter_key, limit, period):
            with _lock:
                _metrics["rate_limited_total"] += 1
            response = jsonify({
                "status": "error",
                "error": "rate_limited",
                "message": "Too many requests. Please retry shortly.",
                "request_id": g.request_id,
            })
            response.status_code = 429
            response.headers["Retry-After"] = "60"
            return response

    @app.after_request
    def _after(response):
        elapsed = (time.perf_counter() - getattr(g, "request_started", time.perf_counter())) * 1000
        with _lock:
            _metrics["requests_total"] += 1
            _metrics["latency_ms_sum"] += elapsed
            if request.path.startswith("/lender/fraudshield") or request.path.startswith("/api/fraudshield"):
                _metrics["fraud_requests_total"] += 1
            if response.status_code >= 500:
                _metrics["errors_total"] += 1
        response.headers["X-Request-ID"] = getattr(g, "request_id", "")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(self)"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline' https:; "
            "script-src 'self' 'unsafe-inline' https:; connect-src 'self' https:; frame-ancestors 'none'"
        )
        if os.getenv("FORCE_HSTS", "0") == "1":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        log.info(json.dumps({
            "event": "http_request", "request_id": getattr(g, "request_id", None),
            "method": request.method, "path": request.path, "status": response.status_code,
            "latency_ms": round(elapsed, 2),
        }))
        return response

    @app.errorhandler(500)
    def _internal_error(exc):
        app.logger.exception("Unhandled request error request_id=%s", getattr(g, "request_id", None))
        return jsonify({
            "status": "error", "message": "Internal service error",
            "request_id": getattr(g, "request_id", None),
        }), 500


def metrics_snapshot() -> dict:
    with _lock:
        m = dict(_metrics)
    total = max(int(m["requests_total"]), 1)
    m["avg_latency_ms"] = round(float(m["latency_ms_sum"]) / total, 2)
    return m
