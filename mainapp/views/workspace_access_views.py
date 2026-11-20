from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from ..catalog import OBJECTS_BY_KEY
from ..helpers.request_helpers import parse_json_object, require_workspace_member
from ..models import AuditLog, Role, User, WorkspaceInvite
from ..serializers import serialize_member, serialize_role
from ..service.audit_service import record_audit_event
from ..service.role_service import (
    CRUD_ACTIONS,
    FIELD_RIGHT_LEVELS,
    OBJECT_FIELD_SCHEMAS,
    ROLE_SWITCH_MODEL_FIELDS,
    clone_role_from_source,
    count_active_seats,
    count_shared_records_by_role_id,
    ensure_workspace_default_roles,
    fallback_role_shared_records_to_owner,
    is_workspace_owner,
    normalize_role_field_rights,
    normalize_role_object_rights,
    serialize_field_schemas,
    user_can_manage_users_and_roles,
)
from ..service.session_service import invalidate_user_sessions

OWNER_PROTECTED_MESSAGE = 'The workspace Owner cannot be deactivated or removed.'
OWNER_ROLE_IMMUTABLE_MESSAGE = (
    'The workspace Owner role always has full permissions and cannot be restricted.'
)
OWNER_ROLE_ASSIGNMENT_MESSAGE = 'The Owner role cannot be assigned to another member.'


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


def _parse_int_id(raw, *, field_name: str):
    if isinstance(raw, bool) or raw is None or raw == '':
        return None, JsonResponse({'error': f'{field_name} is required.'}, status=400)
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return None, JsonResponse({'error': f'{field_name} must be a valid integer ID.'}, status=400)
    if parsed < 1:
        return None, JsonResponse({'error': f'{field_name} must be a valid integer ID.'}, status=400)
    return parsed, None


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


@require_http_methods(['PATCH', 'DELETE'])
def workspace_member_detail_view(request, slug: str, member_id: int):
    """Reassign a non-Owner member's role (PATCH) or refuse direct deletion (DELETE)."""
    acting_user, workspace, error_response = _require_user_management_permission(request, slug)
    if error_response is not None:
        return error_response

    if request.method == 'DELETE':
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

    payload, parse_error = parse_json_object(request)
    if parse_error is not None:
        return parse_error

    role_id, role_id_error = _parse_int_id(payload.get('role_id'), field_name='role_id')
    if role_id_error is not None:
        return role_id_error

    with transaction.atomic():
        ensure_workspace_default_roles(workspace)
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
                {'error': 'The workspace Owner role cannot be reassigned.'},
                status=403,
            )

        if member.id == acting_user.id and not is_workspace_owner(acting_user, workspace):
            return JsonResponse(
                {'error': 'You cannot reassign your own role.'},
                status=400,
            )

        target_role = (
            Role.objects.select_for_update()
            .filter(id=role_id, workspace=workspace)
            .first()
        )
        if target_role is None:
            return JsonResponse(
                {'error': 'Selected role was not found in this workspace.'},
                status=404,
            )
        if target_role.is_owner:
            return JsonResponse({'error': OWNER_ROLE_ASSIGNMENT_MESSAGE}, status=403)

        previous_role = member.role
        previous_role_name = previous_role.name if previous_role is not None else 'Staff'
        changed = member.role_id != target_role.id
        if changed:
            member.role = target_role
            member.save(update_fields=['role'])
            record_audit_event(
                workspace=workspace,
                object_type='user',
                object_id=member.id,
                record_label=member.email,
                action=AuditLog.ACTION_UPDATE,
                field_name='role',
                previous_value=previous_role_name,
                new_value=target_role.name,
                actor_kind=AuditLog.ACTOR_KIND_PERSON,
                actor_user=acting_user,
            )

    return JsonResponse(
        {
            'member': serialize_member(member, workspace),
            'changed': changed,
            'message': (
                f'{member.email} is now assigned to {target_role.name}.'
                if changed
                else f'{member.email} is already assigned to {target_role.name}.'
            ),
        },
        status=200,
    )


# Backward-compatible alias for URL/test imports.
workspace_member_delete_view = workspace_member_detail_view


