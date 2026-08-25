# Drop this in as products/api.py (or wherever `api` resolves to given your
# urls.py: `path('api/products/create', api.publish_product_api, ...)`).
#
# Matches your real Product / ProductImage models exactly (name, description,
# price, category, image, ProductImage gallery). Follows the same
# header-secret pattern you already use for CRON_SECRET (hmac.compare_digest,
# constant-time comparison).

import hmac
import os
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

PUBLISH_SECRET = os.environ["DJANGO_PUBLISH_SECRET"]


def _check_secret(request):
    provided = request.headers.get("X-Publish-Secret", "")
    return hmac.compare_digest(provided, PUBLISH_SECRET)


def _clean_category(Product, raw):
    valid = {c[0] for c in Product.CATEGORIES}
    return raw if raw in valid else "Other"


def _clean_price(raw):
    try:
        return float(raw) if raw else 0
    except ValueError:
        return 0


@csrf_exempt  # exempt because auth is via header secret, not session/cookie
@require_POST
def publish_product_api(request):
    if not _check_secret(request):
        return JsonResponse({"error": "unauthorized"}, status=401)

    from .models import Product, ProductImage

    product = Product.objects.create(
        name=request.POST.get("name", "")[:100],
        description=request.POST.get("description", ""),
        price=_clean_price(request.POST.get("price")),
        category=_clean_category(Product, request.POST.get("category", "Other")),
        image=request.FILES.get("image"),  # main product photo
    )

    for f in request.FILES.getlist("gallery_images"):
        ProductImage.objects.create(product=product, image=f)

    # NOTE: verify this matches your actual product detail URL pattern.
    return JsonResponse({"ok": True, "product_id": product.id, "url": f"/product/{product.slug}/"})


@csrf_exempt
@require_POST
def edit_product_api(request, product_id):
    if not _check_secret(request):
        return JsonResponse({"error": "unauthorized"}, status=401)

    from .models import Product, ProductImage

    product = get_object_or_404(Product, id=product_id)

    # Only update fields that were actually sent, so a partial edit (e.g.
    # just changing the price) doesn't wipe out the rest.
    if "name" in request.POST:
        product.name = request.POST["name"][:100]
    if "description" in request.POST:
        product.description = request.POST["description"]
    if "price" in request.POST:
        product.price = _clean_price(request.POST["price"])
    if "category" in request.POST:
        product.category = _clean_category(Product, request.POST["category"])
    if request.FILES.get("image"):
        product.image = request.FILES["image"]

    product.save()

    for f in request.FILES.getlist("gallery_images"):
        ProductImage.objects.create(product=product, image=f)

    return JsonResponse({"ok": True, "product_id": product.id, "url": f"/product/{product.slug}/"})
