from flask import Blueprint, request, jsonify, session
from utils.db import get_db
import bcrypt
from utils.decorators import login_required
import secrets
from datetime import datetime, timedelta, timezone
from flask import render_template
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException
import os

auth_bp = Blueprint("auth", __name__)

APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:5000")

@auth_bp.route("/signup", methods=["POST"])
def signup():
    data = request.get_json()
    conn = get_db()
    hashed = bcrypt.hashpw(data["password"].encode(), bcrypt.gensalt()).decode()

    # SECURITY: role is never taken from client input. Every self-service
    # signup is a consumer account. Lender accounts are provisioned
    # separately by an admin (see routes/admin.py) and are never created
    # here. Any "role" field the client sends is ignored — it is never
    # read from `data` at all.
    try:
        conn.execute(
            "INSERT INTO users (name,email,password,role) VALUES (%s,%s,%s,%s)",
            (data["name"], data["email"], hashed, "consumer")
        )
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        print("SIGNUP ERROR:", e)
        return jsonify({"success": False, "error": str(e)}), 400
    finally:
        conn.close()


@auth_bp.route("/do-login", methods=["POST"])
def login():
    data = request.get_json()
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE email=%s",
        (data["email"],)
    ).fetchone()
    conn.close()

    if user and bcrypt.checkpw(data["password"].encode(), str(user["password"]).encode()):
        if user.get("status") == "disabled":
            return jsonify({"success": False, "message": "This account has been disabled."}), 403

        session.clear()
        session["user_id"] = user["id"]
        session["logged_in"] = True
        # Every user has a role now (existing rows default to 'consumer' via
        # migration). This does not gate anything in the consumer flow —
        # it only lets role-aware decorators (e.g. lender-only, admin-only
        # routes) recognize the session correctly if this same browser
        # later hits a role-protected page.
        session["role"] = user.get("role") or "consumer"
        return jsonify({"success": True})

    return jsonify({"success": False}), 401


@auth_bp.route("/lender/do-login", methods=["POST"])
def lender_login():
    """
    Dedicated login for the lender/analyst workspace.

    Uses the exact same credential verification as consumer /do-login
    (same users table, same bcrypt hashes) — this is NOT a separate
    password system. The only addition is a server-side role check:
    valid credentials are not enough, the account must have
    role == 'lender'. The role is read from the database record, never
    trusted from the request body.
    """
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"success": False, "message": "Email and password are required."}), 400

    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE email=%s",
        (email,)
    ).fetchone()
    conn.close()

    if not user or not bcrypt.checkpw(password.encode(), str(user["password"]).encode()):
        return jsonify({"success": False, "message": "Invalid email or password."}), 401

    if user.get("role") != "lender":
        # Valid credentials, wrong workspace — explicit, unambiguous denial.
        # We do not fall back to a consumer session here.
        return jsonify({"success": False, "message": "This account is not authorized for analyst access."}), 403

    if user.get("status") == "disabled":
        return jsonify({"success": False, "message": "This account has been disabled. Contact your administrator."}), 403

    session.clear()
    session["user_id"] = user["id"]
    session["logged_in"] = True
    session["role"] = "lender"
    return jsonify({"success": True})


@auth_bp.route("/admin/do-login", methods=["POST"])
def admin_login():
    """
    Dedicated login for the institution-admin workspace.

    Same users table, same bcrypt verification as every other login —
    NOT a separate password system/database. The only addition is a
    server-side check that the account's database role is 'admin'.

    Response codes (per spec):
      - missing email/password           -> 400
      - credentials invalid               -> 401
      - credentials valid, role != admin  -> 403
      - credentials valid, role == admin  -> 200
    """
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"success": False, "message": "Email and password are required."}), 400

    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE email=%s",
        (email,)
    ).fetchone()
    conn.close()

    if not user or not bcrypt.checkpw(password.encode(), str(user["password"]).encode()):
        return jsonify({"success": False, "message": "Invalid email or password."}), 401

    if user.get("role") != "admin":
        # A valid consumer or lender account trying the admin door.
        # We do not say "wrong role" — same generic denial either way,
        # so this endpoint can't be used to probe which emails are admins.
        return jsonify({"success": False, "message": "This account is not authorized for administrator access."}), 403

    if user.get("status") == "disabled":
        return jsonify({"success": False, "message": "This account has been disabled."}), 403

    session.clear()
    session["user_id"] = user["id"]
    session["logged_in"] = True
    session["role"] = "admin"
    return jsonify({"success": True})


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"success": True})


