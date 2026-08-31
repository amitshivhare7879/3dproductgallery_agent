import json

# Must match Product.CATEGORIES exactly (models.py) — the model is
# constrained to pick one of these, since the field is a fixed-choice
# dropdown, not freeform text.
CATEGORY_CHOICES = ["Electronics", "Accessories", "Home", "Fashion", "Toys", "Robotics", "Other"]

PROMPT_TEMPLATE = """You are helping fill out a product listing for
3dproductgallery.in, a store selling 3D-printed products. Based on the
attached photo(s) and the seller's note, produce a JSON object with
exactly these fields:

- name: short, catchy product name (max 100 chars)
- description: customer-facing description. {length_instruction}
  {dims_instruction} {quantity_instruction}
- category: MUST be exactly one of: {categories}
- suggested_price: your best-guess price in INR (integer, no currency symbol)
  based on what similar 3D-printed items sell for. {price_instruction}

Seller's note: "{note}"
{model_info}
{extra}

Respond with ONLY the JSON object, no markdown fences, no extra text.
"""


def build_prompt(note: str, edit_instruction: str | None, previous: dict | None,
                  model_stats: dict | None = None) -> str:
    extra = ""
    if edit_instruction and previous:
        extra = (
            f"\nThe seller previously got this draft: {json.dumps(previous)}\n"
            f"They want this change applied: \"{edit_instruction}\"\n"
            f"Return the full updated JSON object with that change incorporated."
        )

    if model_stats and model_stats.get("dims_cm"):
        w, d, h = model_stats["dims_cm"]
        model_info = (
            f"\nMeasured from the actual 3D model file: dimensions are "
            f"{w} x {d} x {h} cm (W x D x H), estimated print weight "
            f"{model_stats.get('weight_g', '?')}g."
        )
        dims_instruction = (
            f"You MUST mention the exact dimensions ({w} x {d} x {h} cm) "
            f"naturally in the description."
        )
        price_instruction = (
            "A price will be calculated separately from the actual print "
            "weight, so this field is just a fallback -- still give your "
            "best estimate."
        )
    else:
        model_info = ""
        dims_instruction = "No 3D file was provided, so do not state specific dimensions."
        price_instruction = "No weight data is available, so this estimate will be used directly."

    note_word_count = len((note or "").split())
    if note_word_count > 40:
        length_instruction = (
            "The seller wrote a long, detailed note -- DO NOT compress it "
            "into a couple of generic sentences. Rewrite it for clarity, "
            "grammar, and flow, but preserve their key selling points, "
            "specific details, use cases, and tone. The result should be "
            "comparably rich and detailed to what they wrote, not a summary."
        )
    else:
        length_instruction = "Keep it to 2-4 sentences, since the seller's note was brief."

    quantity_instruction = (
        "If the photo(s) show multiple identical or matching items (a set, "
        "pair, or multi-piece design), explicitly state the quantity in "
        "both the name and description (e.g. 'Set of 3', 'Pair of', "
        "'2-Piece'). If it's a single item, don't mention quantity at all."
    )

    return PROMPT_TEMPLATE.format(
        categories=", ".join(CATEGORY_CHOICES),
        note=note or "(no note given)",
        extra=extra,
        model_info=model_info,
        dims_instruction=dims_instruction,
        price_instruction=price_instruction,
        length_instruction=length_instruction,
        quantity_instruction=quantity_instruction,
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
