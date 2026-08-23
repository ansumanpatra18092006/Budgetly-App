"""
chatbot.py — FinTrust AI Financial Assistant (Streaming Edition)
=================================================================
Cloud LLM chatbot powered by Google Gemini.

Design philosophy:
  - Python does ALL the financial math first — LLM only writes plain English
  - Intent-specific prompts so the model knows exactly what to say
  - Fast-path covers ~60% of questions with instant deterministic answers
    (fast-path responses are still streamed word-by-word for consistent UX)
  - Replies are always 2-3 short sentences, skimmable at a glance
  - No markdown, no jargon, no filler phrases

Register in app.py:
    from routes.chatbot import chat_bp
    app.register_blueprint(chat_bp)
"""

from __future__ import annotations

import json
import re
import time
import random
import traceback

import requests
from flask import Blueprint, Response, request, session, stream_with_context

from routes.ai_insights import _fetch_full_metrics
from services.gemini_service import stream_chat as gemini_stream_chat, GEMINI_MODEL
from utils.db import get_db
from utils.decorators import login_required

# ─────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────

chat_bp = Blueprint("chat", __name__)

# { user_id: [{role: ..., content: ...}, ...] }
conversation_memory: dict[int, list[dict]] = {}


# ─────────────────────────────────────────────────────────────────
# Intent detection
# ─────────────────────────────────────────────────────────────────
GREETINGS = {
    "hi", "hello", "hey", "yo", "hii",
    "good morning", "good afternoon", "good evening"
}

SMALL_TALK = {
    "how are you",
    "what's up",
    "whats up",
    "how's it going",
    "hows it going",
    "who are you",
    "what can you do",
    "thanks",
    "thank you"
}

_INTENT_MAP = [
    # NEW: Conversational and greeting intents
    ("greeting", [
        "hi", "hello", "hey", "yo", "hii", "good morning", "good evening", "good afternoon"
    ]),
    
    ("small_talk", [
        "how are you", "what's up", "how's it going", "who are you", "what can you do", "thanks", "thank you"
    ]),

    # EXISTING: Financial intents
    ("affordability", [
        "afford", "can i buy", "can i afford", "can i get",
        "is it okay to buy", "should i buy"
    ]),

    ("saving_advice", [
        "save", "saving", "saving tips", "how to save"
    ]),

    ("goal_status", [
        "goal", "new goal", "target", "milestone"
    ]),

    ("overspending", [
        "overspend", "overspending", "spending too much"
    ]),

    ("reduce_spend", [
        "reduce", "cut", "spend less"
    ]),

    ("budget_status", [
        "budget", "remaining budget", "how much left"
    ]),

    ("investment", [
        "invest", "sip", "fd", "stocks"
    ]),

    ("summary", [
        "summary", "overview", "financial health"
    ]),
]


def _detect_intent(message: str) -> str:
    msg = message.lower()

    # normalize text
    msg = re.sub(r"[^\w\s]", " ", msg)
    msg = re.sub(r"\s+", " ", msg).strip()

    for intent, keywords in _INTENT_MAP:
        for kw in keywords:
            # Added word boundaries (\b) to prevent "hi" triggering inside "this"
            if re.search(rf"\b{re.escape(kw)}\b", msg):
                return intent

    return "general"


# ─────────────────────────────────────────────────────────────────
# Financial analysis  (unchanged logic)
# ─────────────────────────────────────────────────────────────────