@auth_bp.route("/user-profile", methods=["GET"])
def user_profile():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    user_id = session["user_id"]
    conn = get_db()

    user = conn.execute(
        "SELECT id, name, email FROM users WHERE id = %s",
        (user_id,)
    ).fetchone()

    conn.close()

    if not user:
        return jsonify({"error": "User not found"}), 404

    return jsonify({
        "data": {
            "id": user["id"],
            "name": user["name"],
            "email": user["email"]
        }
    })


@auth_bp.route("/change-password", methods=["POST"])
def change_password():
    if "user_id" not in session:
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    data = request.get_json()
    current_password = data.get("current_password")
    new_password = data.get("new_password")

    if not current_password or not new_password:
        return jsonify({"success": False, "message": "Missing fields"}), 400

    conn = get_db()
    user = conn.execute(
        "SELECT password FROM users WHERE id=%s",
        (session["user_id"],)
    ).fetchone()

    if not user:
        conn.close()
        return jsonify({"success": False}), 404

    if not bcrypt.checkpw(current_password.encode(), str(user["password"]).encode()):
        conn.close()
        return jsonify({"success": False, "message": "Current password incorrect"}), 400

    hashed = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()

    conn.execute(
        "UPDATE users SET password=%s WHERE id=%s",
        (hashed, session["user_id"])
    )
    conn.commit()
    conn.close()

    return jsonify({"success": True})


@auth_bp.route("/update-profile", methods=["PUT"])
@login_required
def update_profile():
    data = request.get_json()
    name = data.get("name", "").strip()
    email = data.get("email", "").strip()

    if not name or not email:
        return jsonify({"success": False, "message": "Invalid data"}), 400

    conn = get_db()

    existing = conn.execute(
        "SELECT id FROM users WHERE email=%s AND id!=%s",
        (email, session["user_id"])
    ).fetchone()

    if existing:
        conn.close()
        return jsonify({"success": False, "message": "Email already in use"}), 400

    conn.execute(
        "UPDATE users SET name=%s, email=%s WHERE id=%s",
        (name, email, session["user_id"])
    )
    conn.commit()
    conn.close()

    return jsonify({"success": True})


@auth_bp.route("/forgot-password", methods=["POST"])
def forgot_password():
    data = request.get_json()
    email = data.get("email", "").strip().lower()

    if not email:
        return jsonify({"success": True})

    conn = get_db()
    user = conn.execute(
        "SELECT id FROM users WHERE email=%s",
        (email,)
    ).fetchone()

    if user:
        token = secrets.token_urlsafe(32)
        expiry = datetime.utcnow() + timedelta(minutes=30)

        conn.execute("""
            UPDATE users
            SET reset_token=%s, reset_expiry=%s
            WHERE id=%s
        """, (token, expiry, user["id"]))
        conn.commit()

        reset_link = f"{APP_BASE_URL}/reset-password?token={token}"

        send_reset_email(email, reset_link)

    conn.close()

    return jsonify({"success": True})


@auth_bp.route("/reset-password")
def reset_password_page():
    token = request.args.get("token")

    if not token:
        return "Invalid link", 400

    conn = get_db()
    user = conn.execute("""
        SELECT id FROM users
        WHERE reset_token=%s AND reset_expiry > %s
    """, (token, datetime.utcnow())).fetchone()
    conn.close()

    if not user:
        return "Reset link expired or invalid", 400

    return render_template("reset_password.html", token=token)


