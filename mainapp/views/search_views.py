from django.http import JsonResponse
from django.views.decorators.http import require_GET

from ..helpers.crm_errors import CrmDomainError
from ..helpers.request_helpers import parse_positive_int, require_workspace_member
from ..service.search_service import (
    DEFAULT_SEARCH_PAGE_SIZE,
    MAX_SEARCH_PAGE_SIZE,
    search_workspace_records,
)


@require_GET
def workspace_search_view(request, slug: str):
    """Search across enabled, caller-readable CRM objects in the workspace with pagination."""
    user, workspace, error_response = require_workspace_member(request, slug)
    if error_response is not None:
        return error_response

    page, page_error = parse_positive_int(
        request.GET.get('page'), default=1, name='page'
    )
    if page_error is not None:
        return page_error

    page_size, size_error = parse_positive_int(
        request.GET.get('page_size'),
        default=DEFAULT_SEARCH_PAGE_SIZE,
        name='page_size',
        maximum=MAX_SEARCH_PAGE_SIZE,
    )
    if size_error is not None:
        return size_error

    try:
        payload = search_workspace_records(
            workspace,
            user,
            query=request.GET.get('q') or '',
            object_type=request.GET.get('object_type') or '',
            page=page,
            page_size=page_size,
        )
        return JsonResponse(payload, status=200)
    except CrmDomainError as exc:
        return JsonResponse({'error': exc.message}, status=exc.status_code)
