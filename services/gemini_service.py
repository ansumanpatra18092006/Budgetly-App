import json
import logging
import os
import re

import google.generativeai as genai

logger = logging.getLogger(__name__)

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "models/gemini-3.6-flash"
)

DEFAULT_MODEL = GEMINI_MODEL

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

def stream_chat(messages, model=None, **kwargs):
    model_name = model or DEFAULT_MODEL

    prompt = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if role == "system":
            prompt.append(f"System: {content}")
        elif role == "assistant":
            prompt.append(f"Assistant: {content}")
        else:
            prompt.append(f"User: {content}")

    prompt_text = "\n".join(prompt)

    gemini_model = genai.GenerativeModel(model_name)

    response = gemini_model.generate_content(
        prompt_text,
        stream=True
    )

    for chunk in response:
        try:
            if chunk.text:
                yield chunk.text
        except Exception:
            pass


def generate_json(system_instruction: str, user_payload: dict, model: str = None,
                   timeout_seconds: int = 30) -> dict:
    """
    Single-shot (non-streaming) call that requests a compact JSON response.

    Used for things like "explain this already-computed roadmap" — a
    one-off interpretation task, not a chat, so streaming isn't needed.

    Raises ValueError if the call fails or the response isn't valid JSON —
    callers are expected to catch this and fall back gracefully (the
    roadmap/goal feature must keep working without AI).
    """
    model_name = model or DEFAULT_MODEL

    prompt = (
        f"{system_instruction}\n\n"
        # default=str: caller payloads may contain non-JSON-native types
        # (e.g. datetime.date from a DB row) — stringify rather than crash.
        f"Input data (JSON):\n{json.dumps(user_payload, ensure_ascii=False, default=str)}\n\n"
        "Respond with ONLY a single JSON object. No markdown fences, no preamble."
    )

    try:
        gemini_model = genai.GenerativeModel(model_name)
        response = gemini_model.generate_content(
            prompt,
            stream=False,
            request_options={"timeout": timeout_seconds},
        )
        text = (response.text or "").strip()
    except Exception as exc:
        raise ValueError(f"Gemini call failed: {exc}") from exc

    cleaned = _JSON_FENCE_RE.sub("", text).strip()

    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Gemini returned non-JSON response: %r", text[:500])
        raise ValueError("Gemini returned a malformed response") from exc