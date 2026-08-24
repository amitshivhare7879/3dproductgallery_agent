import os
import httpx

DJANGO_API_BASE_URL = os.environ["DJANGO_API_BASE_URL"]
DJANGO_PUBLISH_SECRET = os.environ["DJANGO_PUBLISH_SECRET"]

# Rough cost-per-gram estimate for PLA prints, used only when an STL was
# provided so we have a weight to work from. Adjust to match your actual
# material + printing cost. This OVERRIDES Gemini's guess when a weight is
# available, since it's grounded in the real print rather than a vibe check.
PRICE_PER_GRAM_INR = float(os.environ.get("PRICE_PER_GRAM_INR", "6"))


async def publish_product(draft) -> dict:
    """
    Sends the finished draft to the Django site's internal publish endpoint.
    Mirrors the header-only secret auth pattern already used for CRON_SECRET
    in the Django project (hmac.compare_digest on the server side).
    """
    url = f"{DJANGO_API_BASE_URL}/api/products/create"
    headers = {"X-Publish-Secret": DJANGO_PUBLISH_SECRET}

    gen = draft.generated or {}
    stats = draft.stl_stats or {}

    price = gen.get("suggested_price")
    if stats.get("weight_g"):
        price = round(stats["weight_g"] * PRICE_PER_GRAM_INR)

    data = {
        "name": gen.get("name"),
        "description": gen.get("description"),
        "category": gen.get("category"),
        "price": price,
    }

    files = []
    if draft.images:
        # First photo -> Product.image, any additional ones -> ProductImage gallery
        files.append(("image", ("main.jpg", open(draft.images[0], "rb"), "image/jpeg")))
        for i, path in enumerate(draft.images[1:]):
            files.append(("gallery_images", (f"image_{i}.jpg", open(path, "rb"), "image/jpeg")))

    async with httpx.AsyncClient() as client:
        r = await client.post(url, headers=headers, data=data, files=files, timeout=60)
        r.raise_for_status()
        return r.json()
