from flask import Blueprint, redirect, session, request, url_for
import requests
from requests_oauthlib import OAuth2Session
import os
from utils.db import get_db

oauth_bp = Blueprint("oauth", __name__)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
REDIRECT_URI = "http://127.0.0.1:5000/google-callback"


def get_google_cfg():
    return requests.get(DISCOVERY_URL).json()


@oauth_bp.route("/google-login")
def google_login():
    google_cfg = get_google_cfg()
    auth_endpoint = google_cfg["authorization_endpoint"]

    oauth = OAuth2Session(
        GOOGLE_CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        scope=["openid", "email", "profile"]
    )

    url, state = oauth.authorization_url(auth_endpoint)
    session["oauth_state"] = state
    return redirect(url)


@oauth_bp.route("/google-callback")
def google_callback():
    google_cfg = get_google_cfg()
    token_endpoint = google_cfg["token_endpoint"]

    oauth = OAuth2Session(
        GOOGLE_CLIENT_ID,
        state=session.get("oauth_state"),
        redirect_uri=REDIRECT_URI
    )

    oauth.fetch_token(
        token_endpoint,
        client_secret=GOOGLE_CLIENT_SECRET,
        authorization_response=request.url
    )

    userinfo = oauth.get(google_cfg["userinfo_endpoint"]).json()

    email = userinfo["email"]
    name = userinfo.get("name", "Google User")

    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE email=?",
        (email,)
    ).fetchone()

    if not user:
        cursor = conn.execute(
            "INSERT INTO users (name, email, password) VALUES (?, ?, '')",
            (name, email)
        )
        conn.commit()
        user_id = cursor.lastrowid
    else:
        user_id = user["id"]

    conn.close()

    session["logged_in"] = True
    session["user_id"] = user_id

    return redirect(url_for("home"))