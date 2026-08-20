import os
import google.generativeai as genai

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "models/gemini-3.6-flash"
)

DEFAULT_MODEL = GEMINI_MODEL

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