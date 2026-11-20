from copy import deepcopy
from dataclasses import dataclass

from django.db.models import Count, Q

from ..catalog import ALL_OBJECTS, OBJECTS_BY_KEY
from ..models import (
    Account,
    Activity,
    Appointment,
    AuditLog,
    Contact,
    Deal,
    Job,
    Lead,
    Role,
    Ticket,
    Visit,
)
from .audit_service import record_audit_event
from .cache_service import (
    DOMAIN_MEMBERS,
    DOMAIN_ROLES,
    get_or_set_workspace_cache_bundle,
    invalidate_workspace_cache,
)

CRUD_ACTIONS: tuple[str, ...] = ('create', 'read', 'update', 'delete')

FIELD_RIGHT_HIDDEN = 'hidden'
FIELD_RIGHT_READ_ONLY = 'read_only'
FIELD_RIGHT_EDITABLE = 'editable'
FIELD_RIGHT_LEVELS: tuple[str, ...] = (
    FIELD_RIGHT_HIDDEN,
    FIELD_RIGHT_READ_ONLY,
    FIELD_RIGHT_EDITABLE,
)

ROLE_SWITCH_MODEL_FIELDS: dict[str, str] = {
    'import': 'allow_import',
    'export': 'allow_export',
    'automations': 'automations',
    'scripts': 'scripts',
    'users_and_roles': 'users_and_roles',
    'theme_and_layouts': 'theme_and_layouts',
    'unattended_agent': 'unattended_agent',
}

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
S2_4_CONFIGURABLE_OBJECT_KEYS: tuple[str, ...] = (
    'account',
    'contact',
    'activity',
    'lead',
    'deal',
    'job',
    'appointment',
    'ticket',
    'visit',
)
ALL_OBJECT_KEYS: tuple[str, ...] = tuple(obj.key for obj in ALL_OBJECTS)


@dataclass(frozen=True)
class FieldSchema:
    key: str
    label: str
    payload_keys: tuple[str, ...]
    serialized_keys: tuple[str, ...]
    uses_contact_label: bool = False


