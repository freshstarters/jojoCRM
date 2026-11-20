from django.http import JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from ..service.cache_service import (
    build_cached_json_response,
    get_cached_business_type_catalog_bundle,
)
from ..service.catalog_service import serialize_business_type_catalog


def hello_world(request):
    return JsonResponse({"message": "Hello World"}, status=200)


@require_GET
def business_type_catalog_view(request):
    """Return the public catalog of 5 core objects, 25 pack objects, and 20 business types."""
    bundle = get_cached_business_type_catalog_bundle(serialize_business_type_catalog)
    return build_cached_json_response(
        request,
        body_text=bundle['body_text'],
        etag=bundle['etag'],
        cache_control='public, max-age=3600, stale-while-revalidate=86400',
        vary='Accept-Encoding',
        cache_hit=bundle['cache_hit'],
    )


@require_GET
@ensure_csrf_cookie
def csrf_token_view(request):
    return JsonResponse({"ok": True}, status=200)
