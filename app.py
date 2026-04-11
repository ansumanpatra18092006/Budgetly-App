from flask import Flask, render_template, session, redirect, url_for
from flask_cors import CORS
from dotenv import load_dotenv
import os

load_dotenv()

from utils.db import init_db, get_db
from utils.db_migrate import run_migrations   # ← NEW

# ── existing blueprints ───────────────────────────────────────────
from routes.auth import auth_bp
from routes.transactions import transactions_bp
from routes.dashboard import dashboard_bp
from routes.goals import goals_bp
from routes.insights import insights_bp
from routes.oauth import oauth_bp
from routes.chatbot import chat_bp
from routes.ai_insights import ai_insights_bp

# ── new blueprints ────────────────────────────────────────────────
from routes.wallet import wallet_bp       # ← NEW
from routes.preview import preview_bp     # ← NEW

# ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")
CORS(app, supports_credentials=True)

# DB setup — run original init then migrations
init_db()

from utils.db_migrate import run_migrations
run_migrations()   # ← NEW (idempotent)


# ── register blueprints ───────────────────────────────────────────
app.register_blueprint(oauth_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(transactions_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(goals_bp)
app.register_blueprint(insights_bp)
app.register_blueprint(chat_bp)
app.register_blueprint(ai_insights_bp)
app.register_blueprint(wallet_bp)    # ← NEW  (prefix /wallet)
app.register_blueprint(preview_bp)   # ← NEW

# ── page routes ───────────────────────────────────────────────────
@app.route("/")
def home():
    if not session.get("logged_in"):
        return redirect(url_for("login_page"))
    return render_template("index.html")

@app.route("/login")
def login_page():
    return render_template("login.html")

@app.route("/me", methods=["GET"])
def get_me():
    if "user_id" not in session:
        return {"error": "Unauthorized"}, 401
    conn = get_db()
    user = conn.execute(
        "SELECT id, name, email FROM users WHERE id=?",
        (session["user_id"],)
    ).fetchone()
    conn.close()
    if not user:
        return {"error": "User not found"}, 404
    return {"id": user["id"], "name": user["name"], "email": user["email"]}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)