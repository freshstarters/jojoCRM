from django.http import JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from ..service.catalog_service import serialize_business_type_catalog


def hello_world(request):
    return JsonResponse({"message": "Hello World"}, status=200)


@require_GET
def business_type_catalog_view(request):
    """Return the public catalog of 5 core objects, 25 pack objects, and 20 business types."""
    return JsonResponse(serialize_business_type_catalog(), status=200)


@require_GET
@ensure_csrf_cookie
def csrf_token_view(request):
    return JsonResponse({"ok": True}, status=200)
