import os
import google.generativeai as genai

from .listing_prompt import build_prompt, parse_and_validate

genai.configure(api_key=os.environ["GEMINI_API_KEY"])

MODEL_NAME = "gemini-3.1-flash-lite"  # gemini-2.0-flash was shut down June 2026


def generate_listing(images: list[bytes], note: str, edit_instruction: str | None = None,
                      previous: dict | None = None) -> dict:
    prompt = build_prompt(note, edit_instruction, previous)

    model = genai.GenerativeModel(MODEL_NAME)
    parts = [prompt] + [{"mime_type": "image/jpeg", "data": img} for img in images]

    response = model.generate_content(parts)
    return parse_and_validate(response.text)
