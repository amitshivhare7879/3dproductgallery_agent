import os
import base64
import json
import httpx

from .listing_prompt import build_prompt, parse_and_validate

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Free-tier vision model on Groq. Note: Groq's vision models currently only
# accept ONE image per request, unlike Gemini which takes several -- so on
# fallback we only send the first photo. Good enough for a draft the seller
# reviews anyway.
MODEL_NAME = "llama-3.2-11b-vision-preview"


def generate_listing(images: list[bytes], note: str, edit_instruction: str | None = None,
                      previous: dict | None = None) -> dict:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY not set -- cannot use Groq fallback")
    if not images:
        raise RuntimeError("Groq fallback needs at least one image")

    prompt = build_prompt(note, edit_instruction, previous)
    b64_image = base64.b64encode(images[0]).decode("utf-8")

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"}},
                ],
            }
        ],
        "temperature": 0.4,
    }
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}

    with httpx.Client() as client:
        r = client.post(GROQ_URL, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]

    return parse_and_validate(content)