def _get_annotated_role(workspace, role_id: int):
    return (
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


@require_http_methods(['GET', 'POST'])
def workspace_roles_view(request, slug: str):
    """List workspace roles (GET) or clone Staff/non-Owner role into a new role (POST)."""
    if request.method == 'GET':
        _user, workspace, error_response = require_workspace_member(request, slug)
        if error_response is not None:
            return error_response

        ensure_workspace_default_roles(workspace)
        shared_counts = count_shared_records_by_role_id(workspace)
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
                'field_schemas': serialize_field_schemas(workspace),
                'roles': [
                    serialize_role(
                        role,
                        member_count=role.total_members,
                        active_member_count=role.active_members,
                        shared_record_count=shared_counts.get(role.id, 0),
                    )
                    for role in roles
                ],
            },
            status=200,
        )

    acting_user, workspace, error_response = _require_user_management_permission(request, slug)
    if error_response is not None:
        return error_response

    payload, parse_error = parse_json_object(request)
    if parse_error is not None:
        return parse_error

    raw_name = payload.get('name')
    if not isinstance(raw_name, str) or not raw_name.strip():
        return JsonResponse({'error': 'Role name is required.'}, status=400)
    cleaned_name = ' '.join(raw_name.strip().split())
    if len(cleaned_name) > 100:
        return JsonResponse({'error': 'Role name must be 100 characters or fewer.'}, status=400)

    raw_desc = payload.get('description', '')
    if raw_desc is None:
        cleaned_desc = ''
    elif isinstance(raw_desc, str):
        cleaned_desc = raw_desc.strip()
        if len(cleaned_desc) > 255:
            return JsonResponse(
                {'error': 'Role description must be 255 characters or fewer.'},
                status=400,
            )
    else:
        return JsonResponse({'error': 'Role description must be text.'}, status=400)

    source_role_id = payload.get('source_role_id')
    parsed_source_id = None
    if source_role_id is not None and source_role_id != '':
        parsed_source_id, source_err = _parse_int_id(source_role_id, field_name='source_role_id')
        if source_err is not None:
            return source_err

    try:
        with transaction.atomic():
            _owner_role, staff_role = ensure_workspace_default_roles(workspace)
            if Role.objects.filter(workspace=workspace, name__iexact=cleaned_name).exists():
                return JsonResponse(
                    {'error': f'A role named "{cleaned_name}" already exists in this workspace.'},
                    status=400,
                )

            source_role = staff_role
            if parsed_source_id is not None:
                source_role = Role.objects.filter(id=parsed_source_id, workspace=workspace).first()
                if source_role is None:
                    return JsonResponse({'error': 'Source role not found.'}, status=404)
                if source_role.is_owner:
                    return JsonResponse(
                        {'error': 'Roles must be cloned from Staff or another non-Owner role.'},
                        status=403,
                    )

            new_role = clone_role_from_source(
                workspace,
                name=cleaned_name,
                description=cleaned_desc,
                source_role=source_role,
            )
            record_audit_event(
                workspace=workspace,
                object_type='role',
                object_id=new_role.id,
                record_label=new_role.name,
                action=AuditLog.ACTION_CREATE,
                field_name='role',
                previous_value=source_role.name,
                new_value=new_role.name,
                actor_kind=AuditLog.ACTOR_KIND_PERSON,
                actor_user=acting_user,
            )
    except IntegrityError:
        return JsonResponse(
            {'error': f'A role named "{cleaned_name}" already exists in this workspace.'},
            status=400,
        )

    return JsonResponse(
        {
            'role': serialize_role(
                new_role,
                member_count=0,
                active_member_count=0,
                shared_record_count=0,
            ),
            'message': f'Cloned role "{new_role.name}" from {source_role.name}.',
        },
        status=201,
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


def _validate_field_rights_patch(raw_field_rights):
    """Validate incoming field_rights dict and return cleaned nested map or JsonResponse."""
    if not isinstance(raw_field_rights, dict) or not raw_field_rights:
        return None, JsonResponse(
            {'error': 'field_rights must be a non-empty JSON object.'},
            status=400,
        )

    cleaned: dict[str, dict[str, str]] = {}
    for obj_key, raw_entry in raw_field_rights.items():
        if obj_key not in OBJECT_FIELD_SCHEMAS:
            return None, JsonResponse(
                {'error': f'Unsupported field permissions object: {obj_key}.'},
                status=400,
            )
        if not isinstance(raw_entry, dict) or not raw_entry:
            return None, JsonResponse(
                {'error': f'Field rights for {obj_key} must be a non-empty object.'},
                status=400,
            )
        valid_fields = {schema.key for schema in OBJECT_FIELD_SCHEMAS[obj_key]}
        cleaned_entry: dict[str, str] = {}
        for field_key, level in raw_entry.items():
            if field_key not in valid_fields:
                return None, JsonResponse(
                    {'error': f'Unknown field "{field_key}" on {obj_key}.'},
                    status=400,
                )
            if level not in FIELD_RIGHT_LEVELS:
                return None, JsonResponse(
                    {
                        'error': (
                            f'Field right for {obj_key}.{field_key} must be one of: '
                            f'{", ".join(FIELD_RIGHT_LEVELS)}.'
                        )
                    },
                    status=400,
                )
            cleaned_entry[field_key] = level
        cleaned[obj_key] = cleaned_entry

    return cleaned, None


def _validate_switches_patch(raw_switches):
    """Validate incoming role switches dict and return cleaned map or JsonResponse."""
    if not isinstance(raw_switches, dict) or not raw_switches:
        return None, JsonResponse(
            {'error': 'switches must be a non-empty JSON object.'},
            status=400,
        )

    cleaned: dict[str, bool] = {}
    for switch_key, value in raw_switches.items():
        if switch_key not in ROLE_SWITCH_MODEL_FIELDS:
            return None, JsonResponse(
                {'error': f'Unknown role switch: {switch_key}.'},
                status=400,
            )
        if not isinstance(value, bool):
            return None, JsonResponse(
                {'error': f'Switch "{switch_key}" must be a boolean.'},
                status=400,
            )
        cleaned[switch_key] = value
    return cleaned, None


@require_http_methods(['GET', 'PATCH', 'DELETE'])
def workspace_role_detail_view(request, slug: str, role_id: int):
    """Retrieve (GET), update (PATCH), or delete (DELETE) a workspace role."""
    if request.method == 'GET':
        _user, workspace, error_response = require_workspace_member(request, slug)
        if error_response is not None:
            return error_response
        ensure_workspace_default_roles(workspace)
        role = _get_annotated_role(workspace, role_id)
        if role is None:
            return JsonResponse({'error': 'Role not found.'}, status=404)
        shared_counts = count_shared_records_by_role_id(workspace)
        return JsonResponse(
            {
                'role': serialize_role(
                    role,
                    member_count=role.total_members,
                    active_member_count=role.active_members,
                    shared_record_count=shared_counts.get(role.id, 0),
                ),
                'field_schemas': serialize_field_schemas(workspace),
            },
            status=200,
        )

    acting_user, workspace, error_response = _require_user_management_permission(request, slug)
    if error_response is not None:
        return error_response

    if request.method == 'DELETE':
        with transaction.atomic():
            ensure_workspace_default_roles(workspace)
            role = (
                Role.objects.select_for_update()
                .filter(id=role_id, workspace=workspace)
                .first()
            )
            if role is None:
                return JsonResponse({'error': 'Role not found.'}, status=404)
            if role.is_owner or role.is_default_staff:
                return JsonResponse(
                    {'error': 'Owner and Default Staff roles cannot be deleted.'},
                    status=403,
                )

            member_count = User.objects.filter(workspace=workspace, role=role).count()
            if member_count > 0:
                return JsonResponse(
                    {
                        'error': (
                            f'Reassign {member_count} workspace '
                            f'{"member" if member_count == 1 else "members"} before deleting {role.name}.'
                        )
                    },
                    status=409,
                )

            now = timezone.now()
            pending_invites_count = WorkspaceInvite.objects.filter(
                workspace=workspace,
                role=role,
                accepted_at__isnull=True,
                revoked_at__isnull=True,
                expires_at__gt=now,
            ).count()
            if pending_invites_count > 0:
                return JsonResponse(
                    {
                        'error': (
                            f'Revoke {pending_invites_count} pending '
                            f'{"invite" if pending_invites_count == 1 else "invites"} '
                            f'using {role.name} before deleting it.'
                        )
                    },
                    status=409,
                )

            role_pk = role.id
            role_name = role.name
            fallback_count = fallback_role_shared_records_to_owner(
                workspace,
                role,
                acting_user=acting_user,
            )
            role.delete()
            record_audit_event(
                workspace=workspace,
                object_type='role',
                object_id=role_pk,
                record_label=role_name,
                action=AuditLog.ACTION_DELETE,
                field_name='role',
                previous_value=role_name,
                new_value=None,
                actor_kind=AuditLog.ACTOR_KIND_PERSON,
                actor_user=acting_user,
            )

        msg = f'Deleted role "{role_name}".'
        if fallback_count:
            msg += (
                f' {fallback_count} shared '
                f'{"record was" if fallback_count == 1 else "records were"} '
                f'switched to Owner only visibility.'
            )
        return JsonResponse(
            {
                'ok': True,
                'fallback_records_count': fallback_count,
                'message': msg,
            },
            status=200,
        )

    payload, parse_error = parse_json_object(request)
    if parse_error is not None:
        return parse_error

    has_any_section = any(
        key in payload for key in ('name', 'description', 'switches', 'object_rights', 'field_rights')
    )
    if not has_any_section:
        return JsonResponse(
            {'error': 'Provide at least one field to update (name, description, switches, object_rights, or field_rights).'},
            status=400,
        )

    cleaned_name = None
    if 'name' in payload:
        raw_name = payload.get('name')
        if not isinstance(raw_name, str) or not raw_name.strip():
            return JsonResponse({'error': 'Role name is required.'}, status=400)
        cleaned_name = ' '.join(raw_name.strip().split())
        if len(cleaned_name) > 100:
            return JsonResponse({'error': 'Role name must be 100 characters or fewer.'}, status=400)

    cleaned_desc = None
    if 'description' in payload:
        raw_desc = payload.get('description')
        if raw_desc is None:
            cleaned_desc = ''
        elif isinstance(raw_desc, str):
            cleaned_desc = raw_desc.strip()
            if len(cleaned_desc) > 255:
                return JsonResponse(
                    {'error': 'Role description must be 255 characters or fewer.'},
                    status=400,
                )
        else:
            return JsonResponse({'error': 'Role description must be text.'}, status=400)

    cleaned_switches = None
    if 'switches' in payload:
        cleaned_switches, sw_error = _validate_switches_patch(payload.get('switches'))
        if sw_error is not None:
            return sw_error

    cleaned_object_rights = None
    if 'object_rights' in payload:
        cleaned_object_rights, obj_error = _validate_object_rights_patch(payload.get('object_rights'))
        if obj_error is not None:
            return obj_error

    cleaned_field_rights = None
    if 'field_rights' in payload:
        cleaned_field_rights, field_error = _validate_field_rights_patch(payload.get('field_rights'))
        if field_error is not None:
            return field_error

    try:
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
                return JsonResponse({'error': OWNER_ROLE_IMMUTABLE_MESSAGE}, status=403)

            # Prevent a non-Owner manager from disabling users_and_roles on their own active role.
            if (
                cleaned_switches is not None
                and cleaned_switches.get('users_and_roles') is False
                and not is_workspace_owner(acting_user, workspace)
                and acting_user.role_id == role.id
            ):
                return JsonResponse(
                    {'error': 'You cannot remove users-and-roles access from your own active role.'},
                    status=400,
                )

            changes: list[tuple[str, object, object]] = []
            update_fields: set[str] = set()

            if cleaned_name is not None and cleaned_name != role.name:
                if role.is_default_staff:
                    return JsonResponse(
                        {'error': 'The default Staff role cannot be renamed.'},
                        status=400,
                    )
                if Role.objects.filter(workspace=workspace, name__iexact=cleaned_name).exclude(id=role.id).exists():
                    return JsonResponse(
                        {'error': f'A role named "{cleaned_name}" already exists in this workspace.'},
                        status=400,
                    )
                changes.append(('name', role.name, cleaned_name))
                role.name = cleaned_name
                update_fields.add('name')

            if cleaned_desc is not None and cleaned_desc != role.description:
                changes.append(('description', role.description, cleaned_desc))
                role.description = cleaned_desc
                update_fields.add('description')

            if cleaned_switches is not None:
                for switch_key, next_val in cleaned_switches.items():
                    model_attr = ROLE_SWITCH_MODEL_FIELDS[switch_key]
                    prev_val = bool(getattr(role, model_attr))
                    if prev_val != next_val:
                        setattr(role, model_attr, next_val)
                        update_fields.add(model_attr)
                        changes.append((f'switch.{switch_key}', prev_val, next_val))

            if cleaned_object_rights is not None:
                merged_rights = normalize_role_object_rights(role)
                obj_changed = False
                for obj_key, actions in cleaned_object_rights.items():
                    for action, next_val in actions.items():
                        prev_val = bool(merged_rights[obj_key][action])
                        if prev_val != next_val:
                            merged_rights[obj_key][action] = next_val
                            obj_changed = True
                            changes.append((f'{obj_key}.{action}', prev_val, next_val))
                if obj_changed:
                    role.object_rights = merged_rights
                    update_fields.add('object_rights')

            if cleaned_field_rights is not None:
                merged_field_rights = normalize_role_field_rights(role)
                field_changed = False
                for obj_key, fields_map in cleaned_field_rights.items():
                    for field_key, next_level in fields_map.items():
                        prev_level = merged_field_rights[obj_key][field_key]
                        if prev_level != next_level:
                            merged_field_rights[obj_key][field_key] = next_level
                            field_changed = True
                            changes.append((f'field_rights.{obj_key}.{field_key}', prev_level, next_level))
                if field_changed:
                    role.field_rights = merged_field_rights
                    update_fields.add('field_rights')

            if changes:
                update_fields.add('updated_at')
                role.save(update_fields=sorted(update_fields))
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
    except IntegrityError:
        return JsonResponse(
            {'error': f'A role named "{cleaned_name}" already exists in this workspace.'},
            status=400,
        )

    annotated_role = _get_annotated_role(workspace, role_id)
    shared_counts = count_shared_records_by_role_id(workspace)
    return JsonResponse(
        {
            'role': serialize_role(
                annotated_role,
                member_count=annotated_role.total_members,
                active_member_count=annotated_role.active_members,
                shared_record_count=shared_counts.get(annotated_role.id, 0),
            ),
            'changed': bool(changes),
            'message': (
                f'Updated permissions for {annotated_role.name}.'
                if changes
                else f'No permission changes for {annotated_role.name}.'
            ),
        },
        status=200,
    )
