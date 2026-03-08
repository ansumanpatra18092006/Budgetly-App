"""
chatbot.py — Budgetly AI Financial Assistant (Streaming Edition)
=================================================================
Local LLM chatbot powered by Ollama (phi3 / mistral).

Design philosophy:
  - Python does ALL the financial math first — LLM only writes plain English
  - Intent-specific prompts so the model knows exactly what to say
  - Fast-path covers ~60% of questions with instant deterministic answers
    (fast-path responses are still streamed word-by-word for consistent UX)
  - Replies are always 2–3 short sentences, skimmable at a glance
  - No markdown, no jargon, no filler phrases

Register in app.py:
    from routes.chatbot import chat_bp
    app.register_blueprint(chat_bp)
"""

from __future__ import annotations

import json
import re
import time
import traceback

import requests
from flask import Blueprint, Response, request, session, stream_with_context

from routes.ai_insights import _fetch_full_metrics
from utils.db import get_db
from utils.decorators import login_required

# ─────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────

chat_bp    = Blueprint("chat", __name__)
OLLAMA_URL = "http://localhost:11434/api/chat"

# { user_id: [{role: ..., content: ...}, ...] }
conversation_memory: dict[int, list[dict]] = {}


# ─────────────────────────────────────────────────────────────────
# Intent detection  (unchanged)
# ─────────────────────────────────────────────────────────────────

_INTENT_MAP: list[tuple[str, list[str]]] = [
    ("affordability", ["afford", "can i buy", "can i get", "enough to buy",
                       "can i spend", "is it okay to buy", "should i buy"]),
    ("saving_advice", ["save more", "how to save", "saving tips", "increase savings",
                       "save faster", "better at saving"]),
    ("goal_status",   ["goal", "target", "milestone", "on track", "how long",
                       "when will i", "reach my goal"]),
    ("overspending",  ["overspend", "spending too much", "why so much",
                       "where is my money", "where does my money go"]),
    ("reduce_spend",  ["reduce", "cut", "lower", "decrease", "trim",
                       "spend less", "save on"]),
    ("budget_status", ["budget", "limit", "how much left", "remaining budget",
                       "budget status"]),
    ("investment",    ["invest", "mutual fund", "stocks", "sip", "fd",
                       "fixed deposit", "where to put"]),
    ("summary",       ["summary", "overview", "how am i doing", "financial health",
                       "my finances", "overall"]),
]


def _detect_intent(message: str) -> str:
    msg = message.lower()
    for intent, keywords in _INTENT_MAP:
        if any(kw in msg for kw in keywords):
            return intent
    return "general"


# ─────────────────────────────────────────────────────────────────
# Financial analysis  (unchanged)
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
# Fast-path  (unchanged logic — now streamed word-by-word)
# ─────────────────────────────────────────────────────────────────

def _fast_path(intent: str, a: dict) -> str | None:
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
# System prompt  (unchanged)
# ─────────────────────────────────────────────────────────────────

def _build_prompt(a: dict, intent: str, message: str) -> str:
    goals_text = ""
    for g in a["goals_summary"]:
        mths = f", ~{g['months_away']} months away" if g["months_away"] else ""
        goals_text += f"  {g['name']}: {g['pct']:.0f}% done (₹{g['saved']:,}/₹{g['target']:,}{mths})\n"
    if not goals_text:
        goals_text = "  None set.\n"

    intent_task = {
        "saving_advice": (
            f"The user wants to save more. Their savings rate is {a['savings_rate']:.0f}% "
            f"(target: 20%+). Top spend is {a['top_cat']} at {a['top_pct']:.0f}%. "
            f"Give 1 specific, actionable saving tip using their real numbers."
        ),
        "investment": (
            f"The user wants to invest their ₹{int(a['surplus']):,}/month surplus. "
            f"Suggest 1–2 beginner-friendly Indian investment options (SIP, FD, etc.) "
            f"with realistic, brief guidance."
        ),
        "general": (
            "Answer the user's question using their real financial data. "
            "Be direct and specific. Do not give generic advice."
        ),
    }.get(intent, "Answer the user's question using their real financial data. Be direct and specific.")

    return f"""You are Budgetly AI — a smart, friendly personal finance assistant for Indian users.

REPLY FORMAT (follow exactly — 3 sentences, no more, no less):
  Sentence 1 — Observation: State one clear insight about the user's finances using their real numbers.
  Sentence 2 — Action:      Tell the user one specific action they can take immediately.
  Sentence 3 — Benefit:     Explain the positive result of that action in ₹ or percentage.

RULES:
- Exactly 3 sentences. Never fewer, never more.
- Plain, simple English. No financial jargon.
- No bullet points, no lists, no markdown, no labels like "Observation:".
- Use ₹ for all amounts. Never $.
- Use only the numbers provided below — never invent figures.

USER'S FINANCIAL DATA (already calculated — use these directly):
  Income:         ₹{int(a['income']):,}
  Expenses:       ₹{int(a['expense']):,}
  Surplus:        ₹{int(a['surplus']):,}
  Savings rate:   {a['savings_rate']:.0f}% — {a['savings_verdict']}
  Budget used:    {a['budget_used_pct']:.0f}% — {a['budget_verdict']}, ₹{a['budget_left']:,.0f} left
  Spending trend: {a['trend']}
  Top category:   {a['top_cat']} = {a['top_pct']:.0f}% of expenses = ₹{a['top_cat_amount']:,}
  10% cut saves:  ₹{a['top_cat_cut_10']:,}/month
  15% cut saves:  ₹{a['top_cat_cut_15']:,}/month
  Safe to spend:  ₹{a['safe_spend_40']:,} comfortably

GOALS:
{goals_text}
TASK: {intent_task}

User said: "{message}"

Your reply (exactly 3 sentences — Observation, Action, Benefit — no labels, plain English):"""


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
# SSE helpers
# ─────────────────────────────────────────────────────────────────

