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

APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:5000")

@auth_bp.route("/signup", methods=["POST"])
def signup():
    data = request.get_json()
    conn = get_db()
    hashed = bcrypt.hashpw(data["password"].encode(), bcrypt.gensalt()).decode()

    try:
        conn.execute(
            "INSERT INTO users (name,email,password) VALUES (%s,%s,%s)",
            (data["name"], data["email"], hashed)
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


def send_reset_email(to_email, reset_link):
    BREVO_API_KEY = os.getenv("BREVO_API_KEY")
    SENDER_EMAIL = os.getenv("BREVO_SENDER_EMAIL", "ansumanpatra200609@gmail.com")

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
        print("Brevo error:", e)
    except Exception as e:
        print("Unexpected email error:", e)