import json

# Must match Product.CATEGORIES exactly (models.py) — the model is
# constrained to pick one of these, since the field is a fixed-choice
# dropdown, not freeform text.
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


def build_prompt(note: str, edit_instruction: str | None, previous: dict | None) -> str:
    extra = ""
    if edit_instruction and previous:
        extra = (
            f"\nThe seller previously got this draft: {json.dumps(previous)}\n"
            f"They want this change applied: \"{edit_instruction}\"\n"
            f"Return the full updated JSON object with that change incorporated."
        )
    return PROMPT_TEMPLATE.format(
        categories=", ".join(CATEGORY_CHOICES),
        note=note or "(no note given)",
        extra=extra,
    )


def parse_and_validate(text: str) -> dict:
    text = text.strip()
    # Guard against the model wrapping the JSON in ```json fences anyway
    if text.startswith("```"):
        text = text.strip("`")
        text = text.replace("json\n", "", 1) if text.startswith("json\n") else text

    data = json.loads(text)

    # Safety net: if the model picks something outside the fixed list, fall
    # back to "Other" rather than letting an invalid category break the
    # Django save.
    if data.get("category") not in CATEGORY_CHOICES:
        data["category"] = "Other"

    return data