def _analyse(metrics: dict, message: str, intent: str) -> dict:
    income          = float(metrics.get("income",          0))
    expense         = float(metrics.get("expense",         0))
    surplus         = float(metrics.get("surplus",         0))
    savings_rate    = float(metrics.get("savings_rate",    0))
    budget          = float(metrics.get("budget",          0))
    budget_used_pct = float(metrics.get("budget_used_pct", 0))
    expense_change  = float(metrics.get("expense_change",  0))
    daily_burn      = float(metrics.get("daily_burn",      0))
    days_left       = int(metrics.get("days_left",         0))
    top_cat         = metrics.get("top_cat_name", "miscellaneous")
    top_pct         = float(metrics.get("top_cat_pct",     0))
    goals           = metrics.get("goals", [])

    safe_spend_40  = round(surplus * 0.40)
    budget_left    = max(0.0, budget - expense) if budget > 0 else 0.0
    top_cat_amount = round(expense * top_pct / 100)
    top_cat_cut_10 = round(top_cat_amount * 0.10)
    top_cat_cut_15 = round(top_cat_amount * 0.15)

    if savings_rate >= 25:   savings_verdict = "excellent"
    elif savings_rate >= 15: savings_verdict = "good"
    elif savings_rate >= 5:  savings_verdict = "low"
    else:                    savings_verdict = "critical"

    if budget_used_pct >= 90:   budget_verdict = "almost gone"
    elif budget_used_pct >= 70: budget_verdict = "getting tight"
    elif budget_used_pct >= 40: budget_verdict = "on track"
    else:                       budget_verdict = "healthy"

    if expense_change > 20:    trend = f"up {expense_change:.0f}% vs last month — rising fast"
    elif expense_change > 5:   trend = f"up {expense_change:.0f}% vs last month"
    elif expense_change < -10: trend = f"down {abs(expense_change):.0f}% vs last month — improving"
    else:                      trend = "stable vs last month"

    goals_summary = []
    for g in goals[:3]:
        tgt   = float(g.get("target_amount", 0) or 0)
        saved = float(g.get("saved_amount",  0) or 0)
        name  = g.get("name", "Goal")
        if tgt <= 0:
            continue
        pct  = round(saved / tgt * 100, 1)
        rem  = tgt - saved
        mths = round(rem / surplus, 1) if surplus > 0 else None
        goals_summary.append({"name": name, "pct": pct, "saved": int(saved),
                               "target": int(tgt), "remaining": int(rem),
                               "months_away": mths})

    asked_amount = None
    if intent == "affordability":
        nums = re.findall(r"\d[\d,]*", message.replace(",", ""))
        if nums:
            asked_amount = float(max(nums, key=len))

    return dict(
        income=income, expense=expense, surplus=surplus,
        savings_rate=savings_rate, budget=budget,
        budget_used_pct=budget_used_pct, budget_left=budget_left,
        expense_change=expense_change, daily_burn=daily_burn,
        days_left=days_left, top_cat=top_cat, top_pct=top_pct,
        top_cat_amount=top_cat_amount, top_cat_cut_10=top_cat_cut_10,
        top_cat_cut_15=top_cat_cut_15, safe_spend_40=safe_spend_40,
        savings_verdict=savings_verdict, budget_verdict=budget_verdict,
        trend=trend, goals_summary=goals_summary, asked_amount=asked_amount,
    )


# ─────────────────────────────────────────────────────────────────
# Fast-path  (includes new conversational paths)
# ─────────────────────────────────────────────────────────────────

