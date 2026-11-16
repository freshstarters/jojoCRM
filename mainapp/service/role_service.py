from ..catalog import ALL_OBJECTS, OBJECTS_BY_KEY
from ..models import Role

CRUD_ACTIONS: tuple[str, ...] = ('create', 'read', 'update', 'delete')
S2_1_OBJECT_KEYS: tuple[str, ...] = (
    'account',
    'contact',
    'lead',
    'deal',
    'activity',
    'job',
    'appointment',
    'ticket',
)
ALL_OBJECT_KEYS: tuple[str, ...] = tuple(obj.key for obj in ALL_OBJECTS)


def build_default_object_rights(*, is_owner: bool = False) -> dict[str, dict[str, bool]]:
    """Build default per-object CRUD permissions for Owner or Staff roles."""
    default_delete = bool(is_owner)
    return {
        obj_key: {
            'create': True,
            'read': True,
            'update': True,
            'delete': default_delete,
        }
        for obj_key in ALL_OBJECT_KEYS
    }


def normalize_role_object_rights(
    role: Role | None,
    *,
    is_owner_fallback: bool = False,
) -> dict[str, dict[str, bool]]:
    """Return a complete {object_key: {create, read, update, delete}} map for a role."""
    if role is None:
        return build_default_object_rights(is_owner=is_owner_fallback)
    if role.is_owner:
        return build_default_object_rights(is_owner=True)

    normalized = build_default_object_rights(is_owner=False)
    stored = role.object_rights if isinstance(role.object_rights, dict) else {}
    for obj_key in ALL_OBJECT_KEYS:
        raw_entry = stored.get(obj_key)
        if not isinstance(raw_entry, dict):
            continue
        for action in CRUD_ACTIONS:
            if isinstance(raw_entry.get(action), bool):
                normalized[obj_key][action] = raw_entry[action]
    return normalized


OWNER_ROLE_DEFAULTS = {
    'name': 'Owner',
    'description': (
        'Permanent workspace creator role. Cannot be deactivated or removed, '
        'and exclusively manages the seat plan and SMS wallet.'
    ),
    'can_manage_seat_plan': True,
    'can_manage_sms_wallet': True,
    'allow_import': True,
    'allow_export': True,
    'automations': True,
    'scripts': True,
    'users_and_roles': True,
    'theme_and_layouts': True,
    'unattended_agent': True,
    'object_rights': build_default_object_rights(is_owner=True),
}

STAFF_ROLE_DEFAULTS = {
    'name': 'Staff',
    'description': (
        'Default team member role. Stored as the template for cloning custom '
        'roles and fine-grained field permissions.'
    ),
    'can_manage_seat_plan': False,
    'can_manage_sms_wallet': False,
    'allow_import': False,
    'allow_export': False,
    'automations': False,
    'scripts': False,
    'users_and_roles': False,
    'theme_and_layouts': False,
    'unattended_agent': False,
    'object_rights': build_default_object_rights(is_owner=False),
}


def ensure_workspace_default_roles(workspace) -> tuple[Role, Role]:
    """Ensure a workspace has its permanent Owner role and default Staff role."""
    owner_role, _ = Role.objects.get_or_create(
        workspace=workspace,
        is_owner=True,
        defaults={
            **OWNER_ROLE_DEFAULTS,
            'is_default_staff': False,
        },
    )
    if not owner_role.object_rights:
        owner_role.object_rights = build_default_object_rights(is_owner=True)
        owner_role.save(update_fields=['object_rights', 'updated_at'])

    staff_role, _ = Role.objects.get_or_create(
        workspace=workspace,
        is_default_staff=True,
        defaults={
            **STAFF_ROLE_DEFAULTS,
            'is_owner': False,
        },
    )
    if not staff_role.object_rights:
        staff_role.object_rights = build_default_object_rights(is_owner=False)
        staff_role.save(update_fields=['object_rights', 'updated_at'])

    staff_qs = workspace.users.filter(role__isnull=True)
    if workspace.created_by_id is not None:
        workspace.users.filter(
            id=workspace.created_by_id,
            role__isnull=True,
        ).update(role=owner_role)
        staff_qs = staff_qs.exclude(id=workspace.created_by_id)
    staff_qs.update(role=staff_role)
    return owner_role, staff_role


def _is_verified_active_workspace_member(user, workspace) -> bool:
    """Return True when user is an authenticated, active, email-verified member of workspace."""
    return bool(
        user
        and getattr(user, 'is_authenticated', False)
        and getattr(user, 'is_active', False)
        and getattr(user, 'email_verified', False)
        and workspace is not None
        and getattr(user, 'workspace_id', None) == workspace.id
    )


def is_workspace_owner(user, workspace) -> bool:
    """Return True when user is the permanent Owner of the given workspace."""
    if not user or workspace is None:
        return False
    if workspace.created_by_id is not None and workspace.created_by_id == user.id:
        return True
    role = getattr(user, 'role', None)
    return bool(
        role is not None
        and role.workspace_id == workspace.id
        and role.is_owner
    )


def user_has_object_right(user, workspace, object_key: str, action: str) -> bool:
    """Check whether the user's role grants create/read/update/delete on object_key."""
    if not _is_verified_active_workspace_member(user, workspace):
        return False
    if action not in CRUD_ACTIONS or object_key not in OBJECTS_BY_KEY:
        return False
    if is_workspace_owner(user, workspace):
        return True

    role = getattr(user, 'role', None)
    if role is None or role.workspace_id != workspace.id:
        _owner_role, staff_role = ensure_workspace_default_roles(workspace)
        role = staff_role

    rights = normalize_role_object_rights(role)
    return bool(rights.get(object_key, {}).get(action, False))


def user_can_manage_users_and_roles(user, workspace) -> bool:
    """Allow verified active Owner or a role holding the users_and_roles switch."""
    if not _is_verified_active_workspace_member(user, workspace):
        return False
    if is_workspace_owner(user, workspace):
        return True
    role = getattr(user, 'role', None)
    return bool(
        role is not None
        and role.workspace_id == workspace.id
        and role.users_and_roles
    )


def user_can_manage_workspace_catalog(user, workspace) -> bool:
    """Allow verified active Owner or a role with users_and_roles or theme_and_layouts."""
    if not _is_verified_active_workspace_member(user, workspace):
        return False
    if is_workspace_owner(user, workspace):
        return True
    role = getattr(user, 'role', None)
    return bool(
        role is not None
        and role.workspace_id == workspace.id
        and (role.users_and_roles or role.theme_and_layouts)
    )


def count_active_seats(workspace) -> int:
    """Count active verified workspace members who can sign in as paid seats."""
    if workspace is None:
        return 0
    return workspace.users.filter(is_active=True, email_verified=True).count()
