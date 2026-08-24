import os
import json
import google.generativeai as genai

genai.configure(api_key=os.environ["GEMINI_API_KEY"])

MODEL_NAME = "gemini-2.0-flash"  # free-tier friendly

# Must match Product.CATEGORIES exactly (models.py) — Gemini is constrained
# to pick one of these, since the field is a fixed-choice dropdown, not
# freeform text.
CATEGORY_CHOICES = ["Electronics", "Accessories", "Home", "Fashion", "Toys", "Robotics", "Other"]

PROMPT_TEMPLATE = """You are helping fill out a product listing for
3dproductgallery.in, a store selling 3D-printed products. Based on the
attached photo(s) and the seller's short note, produce a JSON object with
exactly these fields:

- name: short, catchy product name (max 100 chars)
- description: 2-4 sentence customer-facing description
- category: MUST be exactly one of: {categories}
- suggested_price: your best-guess price in INR (integer, no currency symbol)
  based on what similar 3D-printed items sell for -- this is a rough starting
  point, the seller will confirm or adjust it.

Seller's note: "{note}"
{extra}

Respond with ONLY the JSON object, no markdown fences, no extra text.
"""


def generate_listing(images: list[bytes], note: str, edit_instruction: str | None = None,
                      previous: dict | None = None) -> dict:
    extra = ""
    if edit_instruction and previous:
        extra = (
            f"\nThe seller previously got this draft: {json.dumps(previous)}\n"
            f"They want this change applied: \"{edit_instruction}\"\n"
            f"Return the full updated JSON object with that change incorporated."
        )

    prompt = PROMPT_TEMPLATE.format(
        categories=", ".join(CATEGORY_CHOICES),
        note=note or "(no note given)",
        extra=extra,
    )

    model = genai.GenerativeModel(MODEL_NAME)
    parts = [prompt] + [{"mime_type": "image/jpeg", "data": img} for img in images]

    response = model.generate_content(parts)
    text = response.text.strip()

    # Guard against the model wrapping the JSON in ```json fences anyway
    if text.startswith("```"):
        text = text.strip("`")
        text = text.replace("json\n", "", 1) if text.startswith("json\n") else text

    data = json.loads(text)

    # Safety net: if Gemini picks something outside the fixed list, fall back
    # to "Other" rather than letting an invalid category break the Django save.
    if data.get("category") not in CATEGORY_CHOICES:
        data["category"] = "Other"

    return data