OBJECT_FIELD_SCHEMAS: dict[str, tuple[FieldSchema, ...]] = {
    'account': (
        FieldSchema('name', 'Company name', ('name', 'company'), ('name',)),
        FieldSchema('city', 'City', ('city',), ('city',)),
        FieldSchema('phone', 'Phone', ('phone',), ('phone',)),
        FieldSchema('email', 'Email', ('email',), ('email',)),
        FieldSchema('notes', 'Notes', ('notes',), ('notes',)),
    ),
    'contact': (
        FieldSchema('name', 'Full name', ('name',), ('name',)),
        FieldSchema('title', 'Job title', ('title',), ('title',)),
        FieldSchema('account', 'Account', ('account_id', 'account'), ('account_id', 'account')),
        FieldSchema('email', 'Email', ('email',), ('email',)),
        FieldSchema('phone', 'Phone', ('phone',), ('phone',)),
        FieldSchema('sms_consent', 'SMS consent', ('sms_consent',), ('sms_consent',)),
        FieldSchema('notes', 'Notes', ('notes',), ('notes',)),
    ),
    'lead': (
        FieldSchema('name', 'Lead name', ('name',), ('name',)),
        FieldSchema('company', 'Company', ('company',), ('company',)),
        FieldSchema('email', 'Email', ('email',), ('email',)),
        FieldSchema('phone', 'Phone', ('phone',), ('phone',)),
        FieldSchema('sms_consent', 'SMS consent', ('sms_consent',), ('sms_consent',)),
        FieldSchema('status', 'Status', ('status',), ('status',)),
        FieldSchema('notes', 'Notes', ('notes',), ('notes',)),
    ),
    'deal': (
        FieldSchema('title', 'Deal title', ('title', 'name'), ('title',)),
        FieldSchema('account', 'Account', ('account_id', 'account'), ('account_id', 'account')),
        FieldSchema(
            'contact',
            'Contact',
            ('contact_id', 'contact'),
            ('contact_id', 'contact'),
            uses_contact_label=True,
        ),
        FieldSchema('amount', 'Amount (GHS)', ('amount', 'value'), ('amount',)),
        FieldSchema(
            'stage',
            'Stage',
            ('stage', 'pipeline_stage_id', 'stage_id'),
            ('stage', 'stage_tone', 'pipeline_stage_id'),
        ),
        FieldSchema('target_close_date', 'Target close date', ('target_close_date',), ('target_close_date',)),
        FieldSchema('win_probability', 'Win probability (%)', ('win_probability',), ('win_probability',)),
        FieldSchema('estimated_margin', 'Estimated margin (GHS)', ('estimated_margin',), ('estimated_margin',)),
        FieldSchema('notes', 'Notes', ('notes',), ('notes',)),
    ),
    'job': (
        FieldSchema('title', 'Job title', ('title', 'name'), ('title',)),
        FieldSchema('status', 'Status', ('status',), ('status',)),
        FieldSchema('assignee', 'Assignee', ('assignee_id', 'assignee'), ('assignee_id', 'assignee')),
        FieldSchema('due_date', 'Due date', ('due_date',), ('due_date',)),
        FieldSchema('account', 'Account', ('account_id', 'account'), ('account_id', 'account')),
        FieldSchema(
            'contact',
            'Contact',
            ('contact_id', 'contact'),
            ('contact_id', 'contact'),
            uses_contact_label=True,
        ),
        FieldSchema('notes', 'Notes', ('notes',), ('notes',)),
    ),
    'appointment': (
        FieldSchema('title', 'Appointment title', ('title', 'name'), ('title',)),
        FieldSchema('scheduled_at', 'Scheduled time', ('scheduled_at', 'time'), ('scheduled_at', 'time')),
        FieldSchema('status', 'Status', ('status',), ('status',)),
        FieldSchema('assignee', 'Assignee', ('assignee_id', 'assignee'), ('assignee_id', 'assignee')),
        FieldSchema(
            'contact',
            'Contact',
            ('contact_id', 'contact'),
            ('contact_id', 'contact'),
            uses_contact_label=True,
        ),
        FieldSchema('notes', 'Notes', ('notes',), ('notes',)),
    ),
    'ticket': (
        FieldSchema('title', 'Ticket title', ('title', 'subject', 'name'), ('title',)),
        FieldSchema('status', 'Status', ('status',), ('status',)),
        FieldSchema('priority', 'Priority', ('priority',), ('priority',)),
        FieldSchema('account', 'Account', ('account_id', 'account'), ('account_id', 'account')),
        FieldSchema(
            'contact',
            'Contact',
            ('contact_id', 'contact'),
            ('contact_id', 'contact'),
            uses_contact_label=True,
        ),
        FieldSchema('assignee', 'Assignee', ('assignee_id', 'assignee'), ('assignee_id', 'assignee')),
        FieldSchema('description', 'Description', ('description', 'notes'), ('description',)),
    ),
    'activity': (
        FieldSchema('activity_type', 'Activity type', ('activity_type', 'type'), ('activity_type', 'type')),
        FieldSchema('summary', 'Summary', ('summary', 'text', 'body', 'notes'), ('summary', 'text')),
        FieldSchema('due_at', 'Due at', ('due_at', 'due_date'), ('due_at',)),
        FieldSchema('is_completed', 'Completed', ('is_completed', 'done'), ('is_completed',)),
        FieldSchema('assignee', 'Assignee', ('assignee_id', 'assignee'), ('assignee_id', 'assignee')),
    ),
    'visit': (
        FieldSchema('date', 'Visit date', ('date', 'visited_at'), ('date', 'visited_at')),
        FieldSchema('reason', 'Reason', ('reason',), ('reason',)),
        FieldSchema(
            'contact',
            'Contact',
            ('contact_id', 'contact'),
            ('contact_id', 'contact'),
            uses_contact_label=True,
        ),
        FieldSchema('assignee', 'Assignee', ('assignee_id', 'assignee'), ('assignee_id', 'assignee')),
        FieldSchema('notes', 'Notes', ('notes',), ('notes',)),
        FieldSchema('follow_up', 'Follow-up', ('follow_up',), ('follow_up',)),
    ),
}

