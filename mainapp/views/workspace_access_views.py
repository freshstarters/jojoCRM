from django.db import transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_http_methods

from ..catalog import OBJECTS_BY_KEY
from ..helpers.request_helpers import parse_json_object, require_workspace_member
from ..models import AuditLog, Role, User
from ..serializers import serialize_member, serialize_role
from ..service.audit_service import record_audit_event
from ..service.role_service import (
    CRUD_ACTIONS,
    count_active_seats,
    ensure_workspace_default_roles,
    is_workspace_owner,
    normalize_role_object_rights,
    user_can_manage_users_and_roles,
)
from ..service.session_service import invalidate_user_sessions

OWNER_PROTECTED_MESSAGE = 'The workspace Owner cannot be deactivated or removed.'


def _require_user_management_permission(request, slug: str):
    user, workspace, error_response = require_workspace_member(request, slug)
    if error_response is not None:
        return None, None, error_response
    if not user_can_manage_users_and_roles(user, workspace):
        return (
            None,
            None,
            JsonResponse(
                {
                    'error': (
                        'Only the workspace Owner or a role with users-and-roles '
                        'access can manage workspace members and roles.'
                    )
                },
                status=403,
            ),
        )
    return user, workspace, None


@require_http_methods(['POST'])
def workspace_member_deactivate_view(request, slug: str, member_id: int):
    """Deactivate a non-Owner member, end their active sessions, and exclude them from seats."""
    acting_user, workspace, error_response = _require_user_management_permission(request, slug)
    if error_response is not None:
        return error_response

    with transaction.atomic():
        member = (
            User.objects.select_for_update(of=('self',))
            .select_related('workspace', 'role')
            .filter(id=member_id, workspace=workspace)
            .first()
        )
        if member is None:
            return JsonResponse({'error': 'Workspace member not found.'}, status=404)

        if is_workspace_owner(member, workspace):
            return JsonResponse({'error': OWNER_PROTECTED_MESSAGE}, status=403)

        if member.id == acting_user.id:
            return JsonResponse(
                {'error': 'You cannot deactivate your own active account.'},
                status=400,
            )

        if not member.is_active:
            return JsonResponse(
                {'error': 'This member is already deactivated.'},
                status=400,
            )

        member.is_active = False
        member.save(update_fields=['is_active'])
        invalidate_user_sessions(member)
        record_audit_event(
            workspace=workspace,
            object_type='user',
            object_id=member.id,
            record_label=member.email,
            action=AuditLog.ACTION_UPDATE,
            field_name='status',
            previous_value='Active',
            new_value='Deactivated',
            actor_kind=AuditLog.ACTOR_KIND_PERSON,
            actor_user=acting_user,
        )

    return JsonResponse(
        {
            'member': serialize_member(member, workspace),
            'active_seat_count': count_active_seats(workspace),
            'message': f'{member.email} has been deactivated and signed out.',
        },
        status=200,
    )


@require_http_methods(['POST'])
def workspace_member_reactivate_view(request, slug: str, member_id: int):
    """Reactivate a deactivated non-Owner member and restore workspace access and seat count."""
    acting_user, workspace, error_response = _require_user_management_permission(request, slug)
    if error_response is not None:
        return error_response

    with transaction.atomic():
        member = (
            User.objects.select_for_update(of=('self',))
            .select_related('workspace', 'role')
            .filter(id=member_id, workspace=workspace)
            .first()
        )
        if member is None:
            return JsonResponse({'error': 'Workspace member not found.'}, status=404)

        if is_workspace_owner(member, workspace):
            return JsonResponse(
                {'error': 'The workspace Owner is always active.'},
                status=400,
            )

        if member.is_active:
            return JsonResponse(
                {'error': 'This member is already active.'},
                status=400,
            )

        member.is_active = True
        member.save(update_fields=['is_active'])
        record_audit_event(
            workspace=workspace,
            object_type='user',
            object_id=member.id,
            record_label=member.email,
            action=AuditLog.ACTION_UPDATE,
            field_name='status',
            previous_value='Deactivated',
            new_value='Active',
            actor_kind=AuditLog.ACTOR_KIND_PERSON,
            actor_user=acting_user,
        )

    return JsonResponse(
        {
            'member': serialize_member(member, workspace),
            'active_seat_count': count_active_seats(workspace),
            'message': f'{member.email} has been reactivated.',
        },
        status=200,
    )


@require_http_methods(['DELETE'])
def workspace_member_delete_view(request, slug: str, member_id: int):
    """Refuse Owner removal and direct non-Owner access changes through deactivation."""
    _acting_user, workspace, error_response = _require_user_management_permission(request, slug)
    if error_response is not None:
        return error_response

    member = (
        User.objects.select_related('role')
        .filter(id=member_id, workspace=workspace)
        .first()
    )
    if member is None:
        return JsonResponse({'error': 'Workspace member not found.'}, status=404)

    if is_workspace_owner(member, workspace):
        return JsonResponse({'error': OWNER_PROTECTED_MESSAGE}, status=403)

    return JsonResponse(
        {
            'error': (
                'Workspace members are deactivated rather than deleted so audit '
                'history remains intact.'
            )
        },
        status=400,
    )


