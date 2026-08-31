import os

# Rough INR cost per gram of PLA print. Adjust to match your real material +
# printing cost. Used whenever a model file (STL/3MF) was provided AND the
# seller hasn't explicitly overridden the price.
PRICE_PER_GRAM_INR = float(os.environ.get("PRICE_PER_GRAM_INR", "8"))


def compute_price(model_stats: dict | None, ai_suggested_price, manual_override: float | None = None) -> int:
    """
    Priority order: an explicit price the seller typed always wins, even
    over a weight-based calculation -- "change price to X" should mean X,
    full stop, not get silently recomputed from the STL's weight.
    """
    if manual_override is not None:
        return round(manual_override)
    if model_stats and model_stats.get("weight_g"):
        return round(model_stats["weight_g"] * PRICE_PER_GRAM_INR)
    try:
        return round(float(ai_suggested_price))
    except (TypeError, ValueError):
        return 0
