from flask import Blueprint, request, jsonify, session
from utils.db import get_db
import bcrypt
from utils.decorators import login_required
import secrets
from datetime import datetime, timedelta
from flask import render_template
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException
import os

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/signup", methods=["POST"])
def signup():
    data = request.get_json()
    conn = get_db()
    hashed = bcrypt.hashpw(data["password"].encode(), bcrypt.gensalt())

    try:
        conn.execute(
            "INSERT INTO users (name,email,password) VALUES (?,?,?)",
            (data["name"], data["email"], hashed)
        )
        conn.commit()
        return jsonify({"success": True})
    except:
        return jsonify({"success": False}), 400
    finally:
        conn.close()


@auth_bp.route("/do-login", methods=["POST"])
def login():
    data = request.get_json()
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE email=?",
        (data["email"],)
    ).fetchone()
    conn.close()

    if user and bcrypt.checkpw(data["password"].encode(), user["password"]):
        session["user_id"] = user["id"]
        session["logged_in"] = True
        return jsonify({"success": True})

    return jsonify({"success": False}), 401


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
        "SELECT id, name, email FROM users WHERE id = ?",
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
        "SELECT password FROM users WHERE id=?",
        (session["user_id"],)
    ).fetchone()

    if not user:
        conn.close()
        return jsonify({"success": False}), 404

    if not bcrypt.checkpw(current_password.encode(), user["password"]):
        conn.close()
        return jsonify({"success": False, "message": "Current password incorrect"}), 400

    hashed = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt())

    conn.execute(
        "UPDATE users SET password=? WHERE id=?",
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
        "SELECT id FROM users WHERE email=? AND id!=?",
        (email, session["user_id"])
    ).fetchone()

    if existing:
        conn.close()
        return jsonify({"success": False, "message": "Email already in use"}), 400

    conn.execute(
        "UPDATE users SET name=?, email=? WHERE id=?",
        (name, email, session["user_id"])
    )
    conn.commit()
    conn.close()

    return jsonify({"success": True})


@auth_bp.route("/forgot-password", methods=["POST"])
def forgot_password():
    data = request.get_json()
    email = data.get("email", "").strip().lower()

    # Always return success early if no email provided (security: no info leak)
    if not email:
        return jsonify({"success": True})

    conn = get_db()
    user = conn.execute(
        "SELECT id FROM users WHERE email=?",
        (email,)
    ).fetchone()

    if user:
        token = secrets.token_urlsafe(32)
        expiry = datetime.utcnow() + timedelta(minutes=30)

        conn.execute("""
            UPDATE users
            SET reset_token=?, reset_expiry=?
            WHERE id=?
        """, (token, expiry, user["id"]))
        conn.commit()

        reset_link = f" https://oilless-romona-nostalgically.ngrok-free.dev/reset-password?token={token}"

        # Send email — failure is caught inside, app will not crash
        send_reset_email(email, reset_link)

    conn.close()

    # Always return success — never reveal whether email exists (security)
    return jsonify({"success": True})


@auth_bp.route("/reset-password")
def reset_password_page():
    token = request.args.get("token")

    if not token:
        return "Invalid link", 400

    conn = get_db()
    user = conn.execute("""
        SELECT id FROM users
        WHERE reset_token=? AND reset_expiry > ?
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

    # Guard: token must be present
    if not token:
        return jsonify({"success": False, "message": "Invalid request"})

    # Validate password length (min 6 characters)
    if not new_password or len(new_password) < 6:
        return jsonify({"success": False, "message": "Password must be at least 6 characters"})

    conn = get_db()
    user = conn.execute("""
        SELECT id FROM users
        WHERE reset_token=? AND reset_expiry > ?
    """, (token, datetime.utcnow())).fetchone()

    if not user:
        conn.close()
        return jsonify({"success": False, "message": "Link expired or invalid"})

    # FIX: Use bcrypt (same as login/signup) so login still works after reset
    hashed = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt())

    # Clear token after use — token is single-use
    conn.execute("""
        UPDATE users
        SET password=?,
            reset_token=NULL,
            reset_expiry=NULL
        WHERE id=?
    """, (hashed, user["id"]))

    conn.commit()
    conn.close()

    return jsonify({"success": True})


def send_reset_email(to_email, reset_link):
    # Use environment variable for API key; fall back to hardcoded value for dev
    BREVO_API_KEY = os.getenv("BREVO_API_KEY")
    SENDER_EMAIL = os.getenv("BREVO_SENDER_EMAIL", "ansumanpatra200609@gmail.com")

    print("API:", os.getenv("BREVO_API_KEY"))
    print("Sender:", os.getenv("BREVO_SENDER_EMAIL"))
    # Dev mode: no API key set → print link to terminal instead of sending email
    if not os.getenv("BREVO_API_KEY"):
        print("\n=== RESET LINK (dev mode) ===")
        print(reset_link)
        print("=============================\n")
        return
    SENDER_NAME = "Budgetly"

    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key['api-key'] = BREVO_API_KEY

    api_instance = sib_api_v3_sdk.TransactionalEmailsApi(
        sib_api_v3_sdk.ApiClient(configuration)
    )

    subject = "Reset your Budgetly password"

    html_content = f"""
    <html>
    <body>
        <h2>Password Reset Request</h2>
        <p>You requested to reset your password.</p>
        <p>
            <a href="{reset_link}"
               style="background:#5b21b6;color:white;padding:10px 18px;
                      text-decoration:none;border-radius:6px;">
               Reset Password
            </a>
        </p>
        <p>This link will expire in 30 minutes.</p>
        <p>If you did not request this, ignore this email.</p>
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
    except ApiException as e:
        # Log but do not crash the app
        print("Brevo error:", e)
    except Exception as e:
        print("Unexpected email error:", e)