@require_GET
def workspace_roles_view(request, slug: str):
    """Return workspace roles (Owner, Staff, and future cloned roles) with member counts."""
    _user, workspace, error_response = require_workspace_member(request, slug)
    if error_response is not None:
        return error_response

    ensure_workspace_default_roles(workspace)
    roles = workspace.roles.annotate(
        total_members=Count('users', distinct=True),
        active_members=Count(
            'users',
            filter=Q(users__is_active=True, users__email_verified=True),
            distinct=True,
        ),
    ).order_by('-is_owner', '-is_default_staff', 'name', 'id')

    return JsonResponse(
        {
            'workspace': {
                'name': workspace.name,
                'slug': workspace.slug,
            },
            'active_seat_count': count_active_seats(workspace),
            'roles': [
                serialize_role(
                    role,
                    member_count=role.total_members,
                    active_member_count=role.active_members,
                )
                for role in roles
            ],
        },
        status=200,
    )


def _validate_object_rights_patch(raw_rights):
    """Validate incoming object_rights dict and return cleaned nested map or JsonResponse."""
    if not isinstance(raw_rights, dict) or not raw_rights:
        return None, JsonResponse(
            {'error': 'object_rights must be a non-empty JSON object.'},
            status=400,
        )

    cleaned: dict[str, dict[str, bool]] = {}
    for obj_key, raw_entry in raw_rights.items():
        if obj_key not in OBJECTS_BY_KEY:
            return None, JsonResponse(
                {'error': f'Unknown catalog object key: {obj_key}.'},
                status=400,
            )
        if not isinstance(raw_entry, dict) or not raw_entry:
            return None, JsonResponse(
                {'error': f'Rights for {obj_key} must be a non-empty object.'},
                status=400,
            )
        cleaned_entry: dict[str, bool] = {}
        for action, value in raw_entry.items():
            if action not in CRUD_ACTIONS:
                return None, JsonResponse(
                    {'error': f'Unsupported permission action "{action}" for {obj_key}.'},
                    status=400,
                )
            if not isinstance(value, bool):
                return None, JsonResponse(
                    {'error': f'Permission {obj_key}.{action} must be a boolean.'},
                    status=400,
                )
            cleaned_entry[action] = value
        cleaned[obj_key] = cleaned_entry

    return cleaned, None


@require_http_methods(['GET', 'PATCH'])
def workspace_role_detail_view(request, slug: str, role_id: int):
    """Retrieve or update object_rights for a non-Owner workspace role."""
    if request.method == 'GET':
        _user, workspace, error_response = require_workspace_member(request, slug)
        if error_response is not None:
            return error_response
        ensure_workspace_default_roles(workspace)
        role = (
            workspace.roles.annotate(
                total_members=Count('users', distinct=True),
                active_members=Count(
                    'users',
                    filter=Q(users__is_active=True, users__email_verified=True),
                    distinct=True,
                ),
            )
            .filter(id=role_id)
            .first()
        )
        if role is None:
            return JsonResponse({'error': 'Role not found.'}, status=404)
        return JsonResponse(
            {
                'role': serialize_role(
                    role,
                    member_count=role.total_members,
                    active_member_count=role.active_members,
                )
            },
            status=200,
        )

    acting_user, workspace, error_response = _require_user_management_permission(request, slug)
    if error_response is not None:
        return error_response

    payload, parse_error = parse_json_object(request)
    if parse_error is not None:
        return parse_error

    cleaned_patch, validation_error = _validate_object_rights_patch(payload.get('object_rights'))
    if validation_error is not None:
        return validation_error

    with transaction.atomic():
        ensure_workspace_default_roles(workspace)
        role = (
            Role.objects.select_for_update()
            .filter(id=role_id, workspace=workspace)
            .first()
        )
        if role is None:
            return JsonResponse({'error': 'Role not found.'}, status=404)
        if role.is_owner:
            return JsonResponse(
                {
                    'error': (
                        'The workspace Owner role always has full permissions and '
                        'cannot be restricted.'
                    )
                },
                status=403,
            )

        merged_rights = normalize_role_object_rights(role)
        changes: list[tuple[str, bool, bool]] = []
        for obj_key, actions in cleaned_patch.items():
            for action, next_val in actions.items():
                prev_val = bool(merged_rights[obj_key][action])
                if prev_val != next_val:
                    merged_rights[obj_key][action] = next_val
                    changes.append((f'{obj_key}.{action}', prev_val, next_val))

        if changes:
            role.object_rights = merged_rights
            role.save(update_fields=['object_rights', 'updated_at'])
            for field_name, prev_val, next_val in changes:
                record_audit_event(
                    workspace=workspace,
                    object_type='role',
                    object_id=role.id,
                    record_label=role.name,
                    action=AuditLog.ACTION_UPDATE,
                    field_name=field_name,
                    previous_value=prev_val,
                    new_value=next_val,
                    actor_kind=AuditLog.ACTOR_KIND_PERSON,
                    actor_user=acting_user,
                )

    annotated_role = (
        workspace.roles.annotate(
            total_members=Count('users', distinct=True),
            active_members=Count(
                'users',
                filter=Q(users__is_active=True, users__email_verified=True),
                distinct=True,
            ),
        )
        .filter(id=role_id)
        .first()
    )
    return JsonResponse(
        {
            'role': serialize_role(
                annotated_role,
                member_count=annotated_role.total_members,
                active_member_count=annotated_role.active_members,
            ),
            'changed': bool(changes),
            'message': (
                f'Updated object permissions for {annotated_role.name}.'
                if changes
                else f'No permission changes for {annotated_role.name}.'
            ),
        },
        status=200,
    )
