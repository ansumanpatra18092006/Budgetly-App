# routes/admin.py

"""
Institution-admin workspace: page routes + the minimal provisioning
API needed to create/promote lender (analyst) accounts.

Scope for this phase (per spec): admin login/workspace, list/search
provisioned lenders, provision a new lender, explicitly promote an
existing account to lender, and basic active/disabled status for
lenders. Nothing else — no analytics, no credit-risk logic, no
multi-tenancy.

Registration (add to app.py, alongside the other blueprints):

    from routes.admin import admin_bp
    app.register_blueprint(admin_bp)

This module never creates a session or checks a password itself —
that's all in auth.py (POST /admin/do-login), reusing the same users
table and bcrypt hashing as every other login. This module only
guards pages/APIs with @admin_required and performs the actual
provisioning/promotion writes.
"""

import secrets
import string
from datetime import datetime, timedelta, timezone

import bcrypt
from flask import Blueprint, render_template, request, jsonify, session

from utils.db import get_db
from utils.decorators import admin_required
from routes.auth import send_invite_email, send_lender_invitation_email, APP_BASE_URL

admin_bp = Blueprint("admin", __name__)

# ------------------------------------------------------------------
# Invitation token (Phase A)
# ------------------------------------------------------------------
# Separate from the existing reset_token/temp-password flow above —
# this is a distinct, purpose-built token for the future lender
# activation flow (not implemented yet). Phase A only generates and
# stores it; nothing reads/consumes it yet.
INVITATION_TOKEN_TTL = timedelta(days=7)


def _now_utc():
    """Timezone-aware 'now', in UTC. Used everywhere the invitation
    flow previously used datetime.utcnow() (which is naive), so that
    invitation_expiry is always created/compared consistently."""
    return datetime.now(timezone.utc)


