from functools import wraps
from flask import session, jsonify
from utils.db import get_db

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


def lender_required(f):
    """
    Guards lender/analyst-only routes.

    - not authenticated          -> 401 Unauthorized
    - authenticated, not lender  -> 403 Forbidden
    - authenticated lender, but the account has since been disabled
      by an admin (existing session predates the disable)
                                  -> 403 Forbidden, session cleared
    - authenticated, active lender -> allowed

    Role is read from the server-side session only (set at login time
    from the database record) — never from a client-supplied header,
    query param, or body field. Status is re-checked against the
    database on every request (not just cached in the session at
    login) specifically so a lender disabled mid-session loses access
    immediately, per Phase D — this is the smallest server-side check
    needed since the login endpoint alone can't catch a disable that
    happens after the session already exists.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Unauthorized"}), 401
        if session.get("role") != "lender":
            return jsonify({"error": "Forbidden"}), 403

        conn = get_db()
        try:
            user = conn.execute(
                "SELECT status FROM users WHERE id=%s", (session["user_id"],)
            ).fetchone()
        finally:
            conn.close()

        if not user or user.get("status") == "disabled":
            session.clear()
            return jsonify({"error": "Forbidden"}), 403

        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    """
    Guards admin/institution-admin-only routes.

    - not authenticated         -> 401 Unauthorized
    - authenticated, not admin  -> 403 Forbidden
    - authenticated admin       -> allowed

    Same pattern as lender_required: the role comes exclusively from
    the server-side session (populated at login time from the users
    table), never from anything the client sends. An admin session
    does NOT implicitly grant lender access, and vice versa — each
    workspace has its own decorator and each checks for its own role
    only.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Unauthorized"}), 401
        if session.get("role") != "admin":
            return jsonify({"error": "Forbidden"}), 403
        return f(*args, **kwargs)
    return wrapper