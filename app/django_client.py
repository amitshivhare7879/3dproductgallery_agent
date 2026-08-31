import os
import httpx

from .pricing import compute_price

DJANGO_API_BASE_URL = os.environ["DJANGO_API_BASE_URL"]
DJANGO_PUBLISH_SECRET = os.environ["DJANGO_PUBLISH_SECRET"]


def _build_files(draft) -> list:
    """
    Reads image/video files fully into memory (fine at these small sizes)
    instead of passing open file handles to httpx -- avoids leaking file
    descriptors across requests, since nothing was closing them before.
    """
    files = []
    if draft.images:
        with open(draft.images[0], "rb") as f:
            files.append(("image", ("main.jpg", f.read(), "image/jpeg")))
        for i, path in enumerate(draft.images[1:]):
            with open(path, "rb") as f:
                files.append(("gallery_images", (f"image_{i}.jpg", f.read(), "image/jpeg")))
    if getattr(draft, "video_path", None):
        with open(draft.video_path, "rb") as f:
            files.append(("video", ("product.mp4", f.read(), "video/mp4")))
    return files


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
    price = compute_price(stats, gen.get("suggested_price"), getattr(draft, "manual_price", None))

    data = {
        "name": gen.get("name"),
        "description": gen.get("description"),
        "category": gen.get("category"),
        "price": price,
    }
    files = _build_files(draft)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        r = await client.post(url, headers=headers, data=data, files=files, timeout=60)
        r.raise_for_status()
        return r.json()


async def list_products(query: str = "") -> dict:
    url = f"{DJANGO_API_BASE_URL}/api/products/list"
    headers = {"X-Publish-Secret": DJANGO_PUBLISH_SECRET}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        r = await client.get(url, headers=headers, params={"q": query} if query else {}, timeout=30)
        r.raise_for_status()
        return r.json()


async def get_product(product_id: int) -> dict:
    url = f"{DJANGO_API_BASE_URL}/api/products/get/{product_id}"
    headers = {"X-Publish-Secret": DJANGO_PUBLISH_SECRET}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        r = await client.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        return r.json()


async def edit_product(product_id: int, draft) -> dict:
    url = f"{DJANGO_API_BASE_URL}/api/products/edit/{product_id}"
    headers = {"X-Publish-Secret": DJANGO_PUBLISH_SECRET}

    gen = draft.generated or {}
    stats = draft.stl_stats or {}
    data = {}
    if gen.get("name"):
        data["name"] = gen["name"]
    if gen.get("description"):
        data["description"] = gen["description"]
    if gen.get("category"):
        data["category"] = gen["category"]
    manual_price = getattr(draft, "manual_price", None)
    if gen.get("suggested_price") is not None or stats.get("weight_g") or manual_price is not None:
        data["price"] = compute_price(stats, gen.get("suggested_price"), manual_price)

    files = _build_files(draft)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        r = await client.post(url, headers=headers, data=data, files=files, timeout=60)
        r.raise_for_status()
        return r.json()


async def delete_product(product_id: int) -> dict:
    url = f"{DJANGO_API_BASE_URL}/api/products/delete/{product_id}"
    headers = {"X-Publish-Secret": DJANGO_PUBLISH_SECRET}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        r = await client.post(url, headers=headers, timeout=30)
        r.raise_for_status()
        return r.json()