@auth_bp.route("/reset-password", methods=["POST"])
def reset_password():
    data = request.get_json()
    token = data.get("token")
    new_password = data.get("password")

    if not token:
        return jsonify({"success": False, "message": "Invalid request"})

    if not new_password or len(new_password) < 6:
        return jsonify({"success": False, "message": "Password must be at least 6 characters"})

    conn = get_db()
    user = conn.execute("""
        SELECT id FROM users
        WHERE reset_token=%s AND reset_expiry > %s
    """, (token, datetime.utcnow())).fetchone()

    if not user:
        conn.close()
        return jsonify({"success": False, "message": "Link expired or invalid"})

    hashed = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()

    conn.execute("""
        UPDATE users
        SET password=%s,
            reset_token=NULL,
            reset_expiry=NULL
        WHERE id=%s
    """, (hashed, user["id"]))

    conn.commit()
    conn.close()

    return jsonify({"success": True})


# ------------------------------------------------------------------
# Lender invitation activation (Phase B)
# ------------------------------------------------------------------
# Consumes the invitation_token/invitation_expiry/invitation_used
# columns that admin.py's provision_lender already writes (Phase A).
# Deliberately mirrors the reset-password pattern directly above —
# same bcrypt hashing, same "GET renders a page, POST performs the
# change" shape — rather than introducing any new auth mechanism.

def _now_utc():
    """Timezone-aware 'now', in UTC. invitation_expiry comes back from
    PostgreSQL as an offset-aware datetime, so comparisons against it
    must use an aware 'now' too (datetime.utcnow() is naive and raises
    TypeError when compared against it)."""
    return datetime.now(timezone.utc)