def _fast_path(intent: str, a: dict) -> str | None:
    # NEW: Greeting fast-path without financial analysis
    if intent == "greeting":
        responses = [
            "Hey! 👋 I'm FinTrust AI. How can I help today?",
            "Hello! Need help with budgeting, spending, savings, goals, or a financial decision?",
            "Hi there! What would you like to know?"
        ]
        return random.choice(responses)

    # NEW: Small talk fast-path without financial analysis
    if intent == "small_talk":
        responses = [
            "I'm doing great! I'm here to help you understand your finances and make smarter money decisions.",
            "I can help with budgeting, spending analysis, savings goals, debt planning, and financial insights.",
            "You're welcome! Let me know if you need help with anything financial."
        ]
        return random.choice(responses)

    # EXISTING: Financial fast-paths
    if intent == "budget_status" and a["budget"] > 0:
        daily_safe = int(a["budget_left"] / max(a["days_left"], 1))
        if a["budget_verdict"] == "almost gone":
            return (
                f"Your budget is {a['budget_used_pct']:.0f}% used with only ₹{a['budget_left']:,.0f} left "
                f"and {a['days_left']} days still to go. "
                f"Limit your spending to ₹{daily_safe:,}/day starting today, "
                f"focusing cuts on {a['top_cat']} which is your biggest expense at {a['top_pct']:.0f}%. "
                f"Staying within ₹{daily_safe:,}/day means you'll finish the month without going over budget."
            )
        if a["budget_verdict"] == "getting tight":
            return (
                f"You've used {a['budget_used_pct']:.0f}% of your budget with {a['days_left']} days remaining "
                f"and ₹{a['budget_left']:,.0f} left. "
                f"Set a daily cap of ₹{daily_safe:,} and review your {a['top_cat']} spending this week. "
                f"Sticking to that limit means you'll stay within budget and have ₹0 overspend by month end."
            )
        return (
            f"Your budget is healthy — {a['budget_used_pct']:.0f}% used with ₹{a['budget_left']:,.0f} "
            f"remaining for {a['days_left']} days. "
            f"Keep your daily spending near ₹{daily_safe:,} to maintain this pace. "
            f"At that rate you'll finish the month with money to spare and your budget fully intact."
        )

    if intent == "affordability":
        if a["surplus"] <= 0:
            return (
                f"Your expenses currently exceed your income by ₹{abs(int(a['surplus'])):,} this month, "
                f"so there is no free cash available right now. "
                f"Hold off on this purchase and reduce {a['top_cat']} spending first — "
                f"a 15% cut there would free up ₹{a['top_cat_cut_15']:,} and get you back to a positive balance."
            )
        if a["asked_amount"]:
            amt = a["asked_amount"]
            if amt <= a["safe_spend_40"]:
                leftover = int(a["safe_spend_40"] - amt)
                return (
                    f"Your surplus this month is ₹{int(a['surplus']):,} and your safe spend limit is ₹{a['safe_spend_40']:,}. "
                    f"Go ahead and make this ₹{int(amt):,} purchase — it fits well within your comfortable range. "
                    f"You'll still have ₹{leftover:,} of your safe budget untouched after buying it."
                )
            elif amt <= a["surplus"]:
                return (
                    f"₹{int(amt):,} is within your ₹{int(a['surplus']):,} surplus but above "
                    f"your comfortable limit of ₹{a['safe_spend_40']:,}. "
                    f"You can make this purchase, but keep all other discretionary spending minimal for the rest of the month. "
                    f"Doing so means your finances stay positive, though with very little room to spare."
                )
            else:
                over = int(amt - a["surplus"])
                return (
                    f"₹{int(amt):,} is ₹{over:,} more than your current surplus of ₹{int(a['surplus']):,}, "
                    f"so buying it now would put you in deficit. "
                    f"Wait until next month, or cut {a['top_cat']} by 15% this week to free up ₹{a['top_cat_cut_15']:,}. "
                    f"That saving alone could bridge most of the gap within a few weeks."
                )
        return (
            f"Your monthly surplus is ₹{int(a['surplus']):,} and your safe single-purchase limit is ₹{a['safe_spend_40']:,}. "
            f"Tell me the exact amount you're thinking of and I'll give you a direct yes or no. "
            f"Knowing the figure means I can tell you exactly how much buffer you'd have left after buying it."
        )

    if intent == "summary":
        verdict_line = {
            "excellent": "Your finances are in excellent shape this month.",
            "good":      "Your finances are in decent shape this month.",
            "low":       "Your finances need some attention this month.",
            "critical":  "Your finances are under stress and need immediate action.",
        }[a["savings_verdict"]]
        return (
            f"{verdict_line} "
            f"You earned ₹{int(a['income']):,}, spent ₹{int(a['expense']):,}, "
            f"and have a surplus of ₹{int(a['surplus']):,} — a savings rate of {a['savings_rate']:.0f}%. "
            f"Trim {a['top_cat']} (currently {a['top_pct']:.0f}% of expenses) by 10% "
            f"and you'll save an extra ₹{a['top_cat_cut_10']:,} next month."
        )

    if intent == "overspending":
        return (
            f"{a['top_cat']} is consuming {a['top_pct']:.0f}% of your total expenses — "
            f"that's ₹{a['top_cat_amount']:,} this month, and spending is {a['trend']}. "
            f"Review your {a['top_cat']} transactions this week and cut the lowest-value ones by 15%. "
            f"That single change saves ₹{a['top_cat_cut_15']:,}/month and directly boosts your surplus."
        )

    if intent == "reduce_spend":
        return (
            f"{a['top_cat']} is your highest spend at {a['top_pct']:.0f}% of expenses "
            f"(₹{a['top_cat_amount']:,}/month) — that's where the most savings are hiding. "
            f"Cut {a['top_cat']} by 10% this month by identifying and removing your lowest-value purchases there. "
            f"That one change puts ₹{a['top_cat_cut_10']:,} back in your pocket every single month."
        )

    if intent == "goal_status" and a["goals_summary"]:
        g = a["goals_summary"][0]
        if g["pct"] >= 100:
            return (
                f"Your goal '{g['name']}' is 100% complete — you saved the full ₹{g['target']:,}. "
                f"Set your next goal now while the saving habit is still strong. "
                f"Redirecting your current ₹{int(a['surplus']):,}/month surplus to a new goal means "
                f"you'll make meaningful progress from day one."
            )
        mths_text = (
            f"you'll reach it in about {g['months_away']} months at your current surplus"
            if g["months_away"] else "the timeline is unclear without a positive surplus"
        )
        return (
            f"'{g['name']}' is {g['pct']:.0f}% funded — ₹{g['saved']:,} saved, "
            f"₹{g['remaining']:,} still needed. "
            f"Keep your ₹{int(a['surplus']):,}/month surplus going into this goal consistently. "
            f"At that rate, {mths_text} — no extra effort needed, just consistency."
        )

    return None  # fall through to LLM


