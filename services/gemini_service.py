import json
import logging
import os
import re
import time
from typing import Iterator

import requests

logger = logging.getLogger(__name__)

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
DEFAULT_MODEL = GEMINI_MODEL
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_STREAM_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent"

# Transient Gemini failures should not immediately surface as a lender-facing 502.
# 503 = temporary model unavailability/high demand; 429/5xx can also be transient.
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = max(0, int(os.getenv("GEMINI_MAX_RETRIES", "3")))
BASE_RETRY_DELAY = max(0.5, float(os.getenv("GEMINI_RETRY_BASE_DELAY", "1.5")))

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _api_key() -> str:
    """Return the Gemini API key used by the Gemini Developer API.

    GEMINI_API_KEY is intentionally preferred over GOOGLE_API_KEY because
    GOOGLE_API_KEY is commonly reused for other Google services and may be
    configured with a credential type that is not valid for the Gemini API.
    """
    source = "GEMINI_API_KEY"
    value = os.getenv("GEMINI_API_KEY", "").strip()

    if not value:
        source = "GOOGLE_API_KEY"
        value = os.getenv("GOOGLE_API_KEY", "").strip()

    if not value:
        raise ValueError(
            "Gemini API key is not configured. Set GEMINI_API_KEY "
            "(preferred) or GOOGLE_API_KEY in the environment."
        )

    lowered = value.lower()
    if (
        value.startswith("ya29.")
        or value.startswith("1//")
        or lowered.startswith("bearer ")
    ):
        raise ValueError(
            f"{source} contains an OAuth access token, not a Gemini API key. "
            "Create/copy a Gemini API key from Google AI Studio and set that "
            "value as GEMINI_API_KEY."
        )

    return value


def _model_name(model: str | None) -> str:
    value = (model or DEFAULT_MODEL).strip()
    if value.startswith("models/"):
        value = value[len("models/"):]
    return value


def _retry_delay(response: requests.Response | None, attempt: int) -> float:
    """Return a bounded retry delay, honoring Retry-After when supplied."""
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(15.0, max(0.5, float(retry_after)))
            except ValueError:
                pass

    # 1.5s, 3s, 6s... with a small cap.
    return min(15.0, BASE_RETRY_DELAY * (2 ** attempt))


def _post_with_retries(
    url: str,
    *,
    headers: dict,
    json_payload: dict,
    timeout_seconds: int,
    stream: bool = False,
    params: dict | None = None,
) -> requests.Response:
    """POST to Gemini and retry temporary service/rate-limit failures."""
    last_response: requests.Response | None = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.post(
                url,
                params=params,
                headers=headers,
                json=json_payload,
                stream=stream,
                timeout=timeout_seconds,
            )
        except requests.RequestException as exc:
            # Network-level failures are transient in many local/deployed cases.
            if attempt >= MAX_RETRIES:
                raise ValueError(f"Gemini network error: {exc}") from exc
            delay = _retry_delay(None, attempt)
            logger.warning(
                "Gemini network error on attempt %d/%d: %s; retrying in %.1fs",
                attempt + 1,
                MAX_RETRIES + 1,
                exc,
                delay,
            )
            time.sleep(delay)
            continue

        last_response = response
        if response.status_code < 400:
            return response

        if response.status_code not in RETRYABLE_STATUS_CODES or attempt >= MAX_RETRIES:
            detail = response.text[:1000]
            response.close()

            if response.status_code == 401:
                raise ValueError(
                    "Gemini authentication failed (HTTP 401). The configured "
                    "Gemini credential is invalid or is the wrong credential type. "
                    "Set GEMINI_API_KEY to a Gemini API key from Google AI Studio "
                    "(not an OAuth access token), then redeploy on Render. "
                    f"Google response: {detail}"
                )

            raise ValueError(f"Gemini API HTTP {response.status_code}: {detail}")

        delay = _retry_delay(response, attempt)
        logger.warning(
            "Gemini returned HTTP %d on attempt %d/%d; retrying in %.1fs",
            response.status_code,
            attempt + 1,
            MAX_RETRIES + 1,
            delay,
        )
        response.close()
        time.sleep(delay)

    # Defensive fallback; the loop above always returns or raises.
    if last_response is not None:
        detail = last_response.text[:1000]
        last_response.close()
        raise ValueError(
            f"Gemini API HTTP {last_response.status_code}: {detail}"
        )
    raise ValueError("Gemini request failed without a response")


def _post_json(
    model: str,
    contents: list[dict],
    *,
    generation_config: dict | None = None,
    timeout_seconds: int = 30,
) -> dict:
    key = _api_key()
    payload: dict = {"contents": contents}
    if generation_config:
        payload["generationConfig"] = generation_config

    response = _post_with_retries(
        GEMINI_API_URL.format(model=_model_name(model)),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": key,
        },
        json_payload=payload,
        timeout_seconds=timeout_seconds,
    )

    try:
        return response.json()
    except ValueError as exc:
        raise ValueError("Gemini API returned invalid JSON") from exc
    finally:
        response.close()


def _extract_text(payload: dict) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        return ""
    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    return "".join(
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


def stream_chat(messages, model=None, **kwargs) -> Iterator[str]:
    prompt = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        prompt.append(
            f"System: {content}" if role == "system" else
            f"Assistant: {content}" if role == "assistant" else
            f"User: {content}"
        )

    key = _api_key()
    timeout = int(kwargs.get("timeout_seconds", 60))
    response = _post_with_retries(
        GEMINI_STREAM_URL.format(model=_model_name(model)),
        params={"alt": "sse"},
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "x-goog-api-key": key,
        },
        json_payload={"contents": [{"parts": [{"text": "\n".join(prompt)}]}]},
        stream=True,
        timeout_seconds=timeout,
    )

    try:
        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            line = raw_line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                break
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = _extract_text(payload)
            if text:
                yield text
    finally:
        response.close()


def generate_json(
    system_instruction: str,
    user_payload: dict,
    model: str = None,
    timeout_seconds: int = 30,
) -> dict:
    prompt = (
        f"{system_instruction}\n\n"
        f"Input data (JSON):\n{json.dumps(user_payload, ensure_ascii=False, default=str)}\n\n"
        "Respond with ONLY a single JSON object. No markdown fences, no preamble."
    )

    try:
        payload = _post_json(
            model or DEFAULT_MODEL,
            [{"role": "user", "parts": [{"text": prompt}]}],
            generation_config={
                "responseMimeType": "application/json",
            },
            timeout_seconds=timeout_seconds,
        )
        text = _extract_text(payload).strip()
    except Exception as exc:
        raise ValueError(f"Gemini call failed: {exc}") from exc

    if not text:
        raise ValueError("Gemini returned an empty response")

    cleaned = _JSON_FENCE_RE.sub("", text).strip()
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Gemini returned non-JSON response: %r", text[:500])
        raise ValueError("Gemini returned a malformed response") from exc