def _as_aware_utc(dt):
    """Normalizes a datetime to timezone-aware UTC before comparing,
    so this still works whether the value read back is naive (older
    rows) or aware (current column behavior)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _validate_invitation_token(conn, token):
    """
    Returns (user, error) where error is one of:
      None       -> token is valid and usable right now
      "invalid"  -> token doesn't exist, or belongs to a non-lender account
      "used"     -> invitation_used is already true
      "expired"  -> invitation_expiry has passed

    Role is read from the database record tied to the token — never
    trusted from the request. No user_id is ever taken from the client;
    the token is the only client-supplied identifier.
    """
    if not token:
        return None, "invalid"

    user = conn.execute(
        """
        SELECT id, name, email, role, invitation_used, invitation_expiry
        FROM users
        WHERE invitation_token=%s
        """,
        (token,),
    ).fetchone()

    if not user or user.get("role") != "lender":
        return None, "invalid"
    if user.get("invitation_used"):
        return None, "used"
    expiry = _as_aware_utc(user.get("invitation_expiry"))
    if not expiry or expiry <= _now_utc():
        return None, "expired"

    return user, None


_INVITATION_ERROR_MESSAGES = {
    "invalid": "This invitation link is invalid.",
    "expired": "This invitation link has expired. Please ask your administrator for a new invitation.",
    "used": "This invitation has already been used. If you already activated your account, please log in.",
}


@auth_bp.route("/lender/activate", methods=["GET"])
def lender_activate_page():
    """
    Public page — the invited lender lands here from the activation
    link. Read-only lookup, no state change. Shows the activation form
    (email pre-filled/read-only) when the token is currently valid, or
    a clear invalid/expired/already-used message otherwise.
    """
    token = request.args.get("token")

    conn = get_db()
    try:
        user, error = _validate_invitation_token(conn, token)
    finally:
        conn.close()

    if error:
        return render_template(
            "lender_activate.html",
            error=_INVITATION_ERROR_MESSAGES[error],
            token=token,
        )

    return render_template(
        "lender_activate.html",
        error=None,
        token=token,
        name=user["name"],
        email=user["email"],
    )


@auth_bp.route("/lender/activate", methods=["POST"])
def lender_activate():
    """
    Activates an invited lender account: sets a self-chosen password
    and marks the invitation single-use. Accepts only token, password,
    and confirm_password from the client — no user_id, no role, no
    email. The account is located solely via the token.

    The UPDATE's WHERE clause re-checks invitation_used/expiry/role
    atomically at write time (not just via the earlier read), so two
    concurrent activation requests for the same token can't both
    succeed — the first one to commit wins and the token becomes
    unusable immediately.

    Never logs the new password. Never returns the existing hash.
    Does not change role. Does not create a session (no auto-login) —
    the client redirects to /lender/login on success.
    """
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    password = data.get("password")
    confirm_password = data.get("confirm_password")

    if not token or not password or not confirm_password:
        return jsonify({"success": False, "message": "All fields are required."}), 400

    if password != confirm_password:
        return jsonify({"success": False, "message": "Passwords do not match."}), 400

    if len(password) < 6:
        return jsonify({"success": False, "message": "Password must be at least 6 characters."}), 400

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    conn = get_db()
    try:
        row = conn.execute(
            """
            UPDATE users
            SET password=%s,
                invitation_used=true,
                invitation_token=NULL,
                invitation_expiry=NULL
            WHERE invitation_token=%s
              AND invitation_used=false
              AND invitation_expiry > %s
              AND role='lender'
            RETURNING id
            """,
            (hashed, token, _now_utc()),
        ).fetchone()

        if not row:
            conn.rollback()
            # The atomic guard didn't match. Re-read (no state change)
            # purely to give a specific, honest reason why.
            _, error = _validate_invitation_token(conn, token)
            message = _INVITATION_ERROR_MESSAGES.get(error or "invalid", _INVITATION_ERROR_MESSAGES["invalid"])
            return jsonify({"success": False, "message": message}), 400

        conn.commit()
    except Exception as e:
        conn.rollback()
        print("LENDER ACTIVATE ERROR:", e)
        return jsonify({"success": False, "message": "Could not activate the account."}), 400
    finally:
        conn.close()

    return jsonify({"success": True, "redirect": "/lender/login"})


def send_reset_email(to_email, reset_link):
    _send_transactional_email(
        to_email=to_email,
        subject="Reset your FinTrust password",
        heading="Password Reset Request",
        body_html="<p>You requested to reset your password.</p>",
        cta_text="Reset Password",
        cta_link=reset_link,
        footer_html="<p>This link will expire in 30 minutes.</p>"
                    "<p>If you did not request this, ignore this email.</p>",
        dev_log_label="RESET LINK (dev mode)",
    )


def send_invite_email(to_email, invite_link, temp_password=None):
    """
    Used by the admin workspace when provisioning a new lender/analyst
    account. Reuses the exact same Brevo transactional-email plumbing
    and the exact same reset_token/reset_expiry columns and
    /reset-password flow that self-service password resets already use
    — no separate invite system.

    If BREVO_API_KEY isn't configured (local/dev), this falls back to
    printing the invite link to the console, same as send_reset_email
    already does, and the caller (routes/admin.py) additionally returns
    the temp_password in the API response in that case — clearly
    labeled as a development/demo provisioning mechanism, never done
    when real email delivery is available.
    """
    body_html = "<p>An administrator has provisioned an analyst account for you on FinTrust.</p>"
    if temp_password:
        body_html += (
            f"<p>Temporary password: <code>{temp_password}</code></p>"
            "<p>For security, please set your own password using the link below "
            "before signing in.</p>"
        )
    _send_transactional_email(
        to_email=to_email,
        subject="You've been granted analyst access to FinTrust",
        heading="Analyst Access Granted",
        body_html=body_html,
        cta_text="Set Your Password",
        cta_link=invite_link,
        footer_html="<p>This link will expire in 30 minutes. If it expires, ask your "
                    "administrator to re-send an invite.</p>",
        dev_log_label="LENDER INVITE LINK (dev mode)",
    )


def send_lender_invitation_email(to_email, activation_link, ttl_display):
    """
    Sends the lender invitation/activation email (Phase C). Reuses the
    exact same Brevo transactional-email plumbing as send_reset_email
    and send_invite_email above — no new provider, no new config.

    Deliberately contains only the activation link — never a plaintext
    password, a password hash, or any invitation internals (token
    value aside from what's embedded in the link, expiry timestamp,
    used flag, DB ids). `ttl_display` is a human-readable string like
    "7 days", not a raw duration/expiry value.

    Returns the same status dict as _send_transactional_email, so the
    caller (admin.py) can tell a real Brevo send apart from a dev
    fallback or a failed send, and respond to the admin accordingly.
    """
    body_html = (
        "<p>You have been granted <strong>analyst access</strong> to FinTrust.</p>"
        "<p>This workspace is dedicated to <strong>Credit Risk Intelligence</strong> "
        "and is intended for authorized lenders and credit analysts only.</p>"
        f"<p>This invitation link expires in {ttl_display}.</p>"
        "<p>Please activate your account and choose your own password using the "
        "secure link below.</p>"
    )
    return _send_transactional_email(
        to_email=to_email,
        subject="FinTrust Analyst Workspace Invitation",
        heading="Analyst Workspace Invitation",
        body_html=body_html,
        cta_text="Activate Analyst Account",
        cta_link=activation_link,
        footer_html=f"<p>This link expires in {ttl_display}. If it expires, ask your "
                    "administrator to resend the invitation.</p>",
        dev_log_label="LENDER INVITATION LINK (dev mode)",
    )


def _send_transactional_email(to_email, subject, heading, body_html, cta_text, cta_link, footer_html, dev_log_label):
    """
    Returns a status dict so callers can tell dev-fallback, an actual
    successful Brevo send, and a failed Brevo send apart:
      {"success": True,  "mode": "sent"}
      {"success": False, "mode": "dev_fallback"}
      {"success": False, "mode": "failed", "error": "<message>"}

    Existing callers that don't inspect the return value keep working
    exactly as before — this is purely additive.
    """
    BREVO_API_KEY = os.getenv("BREVO_API_KEY")
    SENDER_EMAIL = os.getenv("BREVO_SENDER_EMAIL", "ansumanpatra200609@gmail.com")

    if not BREVO_API_KEY:
        print(f"\n=== {dev_log_label} ===")
        print(cta_link)
        print("=============================\n")
        return {"success": False, "mode": "dev_fallback"}
    SENDER_NAME = "FinTrust"

    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key['api-key'] = BREVO_API_KEY

    api_instance = sib_api_v3_sdk.TransactionalEmailsApi(
        sib_api_v3_sdk.ApiClient(configuration)
    )

    html_content = f"""
    <html>
    <body>
        <h2>{heading}</h2>
        {body_html}
        <p>
            <a href="{cta_link}"
               style="background:#5b21b6;color:white;padding:10px 18px;
                      text-decoration:none;border-radius:6px;">
               {cta_text}
            </a>
        </p>
        {footer_html}
    </body>
    </html>
    """

    send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
        to=[{"email": to_email}],
        sender={"email": SENDER_EMAIL, "name": SENDER_NAME},
        subject=subject,
        html_content=html_content
    )

    try:
        api_instance.send_transac_email(send_smtp_email)
        return {"success": True, "mode": "sent"}
    except ApiException as e:
        print("Brevo error:", e)
        return {"success": False, "mode": "failed", "error": str(e)}
    except Exception as e:
        print("Unexpected email error:", e)
        return {"success": False, "mode": "failed", "error": str(e)}