from flask import Flask, render_template, session, redirect, url_for
from flask_cors import CORS
from dotenv import load_dotenv
import os

from utils.db import init_db

# Import Blueprints
from routes.auth import auth_bp
from routes.transactions import transactions_bp
from routes.dashboard import dashboard_bp
from routes.goals import goals_bp
from routes.insights import insights_bp
from routes.oauth import oauth_bp

from routes.chatbot import chat_bp
from routes.ai_insights import ai_insights_bp   # ← add this import

from utils.db import get_db

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")
CORS(app, supports_credentials=True)

init_db()

app.register_blueprint(oauth_bp)
load_dotenv()

# Register Blueprints
app.register_blueprint(auth_bp)
app.register_blueprint(transactions_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(goals_bp)
app.register_blueprint(insights_bp)
app.register_blueprint(chat_bp)
app.register_blueprint(ai_insights_bp)       

@app.route("/")
def home():
    if not session.get("logged_in"):
        return redirect(url_for("login_page"))   # if using local login route
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

    return {
        "id": user["id"],
        "name": user["name"],
        "email": user["email"]
    }

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)