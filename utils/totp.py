"""Minimal RFC 6238 TOTP helpers for FinTrust privileged-account MFA.

Uses only Python's standard library so deployments don't gain another runtime
package dependency. Compatible with Google Authenticator, Microsoft
Authenticator, Authy, 1Password, etc.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote


def generate_secret() -> str:
    """Return a 160-bit Base32 secret suitable for TOTP authenticator apps."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _decode_secret(secret: str) -> bytes:
    clean = "".join((secret or "").strip().upper().split())
    padding = "=" * ((8 - len(clean) % 8) % 8)
    return base64.b32decode(clean + padding, casefold=True)


def code_at(secret: str, for_time: int | float | None = None, *, step: int = 30, digits: int = 6) -> str:
    timestamp = int(time.time() if for_time is None else for_time)
    counter = timestamp // step
    key = _decode_secret(secret)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10 ** digits)).zfill(digits)


def verify_code(secret: str, code: str, *, valid_window: int = 1) -> bool:
    candidate = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(candidate) != 6:
        return False
    now = int(time.time())
    for drift in range(-valid_window, valid_window + 1):
        expected = code_at(secret, now + (drift * 30))
        if hmac.compare_digest(expected, candidate):
            return True
    return False


def provisioning_uri(secret: str, account_name: str, *, issuer: str = "FinTrust") -> str:
    label = quote(f"{issuer}:{account_name}", safe="")
    issuer_q = quote(issuer, safe="")
    return f"otpauth://totp/{label}?secret={secret}&issuer={issuer_q}&algorithm=SHA1&digits=6&period=30"
