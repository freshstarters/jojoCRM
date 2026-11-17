from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_http_methods

from ..helpers.request_helpers import (
    parse_json_object,
    parse_positive_int,
    require_workspace_member,
)
from ..models import Activity
from ..service.crm_service import (
    CRM_MODELS_BY_KEY,
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    CrmDomainError,
    create_crm_record,
    delete_crm_record,
    ensure_object_permission,
    get_crm_options,
    get_crm_record_detail,
    get_workspace_dashboard_summary,
    list_crm_records,
    resolve_object_key,
    update_crm_record,
)
from ..service.lead_conversion_service import (
    convert_lead_record,
    get_lead_conversion_preview,
)


@require_GET
def workspace_dashboard_view(request, slug: str):
    """Return role- and pack-aware CRM dashboard counts, pipeline summary, and recent records."""
    user, workspace, error_response = require_workspace_member(request, slug)
    if error_response is not None:
        return error_response
    return JsonResponse(get_workspace_dashboard_summary(workspace, user), status=200)


@require_GET
def workspace_crm_options_view(request, slug: str):
    """Return workspace assignees, Accounts/Contacts summaries, and CRM choice constants."""
    user, workspace, error_response = require_workspace_member(request, slug)
    if error_response is not None:
        return error_response
    return JsonResponse(get_crm_options(workspace, user), status=200)


@require_http_methods(['GET', 'POST'])
def workspace_record_collection_view(request, slug: str, object_key: str):
    """List (GET) or create (POST) records for an enabled S2.1 CRM object."""
    user, workspace, error_response = require_workspace_member(request, slug)
    if error_response is not None:
        return error_response

    try:
        canonical_key = resolve_object_key(object_key)
        if request.method == 'GET':
            page, page_error = parse_positive_int(
                request.GET.get('page'), default=1, name='page'
            )
            if page_error is not None:
                return page_error

            page_size, size_error = parse_positive_int(
                request.GET.get('page_size'),
                default=DEFAULT_PAGE_SIZE,
                name='page_size',
                maximum=MAX_PAGE_SIZE,
            )
            if size_error is not None:
                return size_error

            result = list_crm_records(
                workspace,
                user,
                canonical_key,
                query_params=request.GET,
                page=page,
                page_size=page_size,
            )
            return JsonResponse(result, status=200)

        payload, parse_error = parse_json_object(request)
        if parse_error is not None:
            return parse_error
        result = create_crm_record(workspace, user, canonical_key, payload)
        return JsonResponse(result, status=201)
    except CrmDomainError as exc:
        return JsonResponse({'error': exc.message}, status=exc.status_code)


@require_http_methods(['GET', 'PATCH', 'PUT', 'DELETE'])
def workspace_record_detail_view(request, slug: str, object_key: str, record_id: int):
    """Retrieve (GET), update (PATCH/PUT), or delete (DELETE) a single CRM record."""
    user, workspace, error_response = require_workspace_member(request, slug)
    if error_response is not None:
        return error_response

    try:
        canonical_key = resolve_object_key(object_key)
        if request.method == 'GET':
            result = get_crm_record_detail(workspace, user, canonical_key, record_id)
            return JsonResponse(result, status=200)

        if request.method == 'DELETE':
            result = delete_crm_record(workspace, user, canonical_key, record_id)
            return JsonResponse(result, status=200)

        payload, parse_error = parse_json_object(request)
        if parse_error is not None:
            return parse_error
        result = update_crm_record(workspace, user, canonical_key, record_id, payload)
        return JsonResponse(result, status=200)
    except CrmDomainError as exc:
        return JsonResponse({'error': exc.message}, status=exc.status_code)


@require_http_methods(['GET', 'POST'])
def workspace_record_activities_view(request, slug: str, object_key: str, record_id: int):
    """List (GET) or log (POST) activities attached to a specific workspace record."""
    user, workspace, error_response = require_workspace_member(request, slug)
    if error_response is not None:
        return error_response

    try:
        target_key = resolve_object_key(object_key)
        if target_key not in Activity.VALID_TARGET_OBJECTS:
            return JsonResponse({'error': 'Activities cannot attach to this object.'}, status=404)

        ensure_object_permission(user, workspace, target_key, 'read')
        target_model = CRM_MODELS_BY_KEY[target_key]
        if not target_model.objects.filter(workspace=workspace, id=record_id).exists():
            return JsonResponse({'error': 'Target record not found.'}, status=404)

        if request.method == 'GET':
            page, page_error = parse_positive_int(
                request.GET.get('page'), default=1, name='page'
            )
            if page_error is not None:
                return page_error
            page_size, size_error = parse_positive_int(
                request.GET.get('page_size'),
                default=DEFAULT_PAGE_SIZE,
                name='page_size',
                maximum=MAX_PAGE_SIZE,
            )
            if size_error is not None:
                return size_error

            params = request.GET.copy()
            params['target_object_type'] = target_key
            params['target_object_id'] = str(record_id)
            result = list_crm_records(
                workspace,
                user,
                'activity',
                query_params=params,
                page=page,
                page_size=page_size,
            )
            return JsonResponse(result, status=200)

        payload, parse_error = parse_json_object(request)
        if parse_error is not None:
            return parse_error
        merged_payload = {
            **payload,
            'target_object_type': target_key,
            'target_object_id': record_id,
        }
        result = create_crm_record(workspace, user, 'activity', merged_payload)
        return JsonResponse(result, status=201)
    except CrmDomainError as exc:
        return JsonResponse({'error': exc.message}, status=exc.status_code)


@require_GET
def workspace_lead_conversion_preview_view(request, slug: str, record_id: int):
    """Return matching existing Accounts/Contacts, defaults, and capabilities for Lead conversion."""
    user, workspace, error_response = require_workspace_member(request, slug)
    if error_response is not None:
        return error_response

    try:
        result = get_lead_conversion_preview(workspace, user, record_id)
        return JsonResponse(result, status=200)
    except CrmDomainError as exc:
        return JsonResponse({'error': exc.message}, status=exc.status_code)


@require_http_methods(['POST'])
def workspace_lead_convert_view(request, slug: str, record_id: int):
    """Convert an open Lead into an Account, Contact, and Deal."""
    user, workspace, error_response = require_workspace_member(request, slug)
    if error_response is not None:
        return error_response

    raw_body = (request.body or b'').strip()
    if not raw_body or (
        request.content_type == 'multipart/form-data'
        and not request.POST
        and not request.FILES
    ):
        payload = {}
    else:
        payload, parse_error = parse_json_object(request)
        if parse_error is not None:
            return parse_error

    try:
        result = convert_lead_record(workspace, user, record_id, payload)
        return JsonResponse(result, status=200)
    except CrmDomainError as exc:
        return JsonResponse({'error': exc.message}, status=exc.status_code)