def _as_aware_utc(dt):
    """Normalizes a datetime to timezone-aware UTC. Handles both the
    naive values older rows may still have and the aware values the
    DB driver can return for timestamptz columns, so the comparison
    in _invitation_status never mixes naive and aware datetimes."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# Guards against re-running the ALTER TABLE on every request. Flipped
# to True after the first successful check in this process.
_invitation_schema_ready = False


def _ensure_invitation_schema(conn):
    """
    Idempotently adds the columns this phase needs to the existing
    users table (per spec: 'use the existing users table if
    practical'). Safe to call repeatedly — IF NOT EXISTS guards make
    it a no-op after the first run. This belongs in a proper
    migration (utils/db_migrate.py) long-term; it lives here for now
    since that file wasn't in scope for this change.
    """
    global _invitation_schema_ready
    if _invitation_schema_ready:
        return
    conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS invitation_token text")
    conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS invitation_expiry timestamp without time zone")
    conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS invitation_used boolean NOT NULL DEFAULT false")
    # Phase D: records the outcome of the most recent invitation email
    # attempt ('sent' | 'dev_fallback' | 'failed' | NULL), purely for
    # display in the admin table — never used for access control.
    conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS invitation_email_status text")
    # Partial unique index: enforces token uniqueness while allowing
    # unlimited NULLs (accounts with no active invitation).
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_invitation_token "
        "ON users (invitation_token) WHERE invitation_token IS NOT NULL"
    )
    _invitation_schema_ready = True


def _generate_invitation_token():
    """
    Cryptographically secure, single-purpose invitation token.
    - secrets.token_urlsafe uses os.urandom under the hood (CSPRNG).
    - 32 bytes -> 256 bits of entropy, URL-safe alphabet.
    - Never derived from email/name/id/password — pure randomness.
    """
    return secrets.token_urlsafe(32)


def _invitation_status(u):
    """
    Derives the display-only invitation state directly from the
    backend fields written by provisioning/activation/resend — never
    inferred from unrelated columns (e.g. this does NOT guess
    activation from login history or created_at).

    One of: "disabled" | "active" | "expired" | "failed" | "pending" | "none"
      disabled -> account status is 'disabled' (takes priority; a
                  disabled account's invitation state isn't actionable)
      active   -> invitation_used = true (lender has activated and set
                  their own password)
      none     -> no invitation was ever generated for this account
                  (e.g. promoted from an existing account, which
                  already had a password and never needed one)
      expired  -> invitation_expiry has passed and it's still unused
      failed   -> the most recent invitation email attempt failed and
                  the token hasn't expired or been used
      pending  -> a valid, unused, unexpired invitation exists
    """
    if (u.get("status") or "active") == "disabled":
        return "disabled"
    if u.get("invitation_used"):
        return "active"
    if not u.get("has_invitation"):
        return "none"
    expiry = _as_aware_utc(u.get("invitation_expiry"))
    if not expiry or expiry <= _now_utc():
        return "expired"
    if u.get("invitation_email_status") == "failed":
        return "failed"
    return "pending"


# ------------------------------------------------------------------
# Pages
# ------------------------------------------------------------------

@admin_bp.route("/admin/login", methods=["GET"])
def admin_login_page():
    """
    Public page — the institution-admin sign-in screen. There is no
    public admin signup anywhere; this page only renders a login form
    that posts to POST /admin/do-login (auth.py), which is the only
    place credentials/role are actually checked.
    """
    return render_template("admin_login.html")


@admin_bp.route("/admin/workspace", methods=["GET"])
@admin_required
def admin_workspace_page():
    """Institution admin workspace — provision/manage lender accounts."""
    return render_template("admin_workspace.html")


# ------------------------------------------------------------------
# API
# ------------------------------------------------------------------

def _generate_temp_password():
    # 16 chars, mixed alphabet — plenty of entropy for a short-lived
    # credential that's immediately superseded by a self-chosen
    # password via the reset-password link.
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(16))


def _serialize_user(u):
    return {
        "id": u["id"],
        "name": u["name"],
        "email": u["email"],
        "role": u["role"],
        "status": u.get("status") or "active",
        "invitation_status": _invitation_status(u),
        "created_at": u["created_at"].isoformat() if u.get("created_at") else None,
    }


@admin_bp.route("/admin/api/lenders", methods=["GET"])
@admin_required
def list_lenders():
    """List provisioned lender accounts, optionally filtered by a
    case-insensitive substring match on name/email."""
    q = (request.args.get("q") or "").strip()

    conn = get_db()
    try:
        _ensure_invitation_schema(conn)
        if q:
            rows = conn.execute(
                """
                SELECT id, name, email, role, status, created_at,
                       invitation_used, invitation_expiry, invitation_email_status,
                       (invitation_token IS NOT NULL) AS has_invitation
                FROM users
                WHERE role = 'lender'
                  AND (name ILIKE %s OR email ILIKE %s)
                ORDER BY created_at DESC
                """,
                (f"%{q}%", f"%{q}%"),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, name, email, role, status, created_at,
                       invitation_used, invitation_expiry, invitation_email_status,
                       (invitation_token IS NOT NULL) AS has_invitation
                FROM users
                WHERE role = 'lender'
                ORDER BY created_at DESC
                """
            ).fetchall()
    finally:
        conn.close()

    return jsonify({"success": True, "data": [_serialize_user(r) for r in rows]})


@admin_bp.route("/admin/api/search-user", methods=["GET"])
@admin_required
def search_user():
    """
    Exact-email lookup used by the "promote existing user" workflow.
    Returns the account's current role so the admin can see what
    they're about to change before confirming.
    """
    email = (request.args.get("email") or "").strip()
    if not email:
        return jsonify({"success": False, "message": "Email is required."}), 400

    conn = get_db()
    try:
        user = conn.execute(
            "SELECT id, name, email, role, status, created_at FROM users WHERE email=%s",
            (email,),
        ).fetchone()
    finally:
        conn.close()

    if not user:
        return jsonify({"success": False, "message": "No account found with that email."}), 404

    return jsonify({"success": True, "data": _serialize_user(user)})