def _sse(event: str, data: str) -> str:
    """Format a single Server-Sent Event line."""
    # Escape newlines inside the data field so SSE framing stays intact
    safe = data.replace("\n", "\\n")
    return f"event:{event}\ndata:{safe}\n\n"


def _stream_text(text: str, model_used: str = "instant"):
    """
    Yield a pre-computed string word-by-word as SSE tokens,
    then send a [DONE] event so the client knows it's finished.
    Used for fast-path replies so the UX is identical to LLM streaming.
    """
    words = text.split(" ")
    for i, word in enumerate(words):
        chunk = word if i == 0 else " " + word
        yield _sse("token", chunk)
        time.sleep(0.018)   # ~55 words/sec — feels natural, not laggy
    yield _sse("done", json.dumps({"model_used": model_used}))


def _stream_ollama(messages: list[dict], model: str, user_id: int, user_message: str):
    """
    Open an Ollama streaming request and yield tokens as SSE events.
    Accumulates the full reply, cleans it, then saves to memory.
    """
    full_reply = ""
    try:
        with requests.post(
            OLLAMA_URL,
            json={
                "model":    model,
                "stream":   True,
                "messages": messages,
                "options": {
                    "temperature":    0.25,
                    "top_p":          0.85,
                    "num_predict":    160,
                    "repeat_penalty": 1.2,
                    "stop": ["User:", "User said:", "\n\n\n"],
                },
            },
            stream=True,
            timeout=60,
        ) as resp:
            resp.raise_for_status()
            for raw_line in resp.iter_lines():
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                token = obj.get("message", {}).get("content", "")
                if token:
                    # Apply light cleaning per-token (strip $ → ₹, markdown)
                    token = token.replace("$", "₹")
                    token = re.sub(r"\*+", "", token)
                    token = re.sub(r"#+\s*", "", token)
                    full_reply += token
                    yield _sse("token", token)

                if obj.get("done"):
                    break

        # Post-process the complete accumulated reply
        cleaned = _clean(full_reply)
        cleaned = _trim_sentences(cleaned, max_count=3)

        # Save to memory
        history = conversation_memory.get(user_id, [])
        history.append({"role": "user",      "content": user_message})
        history.append({"role": "assistant", "content": cleaned})
        conversation_memory[user_id] = history[-12:]

    except requests.exceptions.ConnectionError:
        yield _sse("error", "Ollama is not running. Start it with: ollama serve")
        model = "offline"
    except requests.exceptions.Timeout:
        yield _sse("error", "The response timed out — please try again.")
        model = "timeout"
    except Exception:
        traceback.print_exc()
        yield _sse("error", "Something went wrong — please try again in a moment.")
        model = "error"

    yield _sse("done", json.dumps({"model_used": model}))


# ─────────────────────────────────────────────────────────────────
# Main endpoint  — now returns text/event-stream
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

    # ── Single DB call — all metrics at once ──────────────────────
    conn = get_db()
    try:
        metrics = _fetch_full_metrics(conn, user_id)
    finally:
        conn.close()

    # ── Intent + pre-computed analysis ───────────────────────────
    intent = _detect_intent(message)
    a      = _analyse(metrics, message, intent)

    # ── Fast-path: stream pre-computed answer word-by-word ────────
    fast_reply = _fast_path(intent, a)
    if fast_reply:
        return Response(
            stream_with_context(_stream_text(fast_reply.strip(), "instant")),
            mimetype="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    # ── LLM path ─────────────────────────────────────────────────
    system_prompt = _build_prompt(a, intent, message)
    history       = conversation_memory.get(user_id, [])
    messages      = [
        {"role": "system", "content": system_prompt},
        *history[-12:],
        {"role": "user",   "content": message},
    ]
    model = "mistral" if len(message) >= 120 else "phi3"

    return Response(
        stream_with_context(_stream_ollama(messages, model, user_id, message)),
        mimetype="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )