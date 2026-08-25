import os

# Rough INR cost per gram of PLA print. Adjust to match your real material +
# printing cost. Used whenever a model file (STL/3MF) was provided; falls
# back to the AI's guess only when no file was sent.
PRICE_PER_GRAM_INR = float(os.environ.get("PRICE_PER_GRAM_INR", "8"))


def compute_price(model_stats: dict | None, ai_suggested_price) -> int:
    if model_stats and model_stats.get("weight_g"):
        return round(model_stats["weight_g"] * PRICE_PER_GRAM_INR)
    try:
        return round(float(ai_suggested_price))
    except (TypeError, ValueError):
        return 0