@admin_bp.route("/admin/api/provision-lender", methods=["POST"])
@admin_required
def provision_lender():
    """
    Creates a brand-new lender account.

    - role is hardcoded to 'lender' here — the admin UI has no field
      that lets it choose a different role, and even if a client sent
      one in the JSON body it would be ignored (never read below).
    - password is a random temp credential, bcrypt-hashed before
      storage (never stored/logged in plaintext) — same hashing
      utils.db/bcrypt pattern used everywhere else in this app.
    - a reset_token is also set, reusing the exact same column and
      /reset-password flow that "forgot password" already uses, so
      the lender sets their own real password via a secure link
      rather than the admin having to communicate a permanent one.
    - if email delivery isn't configured (BREVO_API_KEY unset), this
      is a local/demo environment: the temp password and invite link
      are returned in the API response, clearly labeled as a
      development-only fallback, and also logged server-side — never
      done when real email delivery is available.
    """
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()

    if not name or not email:
        return jsonify({"success": False, "message": "Full name and email are required."}), 400

    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT id, role FROM users WHERE email=%s", (email,)
        ).fetchone()

        if existing:
            # Never silently repurpose an existing account. Tell the
            # admin exactly what's there and point them at promotion
            # instead, which requires its own explicit confirmation.
            return jsonify({
                "success": False,
                "message": (
                    f"An account with this email already exists (current role: "
                    f"{existing['role']}). Use 'Promote to Lender' instead if you "
                    f"intend to grant this existing account analyst access."
                ),
                "existing_role": existing["role"],
            }), 409

        temp_password = _generate_temp_password()
        hashed = bcrypt.hashpw(temp_password.encode(), bcrypt.gensalt()).decode()

        token = secrets.token_urlsafe(32)
        expiry = _now_utc() + timedelta(minutes=30)

        # New, independent invitation token (Phase A). Unrelated to
        # the reset_token above, which continues to power the
        # existing temp-password/reset-password flow unchanged.
        _ensure_invitation_schema(conn)
        invitation_token = _generate_invitation_token()
        invitation_expiry = _now_utc() + INVITATION_TOKEN_TTL

        row = conn.execute(
            """
            INSERT INTO users (
                name, email, password, role, status,
                reset_token, reset_expiry,
                invitation_token, invitation_expiry, invitation_used
            )
            VALUES (%s, %s, %s, 'lender', 'active', %s, %s, %s, %s, false)
            RETURNING id, name, email, role, status, created_at
            """,
            (name, email, hashed, token, expiry, invitation_token, invitation_expiry),
        ).fetchone()
        conn.commit()
    except Exception as e:
        conn.rollback()
        print("PROVISION LENDER ERROR:", e)
        return jsonify({"success": False, "message": "Could not create the account."}), 400
    finally:
        conn.close()

    # Legacy reset-password link — kept computed only for the dev_only
    # payload below (harmless/unused otherwise). The lender's actual
    # onboarding path is now the activation link emailed below; this
    # module no longer sends the old temp-password email for lenders,
    # so the lender does not receive two conflicting emails.
    invite_link = f"{APP_BASE_URL}/reset-password?token={token}"

    activation_url = f"{APP_BASE_URL}/lender/activate?token={invitation_token}"
    print(f"Invitation created: {activation_url}")

    ttl_display = f"{INVITATION_TOKEN_TTL.days} days"
    invitation_result = send_lender_invitation_email(email, activation_url, ttl_display=ttl_display)

    import os
    dev_mode = not bool(os.getenv("BREVO_API_KEY"))

    # Only ever tell the admin the invitation was "sent" when Brevo
    # actually accepted the send. A failed send never pretends to
    # have succeeded, and never rolls back the already-created
    # account or invalidates the still-usable invitation token.
    if invitation_result["mode"] == "sent":
        email_status = "sent"
        status_message = "Invitation sent."
    elif invitation_result["mode"] == "dev_fallback":
        email_status = "dev_fallback"
        status_message = (
            "Email delivery isn't configured, so the activation link is shown "
            "below for this development/demo environment only."
        )
    else:
        email_status = "failed"
        status_message = (
            "The invitation email could not be delivered. The account and its "
            "invitation link are still valid — you can retry sending the "
            "invitation later."
        )

    response = {
        "success": True,
        "message": "Lender account provisioned. " + status_message,
        "data": _serialize_user(row),
        "email_status": email_status,  # "sent" | "dev_fallback" | "failed"
    }

    # Persist the outcome so the "Invitation Status" column reflects it
    # on the next page load, not just in this immediate response.
    status_conn = get_db()
    try:
        status_conn.execute(
            "UPDATE users SET invitation_email_status=%s WHERE id=%s",
            (email_status, row["id"]),
        )
        status_conn.commit()
    except Exception as e:
        status_conn.rollback()
        print("PROVISION LENDER: could not persist invitation_email_status:", e)
    finally:
        status_conn.close()

    if dev_mode:
        response["dev_only"] = {
            "invite_link": invite_link,
            "temp_password": temp_password,
            "invitation_activation_url": activation_url,
            "invitation_expires_at": invitation_expiry.isoformat(),
        }
    elif email_status == "failed":
        # Brevo IS configured but this particular send failed. Surface
        # the activation link (not a password) so the admin has a way
        # to get it to the lender manually while retry/resend isn't
        # built yet — never shown when the send actually succeeded.
        response["activation_url"] = activation_url

    return jsonify(response)