PRIMARY_LABEL_FIELD_BY_OBJECT: dict[str, str] = {
    'account': 'name',
    'contact': 'name',
    'lead': 'name',
    'deal': 'title',
    'job': 'title',
    'appointment': 'title',
    'visit': 'reason',
    'ticket': 'title',
    'activity': 'summary',
}

_SHARED_RECORD_MODELS = (
    ('account', Account),
    ('contact', Contact),
    ('lead', Lead),
    ('deal', Deal),
    ('job', Job),
    ('appointment', Appointment),
    ('visit', Visit),
    ('ticket', Ticket),
    ('activity', Activity),
)


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


def build_default_field_rights() -> dict[str, dict[str, str]]:
    """Build default per-object field permissions (every field editable)."""
    return {
        obj_key: {
            schema.key: FIELD_RIGHT_EDITABLE
            for schema in schemas
        }
        for obj_key, schemas in OBJECT_FIELD_SCHEMAS.items()
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


def normalize_role_field_rights(
    role: Role | None,
    *,
    is_owner_fallback: bool = False,
) -> dict[str, dict[str, str]]:
    """Return a complete {object_key: {field_key: hidden|read_only|editable}} map."""
    normalized = build_default_field_rights()
    if role is None or role.is_owner or is_owner_fallback:
        return normalized

    stored = role.field_rights if isinstance(role.field_rights, dict) else {}
    for obj_key, schemas in OBJECT_FIELD_SCHEMAS.items():
        raw_entry = stored.get(obj_key)
        if not isinstance(raw_entry, dict):
            continue
        for schema in schemas:
            raw_level = raw_entry.get(schema.key)
            if raw_level in FIELD_RIGHT_LEVELS:
                normalized[obj_key][schema.key] = raw_level
    return normalized


def serialize_field_schemas(workspace=None) -> dict[str, list[dict[str, str]]]:
    """Return field metadata per configurable object, adapting Contact labels to workspace."""
    contact_label = (getattr(workspace, 'contact_label', '') or 'Contact').strip() or 'Contact'
    result: dict[str, list[dict[str, str]]] = {}
    for obj_key, schemas in OBJECT_FIELD_SCHEMAS.items():
        result[obj_key] = [
            {
                'key': schema.key,
                'label': contact_label if schema.uses_contact_label else schema.label,
            }
            for schema in schemas
        ]
    return result


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
    'field_rights': build_default_field_rights(),
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
    'field_rights': build_default_field_rights(),
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
    owner_update_fields = []
    if not owner_role.object_rights:
        owner_role.object_rights = build_default_object_rights(is_owner=True)
        owner_update_fields.append('object_rights')
    if not owner_role.field_rights:
        owner_role.field_rights = build_default_field_rights()
        owner_update_fields.append('field_rights')
    if owner_update_fields:
        owner_role.save(update_fields=[*owner_update_fields, 'updated_at'])

    staff_role, _ = Role.objects.get_or_create(
        workspace=workspace,
        is_default_staff=True,
        defaults={
            **STAFF_ROLE_DEFAULTS,
            'is_owner': False,
        },
    )
    staff_update_fields = []
    if not staff_role.object_rights:
        staff_role.object_rights = build_default_object_rights(is_owner=False)
        staff_update_fields.append('object_rights')
    if not staff_role.field_rights:
        staff_role.field_rights = build_default_field_rights()
        staff_update_fields.append('field_rights')
    if staff_update_fields:
        staff_role.save(update_fields=[*staff_update_fields, 'updated_at'])

    staff_qs = workspace.users.filter(role__isnull=True)
    updated_users = 0
    if workspace.created_by_id is not None:
        updated_users += workspace.users.filter(
            id=workspace.created_by_id,
            role__isnull=True,
        ).update(role=owner_role)
        staff_qs = staff_qs.exclude(id=workspace.created_by_id)
    updated_users += staff_qs.update(role=staff_role)
    if updated_users:
        invalidate_workspace_cache(workspace.id, domains=(DOMAIN_MEMBERS, DOMAIN_ROLES))
    return owner_role, staff_role


def clone_role_from_source(
    workspace,
    *,
    name: str,
    description: str = '',
    source_role: Role | None = None,
) -> Role:
    """Clone Staff (or a specified non-Owner role), copying object rights, field rights, and switches."""
    _owner_role, staff_role = ensure_workspace_default_roles(workspace)
    template = source_role if source_role is not None else staff_role
    if template.is_owner or template.workspace_id != workspace.id:
        raise ValueError('Roles can only be cloned from a non-Owner role in this workspace.')

    return Role.objects.create(
        workspace=workspace,
        name=name,
        description=description if description else f'Cloned from {template.name}.',
        is_owner=False,
        is_default_staff=False,
        can_manage_seat_plan=False,
        can_manage_sms_wallet=False,
        allow_import=bool(template.allow_import),
        allow_export=bool(template.allow_export),
        automations=bool(template.automations),
        scripts=bool(template.scripts),
        users_and_roles=bool(template.users_and_roles),
        theme_and_layouts=bool(template.theme_and_layouts),
        unattended_agent=bool(template.unattended_agent),
        object_rights=deepcopy(normalize_role_object_rights(template)),
        field_rights=deepcopy(normalize_role_field_rights(template)),
    )


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


def resolve_effective_user_role(user, workspace) -> Role | None:
    """Resolve the active Role for user in workspace, falling back to default Staff when unset."""
    if not user or workspace is None:
        return None
    role = getattr(user, 'role', None)
    if role is not None and role.workspace_id == workspace.id:
        return role
    owner_role, staff_role = ensure_workspace_default_roles(workspace)
    if is_workspace_owner(user, workspace):
        return owner_role
    return staff_role


def user_has_object_right(user, workspace, object_key: str, action: str) -> bool:
    """Check whether the user's role grants create/read/update/delete on object_key."""
    if not _is_verified_active_workspace_member(user, workspace):
        return False
    if action not in CRUD_ACTIONS or object_key not in OBJECTS_BY_KEY:
        return False
    if is_workspace_owner(user, workspace):
        return True

    role = resolve_effective_user_role(user, workspace)
    rights = normalize_role_object_rights(role)
    return bool(rights.get(object_key, {}).get(action, False))


def get_user_field_rights(user, workspace, object_key: str) -> dict[str, str]:
    """Return {field_key: hidden|read_only|editable} for user on object_key."""
    if object_key not in OBJECT_FIELD_SCHEMAS:
        return {}
    if is_workspace_owner(user, workspace):
        return {
            schema.key: FIELD_RIGHT_EDITABLE
            for schema in OBJECT_FIELD_SCHEMAS[object_key]
        }
    role = resolve_effective_user_role(user, workspace)
    return normalize_role_field_rights(role).get(object_key, {})


def get_field_right(user, workspace, object_key: str, field_name: str) -> str:
    """Return hidden, read_only, or editable for a single field on object_key."""
    rights = get_user_field_rights(user, workspace, object_key)
    return rights.get(field_name, FIELD_RIGHT_EDITABLE)


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


def user_can_manage_sales_settings(user, workspace) -> bool:
    """Allow verified active Owner or roles with users_and_roles or theme_and_layouts."""
    return user_can_manage_workspace_catalog(user, workspace)


def compute_user_permissions_version(user, workspace) -> str:
    """Compute a lightweight version token that changes immediately on role or permission edits."""
    if not user or workspace is None:
        return ''
    is_owner = is_workspace_owner(user, workspace)
    role = getattr(user, 'role', None)
    role_id = role.id if role is not None else 0
    role_updated_us = (
        int(role.updated_at.timestamp() * 1_000_000)
        if role is not None and role.updated_at is not None
        else 0
    )
    session_ver = getattr(user, 'session_version', 1)
    return f'o{int(is_owner)}:r{role_id}:u{role_updated_us}:s{session_ver}'


def build_record_visibility_q(user, workspace, *, prefix: str = '') -> Q:
    """Return a Django Q filter matching CRM records visible to user inside workspace."""
    if is_workspace_owner(user, workspace):
        return Q()

    role = resolve_effective_user_role(user, workspace)
    role_id = role.id if role is not None else None

    vis_key = f'{prefix}visibility'
    owner_key = f'{prefix}owner_id'
    shared_role_key = f'{prefix}shared_role_id'

    visibility_q = Q(**{vis_key: 'workspace'}) | Q(**{owner_key: user.id})
    if role_id is not None:
        visibility_q |= Q(**{vis_key: 'role', shared_role_key: role_id})
    return visibility_q


def user_can_see_record(user, workspace, record) -> bool:
    """Check whether user can view a single CRM record according to its visibility."""
    if record is None or workspace is None or record.workspace_id != workspace.id:
        return False
    if is_workspace_owner(user, workspace):
        return True
    if record.visibility == 'workspace':
        return True
    if record.owner_id == user.id:
        return True
    if record.visibility == 'role' and record.shared_role_id is not None:
        role = resolve_effective_user_role(user, workspace)
        return role is not None and role.id == record.shared_role_id
    return False


def user_can_manage_record_sharing(user, workspace, record) -> bool:
    """Allow record owner, workspace Owner, or users_and_roles managers to change sharing."""
    if record is None or workspace is None or record.workspace_id != workspace.id:
        return False
    if is_workspace_owner(user, workspace):
        return True
    if record.owner_id == user.id:
        return True
    return user_can_manage_users_and_roles(user, workspace)


def count_shared_records_by_role_id(workspace) -> dict[int, int]:
    """Count CRM records currently shared with each role (visibility='role') in workspace."""
    counts: dict[int, int] = {}
    for _object_key, model_cls in _SHARED_RECORD_MODELS:
        rows = (
            model_cls.objects.filter(
                workspace=workspace,
                visibility='role',
                shared_role_id__isnull=False,
            )
            .values('shared_role_id')
            .annotate(cnt=Count('id'))
        )
        for row in rows:
            role_id = row['shared_role_id']
            counts[role_id] = counts.get(role_id, 0) + int(row['cnt'])
    return counts


def fallback_role_shared_records_to_owner(workspace, role: Role, *, acting_user) -> int:
    """Switch all CRM records shared with role to 'owner' visibility and audit each change."""
    updated_count = 0
    for object_key, model_cls in _SHARED_RECORD_MODELS:
        records = list(
            model_cls.objects.select_for_update()
            .filter(workspace=workspace, shared_role=role)
        )
        for record in records:
            prev_vis = f'{record.visibility}:{role.name}' if record.visibility == 'role' else record.visibility
            record.visibility = 'owner'
            record.shared_role = None
            record.save(update_fields=['visibility', 'shared_role', 'updated_at'])
            updated_count += 1
            label = (
                getattr(record, 'name', None)
                or getattr(record, 'title', None)
                or getattr(record, 'reason', None)
                or getattr(record, 'summary', '')[:60]
            )
            record_audit_event(
                workspace=workspace,
                object_type=object_key,
                object_id=record.id,
                record_label=label or f'{object_key}#{record.id}',
                action=AuditLog.ACTION_UPDATE,
                field_name='visibility',
                previous_value=prev_vis,
                new_value='owner',
                actor_kind=AuditLog.ACTOR_KIND_PERSON,
                actor_user=acting_user,
                record=record,
            )
    return updated_count


def count_active_seats(workspace) -> int:
    """Count active verified workspace members who can sign in as paid seats (cached per workspace)."""
    if workspace is None or not getattr(workspace, 'id', None):
        return 0
    bundle = get_or_set_workspace_cache_bundle(
        workspace.id,
        domains=(DOMAIN_MEMBERS,),
        key_suffix='active_seats',
        scope='active_seats',
        loader=lambda: workspace.users.filter(is_active=True, email_verified=True).count(),
    )
    return int(bundle['payload'])