# ─────────────────────────────────────────────────────────────────
# System prompt  (updated to be conversational & dynamic)
# ─────────────────────────────────────────────────────────────────

def _build_prompt(a: dict, intent: str, message: str) -> str:
    goals_text = ""
    for g in a["goals_summary"]:
        mths = f", about {g['months_away']} months away" if g["months_away"] else ""
        goals_text += f"{g['name']}: {g['pct']:.0f}% done (₹{g['saved']:,}/₹{g['target']:,}{mths})\n"
    if not goals_text:
        goals_text = "No active goals.\n"

    # NEW: Contextual intent instructions
    intent_task = {
        "saving_advice": (
            f"The user wants to improve savings. Their savings rate is {a['savings_rate']:.0f}% "
            f"and their highest spending category is {a['top_cat']} ({a['top_pct']:.0f}%). "
            f"Give one practical way to reduce spending and increase savings."
        ),
        "investment": (
            f"The user has a monthly surplus of ₹{int(a['surplus']):,}. "
            f"Suggest a simple and realistic way to invest this amount for a beginner in India."
        ),
        "greeting": "The user is greeting you. Respond naturally and ask how you can help with their finances. DO NOT mention financial stats.",
        "small_talk": "The user is making casual conversation. Be friendly and chatty. DO NOT mention financial stats.",
        "general": (
            "Answer the user's question practically. If it's a financial question, use their real financial data. "
            "If it's casual conversation, prioritize natural conversation and DO NOT force financial metrics."
        ),
    }.get(intent, "Answer naturally. Use financial data only if the question is financial.")

    return f"""
You are FinTrust AI, a friendly, smart, and conversational personal finance coach.

Your job is to give short, clear, and useful financial advice or casually chat, depending on the user's prompt.
Prioritize natural conversation for casual messages, and financial reasoning for financial queries.

IMPORTANT RULES:
- Write EXACTLY 2 to 3 sentences.
- Be friendly and conversational, not overly formal or robotic.
- Do NOT use labels like Observation, Action, or Benefit.
- Use simple, natural English (like talking to a friend).
- Use ₹ for money values.
- Never invent numbers — use the data given below ONLY if relevant to the question.
- NEVER force financial statistics into greetings or casual conversations.

USER FINANCIAL DATA (Use ONLY if the user asks a financial question):
Income: ₹{int(a['income']):,}
Expenses: ₹{int(a['expense']):,}
Surplus: ₹{int(a['surplus']):,}
Savings rate: {a['savings_rate']:.0f}% ({a['savings_verdict']})
Budget used: {a['budget_used_pct']:.0f}% ({a['budget_verdict']}) with ₹{a['budget_left']:,.0f} left
Spending trend: {a['trend']}
Top spending: {a['top_cat']} ({a['top_pct']:.0f}%) = ₹{a['top_cat_amount']:,}
10% cut saves: ₹{a['top_cat_cut_10']:,}
15% cut saves: ₹{a['top_cat_cut_15']:,}
Safe spend amount: ₹{a['safe_spend_40']:,}

GOALS:
{goals_text}

TASK:
{intent_task}

USER MESSAGE:
{message}

RESPONSE FORMAT:
If financial: 1. Explain situation with numbers. 2. Suggest action. 3. Explain benefit.
If casual/greeting: Be friendly and helpful without numbers.

Your answer:
""".strip()


# ─────────────────────────────────────────────────────────────────
# Reply cleanup  (unchanged)
# ─────────────────────────────────────────────────────────────────

_FILLER = [
    "Certainly,", "Certainly!", "Of course,", "Of course!",
    "Sure,", "Sure!", "Absolutely,", "Great question!",
    "That's a great question.", "Happy to help!", "I'd be happy to help.",
]


