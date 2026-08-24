# Drop this into your Django project (e.g. products/views.py or a new
# api.py in that app), and wire it into urls.py as:
#   path("api/products/create", publish_product_api)
#
# Matches your real Product / ProductImage models exactly (name, description,
# price, category, image, ProductImage gallery). Follows the same
# header-secret pattern you already use for CRON_SECRET (hmac.compare_digest,
# constant-time comparison).

import hmac
import os
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

PUBLISH_SECRET = os.environ["DJANGO_PUBLISH_SECRET"]


@csrf_exempt  # exempt because auth is via header secret, not session/cookie
@require_POST
def publish_product_api(request):
    provided = request.headers.get("X-Publish-Secret", "")
    if not hmac.compare_digest(provided, PUBLISH_SECRET):
        return JsonResponse({"error": "unauthorized"}, status=401)

    from .models import Product, ProductImage

    valid_categories = {c[0] for c in Product.CATEGORIES}
    category = request.POST.get("category", "Other")
    if category not in valid_categories:
        category = "Other"

    try:
        price = request.POST.get("price")
        price = float(price) if price else 0
    except ValueError:
        price = 0

    product = Product.objects.create(
        name=request.POST.get("name", "")[:100],
        description=request.POST.get("description", ""),
        price=price,
        category=category,
        image=request.FILES.get("image"),  # main product photo
    )

    for f in request.FILES.getlist("gallery_images"):
        ProductImage.objects.create(product=product, image=f)

    # NOTE: verify this matches your actual product detail URL pattern
    # (check urls.py) -- this is a guess based on the slug field.
    return JsonResponse({"ok": True, "product_id": product.id, "url": f"/product/{product.slug}/"})
