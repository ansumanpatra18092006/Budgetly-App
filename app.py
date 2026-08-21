from flask import Flask, render_template, session, redirect, url_for
from flask_cors import CORS
from dotenv import load_dotenv, find_dotenv
import os

# ─────────────────────────────────────────────────────────────
# Environment
# ─────────────────────────────────────────────────────────────
print("DOTENV FILE:", find_dotenv())
load_dotenv(find_dotenv(), override=True)

# ─────────────────────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────────────────────
from utils.db import init_db

# ─────────────────────────────────────────────────────────────
# Migrations
# ─────────────────────────────────────────────────────────────
from utils.db_migrate import run_migrations

# ─────────────────────────────────────────────────────────────
# Blueprints
# ─────────────────────────────────────────────────────────────
from routes.auth import auth_bp
from routes.transactions import transactions_bp
from routes.dashboard import dashboard_bp
from routes.goals import goals_bp
from routes.insights import insights_bp
from routes.oauth import oauth_bp
from routes.chatbot import chat_bp
from routes.ai_insights import ai_insights_bp
from routes.preview import preview_bp
from routes.credit_risk import credit_risk_bp

# ─────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────
app = Flask(__name__)

# Secret key
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")

# ─────────────────────────────────────────────────────────────
# CORS
# ─────────────────────────────────────────────────────────────
CORS(
    app,
    supports_credentials=True,
    resources={r"/*": {"origins": "*"}},
)

# ─────────────────────────────────────────────────────────────
# Session configuration
# ─────────────────────────────────────────────────────────────
# For production behind HTTPS, keep Secure enabled.
# SameSite=Lax works for the normal web flow.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="None",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true",
)

# ─────────────────────────────────────────────────────────────
# Database initialization
# ─────────────────────────────────────────────────────────────
try:
    init_db()
    run_migrations()
    print("✅ Database initialized")
except Exception as e:
    print(f"⚠️ DB init skipped: {e}")

# ─────────────────────────────────────────────────────────────
# Register blueprints
# ─────────────────────────────────────────────────────────────
app.register_blueprint(oauth_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(transactions_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(goals_bp)
app.register_blueprint(insights_bp)
app.register_blueprint(chat_bp)
app.register_blueprint(ai_insights_bp)
app.register_blueprint(preview_bp)
app.register_blueprint(credit_risk_bp)

# ─────────────────────────────────────────────────────────────
# Page routes
# ─────────────────────────────────────────────────────────────

@app.route("/")
def home():
    if not session.get("logged_in"):
        return redirect(url_for("login_page"))

    return render_template("index.html")


@app.route("/login")
def login_page():
    return render_template("login.html")


# ─────────────────────────────────────────────────────────────
# Session/user verification endpoint
# ─────────────────────────────────────────────────────────────

@app.route("/me", methods=["GET"])
def get_me():
    """
    Return the currently authenticated user.

    PostgreSQL version:
    Uses %s parameter placeholders rather than SQLite's ? syntax.
    """
    user_id = session.get("user_id")

    if not user_id:
        return {"error": "Unauthorized"}, 401

    from utils.db import get_db

    conn = get_db()

    try:
        user = conn.execute(
            """
            SELECT id, name, email
            FROM users
            WHERE id = %s
            """,
            (user_id,),
        ).fetchone()
    finally:
        conn.close()

    if not user:
        return {"error": "User not found"}, 404

    return {
        "data": {
            "id": user["id"],
            "name": user["name"],
            "email": user["email"],
        }
    }


# ─────────────────────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return {"status": "ok"}


# ─────────────────────────────────────────────────────────────
# Local development
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000)),
        debug=False,
    )