def _clean(text: str) -> str:
    for phrase in _FILLER:
        text = text.replace(phrase, "")
    text = text.replace("$", "₹")
    text = re.sub(r"\*+", "", text)
    text = re.sub(r"#+\s*", "", text)
    text = re.sub(r"\n{2,}", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def _trim_sentences(text: str, max_count: int = 3) -> str:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    parts = [p.strip() for p in parts if p.strip()]
    result = " ".join(parts[:max_count])
    if result and result[-1] not in ".!?":
        match = re.search(r"[.!?][^.!?]*$", result)
        if match:
            result = result[: match.start() + 1]
    return result.strip()


# ─────────────────────────────────────────────────────────────────
# SSE helpers  (unchanged logic)
# ─────────────────────────────────────────────────────────────────

def _sse(event: str, data: str) -> str:
    """Format a single Server-Sent Event line."""
    safe = data.replace("\n", "\\n")
    return f"event:{event}\ndata:{safe}\n\n"


def _stream_text(text: str, model_used: str = "instant"):
    """
    Yield a pre-computed string word-by-word as SSE tokens,
    then send a [DONE] event so the client knows it's finished.
    """
    words = text.split(" ")
    yield _sse("token", "")
    for i, word in enumerate(words):
        chunk = word if i == 0 else " " + word
        yield _sse("token", chunk)
        time.sleep(0.01)
    yield _sse("done", json.dumps({"model_used": model_used}))


def _stream_gemini(messages: list[dict], model: str, user_id: int, user_message: str):
    """
    Open a Gemini streaming request and yield tokens as SSE events.
    Accumulates the full reply, cleans it, then saves to memory.
    """
    full_reply = ""
    try:
        for token in gemini_stream_chat(messages, model=model):
            token = token.replace("$", "₹")
            token = re.sub(r"\*+", "", token)
            token = re.sub(r"#+\s*", "", token)
            full_reply += token
            yield _sse("token", token)

        cleaned = _clean(full_reply)
        cleaned = _trim_sentences(cleaned, max_count=3)

        history = conversation_memory.get(user_id, [])
        history.append({"role": "user",      "content": user_message})
        history.append({"role": "assistant", "content": cleaned})
        conversation_memory[user_id] = history[-12:]

    except RuntimeError:
        yield _sse("error", "Gemini API key is not configured. Set GEMINI_API_KEY in .env.")
        model = "offline"
    except requests.exceptions.ConnectionError:
        yield _sse("error", "Couldn't reach Gemini — check your connection and try again.")
        model = "offline"
    except requests.exceptions.Timeout:
        yield _sse("error", "The response timed out — please try again.")
        model = "timeout"
    except requests.exceptions.HTTPError as exc:
        traceback.print_exc()
        status = exc.response.status_code if exc.response is not None else "unknown"
        yield _sse("error", f"Gemini request failed ({status}) — please try again.")
        model = "error"
    except Exception:
        traceback.print_exc()
        yield _sse("error", "Something went wrong — please try again in a moment.")
        model = "error"

    yield _sse("done", json.dumps({"model_used": model}))


# ─────────────────────────────────────────────────────────────────
# Main endpoint
# ─────────────────────────────────────────────────────────────────

@chat_bp.route("/chat", methods=["POST"])
@login_required
def chat():
    user_id = session["user_id"]
    data    = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()

    if not message:
        def _empty():
            yield _sse("token", "Please type a message.")
            yield _sse("done", json.dumps({"model_used": "none"}))
        return Response(stream_with_context(_empty()),
                        mimetype="text/event-stream",
                        headers={"X-Accel-Buffering": "no",
                                 "Cache-Control": "no-cache"})

    conn = get_db()
    try:
        metrics = _fetch_full_metrics(conn, user_id)
    finally:
        conn.close()

    intent = _detect_intent(message)
    a      = _analyse(metrics, message, intent)

    fast_reply = _fast_path(intent, a)
    if fast_reply:
        return Response(
            stream_with_context(_stream_text(fast_reply.strip(), "instant")),
            mimetype="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    system_prompt = _build_prompt(a, intent, message)
    history       = conversation_memory.get(user_id, [])
    messages      = [
        {"role": "system", "content": system_prompt},
        *history[-12:],
        {"role": "user",   "content": message},
    ]
    model = GEMINI_MODEL

    return Response(
        stream_with_context(_stream_gemini(messages, model, user_id, message)),
        mimetype="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )