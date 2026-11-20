import math

from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_http_methods

from ..catalog import BUSINESS_TYPES_BY_KEY
from ..helpers.request_helpers import (
    parse_json_object,
    parse_positive_int,
    require_workspace_member,
)
from ..models import AuditLog, Workspace
from ..serializers import serialize_workspace_and_user
from ..service.audit_service import (
    VALID_ACTOR_KINDS,
    record_audit_event,
    serialize_audit_entry,
)
from ..service.catalog_service import (
    CatalogError,
    enable_pack_for_workspace,
    serialize_workspace_packs_detail,
    set_workspace_table_enabled,
)
from ..service.pipeline_service import is_deal_table_enabled
from ..service.role_service import (
    ALL_OBJECT_KEYS,
    user_can_manage_users_and_roles,
    user_can_manage_workspace_catalog,
    user_has_object_right,
)

DEFAULT_AUDIT_PAGE_SIZE = 20
MAX_AUDIT_PAGE_SIZE = 100
PRIVATE_SECURITY_OBJECT_TYPES = ('workspace_invite', 'user', 'external_identity', 'passkey')


def _require_catalog_management_permission(request, slug: str):
    user, workspace, error_response = require_workspace_member(request, slug)
    if error_response is not None:
        return None, None, error_response
    if not user_can_manage_workspace_catalog(user, workspace):
        return (
            None,
            None,
            JsonResponse(
                {
                    'error': (
                        'Only the workspace Owner or a role with users-and-roles or '
                        'theme-and-layouts access can update workspace settings and packs.'
                    )
                },
                status=403,
            ),
        )
    return user, workspace, None


@require_http_methods(["GET", "PATCH"])
def workspace_detail_view(request, slug):
    user, workspace, error_response = require_workspace_member(request, slug)
    if error_response is not None:
        return error_response

    if request.method == "PATCH":
        return update_workspace_profile_response(request, user, workspace)

    return JsonResponse(
        serialize_workspace_and_user(workspace, user),
        status=200,
    )


def update_workspace_profile_response(request, user, workspace):
    """Update business_type_label and/or contact_label for a workspace."""
    if not user_can_manage_workspace_catalog(user, workspace):
        return JsonResponse(
            {
                'error': (
                    'Only the workspace Owner or a role with users-and-roles or '
                    'theme-and-layouts access can update workspace labels.'
                )
            },
            status=403,
        )

    payload, parse_error = parse_json_object(request)
    if parse_error is not None:
        return parse_error

    has_bt_label = 'business_type_label' in payload
    has_contact_label = 'contact_label' in payload
    reset_contact = payload.get('reset_contact_label') is True

    if not has_bt_label and not has_contact_label and not reset_contact:
        return JsonResponse(
            {'error': 'Provide business_type_label or contact_label to update.'},
            status=400,
        )

    next_bt_label = None
    if has_bt_label:
        raw_bt_label = payload.get('business_type_label')
        if not isinstance(raw_bt_label, str) or not raw_bt_label.strip():
            return JsonResponse(
                {'error': 'Business-type label cannot be empty.'},
                status=400,
            )
        next_bt_label = ' '.join(raw_bt_label.strip().split())
        if len(next_bt_label) > 150:
            return JsonResponse(
                {'error': 'Business-type label must be 150 characters or fewer.'},
                status=400,
            )

    next_contact_label = None
    if reset_contact:
        primary_bt = BUSINESS_TYPES_BY_KEY.get(workspace.business_type)
        next_contact_label = primary_bt.default_contact_label if primary_bt else 'Contact'
    elif has_contact_label:
        raw_contact_label = payload.get('contact_label')
        if not isinstance(raw_contact_label, str) or not raw_contact_label.strip():
            return JsonResponse(
                {'error': 'Contact label cannot be empty.'},
                status=400,
            )
        next_contact_label = ' '.join(raw_contact_label.strip().split())
        if len(next_contact_label) > 80:
            return JsonResponse(
                {'error': 'Contact label must be 80 characters or fewer.'},
                status=400,
            )

    changed_fields = []
    with transaction.atomic():
        locked_ws = Workspace.objects.select_for_update().get(pk=workspace.pk)
        if next_bt_label is not None and next_bt_label != locked_ws.business_type_label:
            previous_bt_label = locked_ws.business_type_label
            locked_ws.business_type_label = next_bt_label
            changed_fields.append('business_type_label')
            record_audit_event(
                workspace=locked_ws,
                object_type='workspace',
                object_id=locked_ws.id,
                record_label=locked_ws.name,
                action=AuditLog.ACTION_UPDATE,
                field_name='business_type_label',
                previous_value=previous_bt_label,
                new_value=next_bt_label,
                actor_kind=AuditLog.ACTOR_KIND_PERSON,
                actor_user=user,
            )

        if next_contact_label is not None and next_contact_label != locked_ws.contact_label:
            previous_contact = locked_ws.contact_label
            locked_ws.contact_label = next_contact_label
            changed_fields.append('contact_label')
            record_audit_event(
                workspace=locked_ws,
                object_type='workspace',
                object_id=locked_ws.id,
                record_label=locked_ws.name,
                action=AuditLog.ACTION_UPDATE,
                field_name='contact_label',
                previous_value=previous_contact,
                new_value=next_contact_label,
                actor_kind=AuditLog.ACTOR_KIND_PERSON,
                actor_user=user,
            )

        if changed_fields:
            locked_ws.save(update_fields=changed_fields)

    response_payload = serialize_workspace_and_user(locked_ws, user)
    response_payload['changed'] = bool(changed_fields)
    response_payload['message'] = (
        'Workspace labels updated.'
        if changed_fields
        else 'Workspace labels unchanged.'
    )
    return JsonResponse(response_payload, status=200)


@require_http_methods(['GET', 'POST'])
def workspace_packs_view(request, slug: str):
    """List workspace packs and catalog tables (GET) or turn on another pack (POST)."""
    if request.method == 'GET':
        _user, workspace, error_response = require_workspace_member(request, slug)
        if error_response is not None:
            return error_response
        return JsonResponse(serialize_workspace_packs_detail(workspace), status=200)

    user, workspace, error_response = _require_catalog_management_permission(request, slug)
    if error_response is not None:
        return error_response

    payload, parse_error = parse_json_object(request)
    if parse_error is not None:
        return parse_error

    raw_pack_key = payload.get('pack_key') or payload.get('business_type')
    try:
        result = enable_pack_for_workspace(workspace, raw_pack_key, actor_user=user)
    except CatalogError as exc:
        return JsonResponse({'error': exc.message}, status=exc.status_code)

    detail = serialize_workspace_packs_detail(workspace)
    bt = result['business_type']
    added_count = len(result['newly_enabled_keys'])
    detail['enabled_pack_key'] = bt.key
    detail['newly_enabled_keys'] = result['newly_enabled_keys']
    if added_count > 0:
        detail['message'] = (
            f"{bt.label} pack enabled ({added_count} table"
            f"{'' if added_count == 1 else 's'} turned on)."
        )
    else:
        detail['message'] = f"{bt.label} pack tables are already enabled."
    return JsonResponse(detail, status=200)


@require_http_methods(['PATCH', 'POST'])
def workspace_table_detail_view(request, slug: str, object_key: str):
    """Enable or disable a single catalog table in the workspace (empty pack tables only)."""
    user, workspace, error_response = _require_catalog_management_permission(request, slug)
    if error_response is not None:
        return error_response

    payload, parse_error = parse_json_object(request)
    if parse_error is not None:
        return parse_error

    if 'is_enabled' not in payload or not isinstance(payload.get('is_enabled'), bool):
        return JsonResponse(
            {'error': 'is_enabled must be provided as a boolean.'},
            status=400,
        )

    is_enabled = payload['is_enabled']
    try:
        result = set_workspace_table_enabled(
            workspace,
            object_key,
            enabled=is_enabled,
            actor_user=user,
        )
    except CatalogError as exc:
        return JsonResponse({'error': exc.message}, status=exc.status_code)

    detail = serialize_workspace_packs_detail(workspace)
    target_table = next(
        (tbl for tbl in detail['tables'] if tbl['key'] == result['object_key']),
        None,
    )
    table_label = target_table['label'] if target_table else object_key
    detail['changed'] = result['changed']
    if result['changed']:
        detail['message'] = (
            f'{table_label} turned on.' if is_enabled else f'{table_label} turned off.'
        )
    else:
        detail['message'] = (
            f'{table_label} is already on.' if is_enabled else f'{table_label} is already off.'
        )
    return JsonResponse(detail, status=200)


@require_GET
def workspace_audit_view(request, slug: str):
    """Return paginated audit log entries for the workspace, scoped by caller permissions."""
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
        default=DEFAULT_AUDIT_PAGE_SIZE,
        name='page_size',
        maximum=MAX_AUDIT_PAGE_SIZE,
    )
    if size_error is not None:
        return size_error

    actor_kind = (request.GET.get('actor_kind') or '').strip().lower()
    if actor_kind and actor_kind not in VALID_ACTOR_KINDS:
        return JsonResponse({'error': 'Invalid actor_kind filter.'}, status=400)

    qs = AuditLog.objects.filter(workspace=workspace)
    if not user_can_manage_users_and_roles(user, workspace):
        unreadable_crm_types = {
            obj_key
            for obj_key in ALL_OBJECT_KEYS
            if not user_has_object_right(user, workspace, obj_key, 'read')
        }
        if 'deal' in unreadable_crm_types or not is_deal_table_enabled(workspace):
            unreadable_crm_types.update({'pipeline', 'pipeline_stage'})
        qs = qs.filter(
            Q(actor_user=user)
            | Q(object_type='user', object_id=str(user.id))
            | ~Q(object_type__in=PRIVATE_SECURITY_OBJECT_TYPES)
        ).exclude(object_type__in=unreadable_crm_types)

    if actor_kind:
        qs = qs.filter(actor_kind=actor_kind)

    qs = qs.order_by('-created_at', '-id')
    total_count = qs.count()
    total_pages = max(1, math.ceil(total_count / page_size)) if total_count else 1
    offset = (page - 1) * page_size
    entries = list(qs[offset : offset + page_size])

    return JsonResponse(
        {
            'results': [serialize_audit_entry(entry) for entry in entries],
            'total_count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': total_pages,
            'has_next': page < total_pages,
            'has_previous': page > 1,
        },
        status=200,
    )