@admin_bp.route("/admin/api/resend-invitation", methods=["POST"])
@admin_required
def resend_invitation():
    """
    Re-invites a lender who hasn't activated yet (pending, expired, or
    a previously-failed send). Generates a brand-new invitation token
    and expiry, overwriting the old one in place — since a user has at
    most one invitation_token column value, the previous invitation is
    invalidated the instant the new one is written; there is no
    separate "invalidate" step needed. Never creates a new account,
    never exposes a password, and never changes role or the account's
    active/disabled status.
    """
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")

    if not user_id:
        return jsonify({"success": False, "message": "A user_id is required."}), 400

    conn = get_db()
    try:
        _ensure_invitation_schema(conn)
        user = conn.execute(
            "SELECT id, name, email, role, status, invitation_used FROM users WHERE id=%s",
            (user_id,),
        ).fetchone()

        if not user or user["role"] != "lender":
            return jsonify({"success": False, "message": "Lender account not found."}), 404
        if user.get("status") == "disabled":
            return jsonify({"success": False, "message": "Cannot resend an invitation for a disabled account."}), 400
        if user.get("invitation_used"):
            return jsonify({"success": False, "message": "This account is already activated."}), 400

        new_token = _generate_invitation_token()
        new_expiry = _now_utc() + INVITATION_TOKEN_TTL

        conn.execute(
            """
            UPDATE users
            SET invitation_token=%s,
                invitation_expiry=%s,
                invitation_used=false,
                invitation_email_status=NULL
            WHERE id=%s
            """,
            (new_token, new_expiry, user_id),
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print("RESEND INVITATION ERROR:", e)
        return jsonify({"success": False, "message": "Could not resend the invitation."}), 400
    finally:
        conn.close()

    activation_url = f"{APP_BASE_URL}/lender/activate?token={new_token}"
    print(f"Invitation re-created: {activation_url}")

    ttl_display = f"{INVITATION_TOKEN_TTL.days} days"
    invitation_result = send_lender_invitation_email(user["email"], activation_url, ttl_display=ttl_display)

    import os
    dev_mode = not bool(os.getenv("BREVO_API_KEY"))

    if invitation_result["mode"] == "sent":
        email_status = "sent"
        status_message = "Invitation resent."
    elif invitation_result["mode"] == "dev_fallback":
        email_status = "dev_fallback"
        status_message = (
            "Email delivery isn't configured, so the activation link is shown "
            "below for this development/demo environment only."
        )
    else:
        email_status = "failed"
        status_message = (
            "The invitation email could not be delivered. The new invitation "
            "link is still valid — you can try resending again later."
        )

    status_conn = get_db()
    try:
        status_conn.execute(
            "UPDATE users SET invitation_email_status=%s WHERE id=%s",
            (email_status, user_id),
        )
        status_conn.commit()
    except Exception as e:
        status_conn.rollback()
        print("RESEND INVITATION: could not persist invitation_email_status:", e)
    finally:
        status_conn.close()

    response = {"success": True, "message": status_message, "email_status": email_status}
    if dev_mode:
        response["dev_only"] = {
            "invitation_activation_url": activation_url,
            "invitation_expires_at": new_expiry.isoformat(),
        }
    elif email_status == "failed":
        response["activation_url"] = activation_url

    return jsonify(response)


@admin_bp.route("/admin/api/promote-lender", methods=["POST"])
@admin_required
def promote_lender():
    """
    Promotes an EXISTING account to role='lender'. Requires an
    explicit confirm=true in the body — mirrors the UI's confirmation
    dialog ("Promote this account to lender access?"). Never happens
    as a side effect of anything else (e.g. duplicate-email handling
    in provision_lender deliberately does NOT call this).
    """
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    confirm = data.get("confirm") is True

    if not email:
        return jsonify({"success": False, "message": "Email is required."}), 400
    if not confirm:
        return jsonify({"success": False, "message": "Confirmation is required to promote an account."}), 400

    conn = get_db()
    try:
        user = conn.execute(
            "SELECT id, role FROM users WHERE email=%s", (email,)
        ).fetchone()

        if not user:
            return jsonify({"success": False, "message": "No account found with that email."}), 404

        if user["role"] == "admin":
            # Don't let admin-workspace actions touch admin accounts —
            # role changes for admins are a manual/DB-level action only,
            # same as initial admin provisioning.
            return jsonify({"success": False, "message": "Admin accounts cannot be modified from this workspace."}), 400

        if user["role"] == "lender":
            return jsonify({"success": False, "message": "This account is already a lender."}), 400

        row = conn.execute(
            """
            UPDATE users SET role='lender'
            WHERE id=%s
            RETURNING id, name, email, role, status, created_at
            """,
            (user["id"],),
        ).fetchone()
        conn.commit()
    except Exception as e:
        conn.rollback()
        print("PROMOTE LENDER ERROR:", e)
        return jsonify({"success": False, "message": "Could not promote this account."}), 400
    finally:
        conn.close()

    return jsonify({"success": True, "message": "Account promoted to lender.", "data": _serialize_user(row)})


@admin_bp.route("/admin/api/toggle-status", methods=["POST"])
@admin_required
def toggle_status():
    """
    Enable/disable a provisioned lender's access. Deliberately scoped
    to role='lender' only in this phase — an admin cannot disable
    another admin or a consumer from this workspace.
    """
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    new_status = data.get("status")

    if not user_id or new_status not in ("active", "disabled"):
        return jsonify({"success": False, "message": "A valid user_id and status ('active'|'disabled') are required."}), 400

    conn = get_db()
    try:
        user = conn.execute("SELECT id, role FROM users WHERE id=%s", (user_id,)).fetchone()
        if not user:
            return jsonify({"success": False, "message": "User not found."}), 404
        if user["role"] != "lender":
            return jsonify({"success": False, "message": "Only lender accounts can be managed here."}), 400

        row = conn.execute(
            """
            UPDATE users SET status=%s
            WHERE id=%s
            RETURNING id, name, email, role, status, created_at
            """,
            (new_status, user_id),
        ).fetchone()
        conn.commit()
    except Exception as e:
        conn.rollback()
        print("TOGGLE STATUS ERROR:", e)
        return jsonify({"success": False, "message": "Could not update status."}), 400
    finally:
        conn.close()

    return jsonify({"success": True, "data": _serialize_user(row)})