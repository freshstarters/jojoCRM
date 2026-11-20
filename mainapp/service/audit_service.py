import math

from django.db.models import Q, TextField
from django.db.models.functions import Cast
from django.utils import timezone

from ..catalog import ALL_OBJECTS, OBJECTS_BY_KEY
from ..models import AuditLog

VALID_ACTOR_KINDS = frozenset(
    {
        AuditLog.ACTOR_KIND_PERSON,
        AuditLog.ACTOR_KIND_SCRIPT,
        AuditLog.ACTOR_KIND_AUTOMATION,
        AuditLog.ACTOR_KIND_AGENT,
    }
)

VALID_AUDIT_ACTIONS = frozenset(
    {
        AuditLog.ACTION_CREATE,
        AuditLog.ACTION_UPDATE,
        AuditLog.ACTION_DELETE,
    }
)

DEFAULT_AUDIT_PAGE_SIZE = 20
MAX_AUDIT_PAGE_SIZE = 100
DEFAULT_RECORD_AUDIT_PAGE_SIZE = 10
MAX_RECORD_AUDIT_PAGE_SIZE = 50

PRIVATE_SECURITY_OBJECT_TYPES = (
    'workspace_invite',
    'user',
    'external_identity',
    'passkey',
)

SYSTEM_OBJECT_LABELS: dict[str, str] = {
    'workspace': 'Workspace',
    'workspace_pack': 'Pack',
    'workspace_table': 'Table',
    'role': 'Role',
    'pipeline': 'Pipeline',
    'pipeline_stage': 'Pipeline stage',
    'user': 'User',
    'workspace_invite': 'Invite',
    'external_identity': 'Sign-in link',
    'passkey': 'Passkey',
}

INSPECTABLE_CRM_PANE_BY_KEY: dict[str, str] = {
    'account': 'accounts',
    'contact': 'contacts',
    'lead': 'leads',
    'deal': 'deals',
    'job': 'jobs',
    'appointment': 'appointments',
    'visit': 'visits',
    'ticket': 'tickets',
}

RECORD_CODE_PREFIX_BY_KEY: dict[str, str] = {
    'account': 'ACC',
    'contact': 'CON',
    'lead': 'LD',
    'deal': 'DL',
    'activity': 'ACT',
    'job': 'JOB',
    'appointment': 'APT',
    'visit': 'VIS',
    'ticket': 'TCK',
}

AUDIT_OBJECT_TYPE_ALIASES: dict[str, str] = {
    'accounts': 'account',
    'contacts': 'contact',
    'leads': 'lead',
    'deals': 'deal',
    'jobs': 'job',
    'appointments': 'appointment',
    'visits': 'visit',
    'tickets': 'ticket',
    'activities': 'activity',
    'roles': 'role',
    'pipelines': 'pipeline',
    'pipeline_stages': 'pipeline_stage',
    'invites': 'workspace_invite',
    'users': 'user',
    'passkeys': 'passkey',
}

KNOWN_AUDIT_OBJECT_TYPES: frozenset[str] = frozenset(
    {obj.key for obj in ALL_OBJECTS} | set(SYSTEM_OBJECT_LABELS.keys())
)


class AuditQueryError(Exception):
    """Domain validation error for audit log queries."""

    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _json_safe_value(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _json_safe_value(v) for k, v in value.items()}
    return str(value)


def record_audit_event(
    *,
    workspace,
    object_type: str,
    record_label: str,
    field_name: str,
    previous_value=None,
    new_value=None,
    actor_kind: str = AuditLog.ACTOR_KIND_PERSON,
    actor_user=None,
    actor_label: str = '',
    object_id='',
    action: str = AuditLog.ACTION_UPDATE,
    created_at=None,
    record=None,
    record_visibility: str = '',
    record_owner=None,
    record_shared_role=None,
) -> AuditLog:
    """Create one immutable AuditLog entry for a workspace record or access change."""
    if workspace is None:
        raise ValueError('workspace is required for an audit log entry.')
    if actor_kind not in VALID_ACTOR_KINDS:
        raise ValueError(f'Unsupported actor_kind: {actor_kind}')

    resolved_label = (actor_label or '').strip()
    if not resolved_label and actor_user is not None:
        resolved_label = getattr(actor_user, 'email', '') or str(actor_user)
    if actor_kind == AuditLog.ACTOR_KIND_PERSON and actor_user is None and not resolved_label:
        raise ValueError('Person audit entries require an actor_user or actor_label.')
    if not resolved_label:
        resolved_label = actor_kind.capitalize()

    resolved_object_id = '' if object_id is None else str(object_id)[:64]

    if record is not None:
        if not record_visibility:
            record_visibility = getattr(record, 'visibility', '') or ''
        if record_owner is None:
            record_owner = getattr(record, 'owner', None)
        if record_shared_role is None:
            record_shared_role = getattr(record, 'shared_role', None)

    return AuditLog.objects.create(
        workspace=workspace,
        actor_kind=actor_kind,
        actor_user=actor_user if actor_kind == AuditLog.ACTOR_KIND_PERSON else None,
        actor_label=resolved_label[:255],
        object_type=(object_type or '')[:64],
        object_id=resolved_object_id,
        record_label=(record_label or '')[:255],
        action=(action or AuditLog.ACTION_UPDATE)[:32],
        field_name=(field_name or '')[:120],
        previous_value=_json_safe_value(previous_value),
        new_value=_json_safe_value(new_value),
        record_visibility=(record_visibility or '')[:20],
        record_owner=record_owner,
        record_shared_role=record_shared_role,
        created_at=created_at or timezone.now(),
    )


def serialize_audit_entry(entry: AuditLog) -> dict:
    """Serialize an AuditLog record for JSON API consumers."""
    return {
        'id': entry.id,
        'actor': entry.actor_label,
        'actor_kind': entry.actor_kind,
        'actor_user_id': entry.actor_user_id,
        'object_type': entry.object_type,
        'object_id': entry.object_id,
        'record_label': entry.record_label,
        'action': entry.action,
        'field_name': entry.field_name,
        'previous_value': entry.previous_value,
        'new_value': entry.new_value,
        'created_at': entry.created_at.isoformat(),
    }


def _get_crm_models_by_key():
    from ..models import (
        Account,
        Activity,
        Appointment,
        Contact,
        Deal,
        Job,
        Lead,
        Ticket,
        Visit,
    )

    return {
        'account': Account,
        'contact': Contact,
        'lead': Lead,
        'deal': Deal,
        'activity': Activity,
        'job': Job,
        'appointment': Appointment,
        'visit': Visit,
        'ticket': Ticket,
    }


def _build_hidden_audit_field_names(object_key: str, hidden_schema_keys: set[str]) -> set[str]:
    from .role_service import OBJECT_FIELD_SCHEMAS

    if not hidden_schema_keys:
        return set()
    names: set[str] = set()
    for schema in OBJECT_FIELD_SCHEMAS.get(object_key, ()):
        if schema.key in hidden_schema_keys:
            names.add(schema.key)
            names.update(schema.payload_keys)
            names.update(schema.serialized_keys)
    if object_key == 'deal' and 'stage' in hidden_schema_keys:
        names.update({'stage', 'pipeline_stage', 'pipeline_stage_id'})
    return names


def _build_scoped_audit_queryset(workspace, user):
    """Build an AuditLog queryset strictly enforcing S2.4 object, field, and sharing rules."""
    from ..models import Activity, WorkspaceTable
    from .catalog_service import ensure_workspace_catalog
    from .role_service import (
        ALL_OBJECT_KEYS,
        FIELD_RIGHT_HIDDEN,
        PRIMARY_LABEL_FIELD_BY_OBJECT,
        build_record_visibility_q,
        get_user_field_rights,
        is_workspace_owner,
        resolve_effective_user_role,
        user_can_manage_users_and_roles,
        user_has_object_right,
    )

    ensure_workspace_catalog(workspace)
    enabled_keys = set(
        WorkspaceTable.objects.filter(
            workspace=workspace,
            is_enabled=True,
        ).values_list('object_key', flat=True)
    )
    is_owner = is_workspace_owner(user, workspace)
    can_manage_users = user_can_manage_users_and_roles(user, workspace)
    crm_models = _get_crm_models_by_key()

    qs = AuditLog.objects.filter(workspace=workspace)
    redacted_label_objects: set[str] = set()
    hidden_fields_by_obj: dict[str, set[str]] = {}
    readable_crm_keys: set[str] = set()

    if is_owner:
        readable_crm_keys = {k for k in crm_models if k in enabled_keys}
        return qs, {
            'is_owner': True,
            'can_manage_users': True,
            'enabled_keys': enabled_keys,
            'readable_crm_keys': readable_crm_keys,
            'hidden_fields_by_obj': hidden_fields_by_obj,
            'redacted_label_objects': redacted_label_objects,
            'deal_readable': 'deal' in enabled_keys,
            'deal_title_hidden': False,
        }

    # 1. Private security object types gated by users_and_roles switch
    if not can_manage_users:
        qs = qs.filter(
            Q(actor_user=user)
            | Q(object_type='user', object_id=str(user.id))
            | ~Q(object_type__in=PRIVATE_SECURITY_OBJECT_TYPES)
        )

    # 2. CRM object read rights and enabled table gating (applies to ALL non-Owners)
    excluded_object_types: set[str] = set()
    for obj_key in ALL_OBJECT_KEYS:
        if obj_key not in enabled_keys or not user_has_object_right(user, workspace, obj_key, 'read'):
            excluded_object_types.add(obj_key)
        elif obj_key in crm_models:
            readable_crm_keys.add(obj_key)

    deal_readable = 'deal' in readable_crm_keys
    if not deal_readable:
        excluded_object_types.update({'pipeline', 'pipeline_stage'})
        qs = qs.exclude(object_type='lead', field_name='converted_deal')

    if excluded_object_types:
        qs = qs.exclude(object_type__in=excluded_object_types)

    # 3. Hidden fields per readable CRM object
    for obj_key in readable_crm_keys:
        field_rights = get_user_field_rights(user, workspace, obj_key)
        hidden_keys = {
            f_key for f_key, level in field_rights.items() if level == FIELD_RIGHT_HIDDEN
        }
        if hidden_keys:
            hidden_fields_by_obj[obj_key] = hidden_keys
            audit_field_names = _build_hidden_audit_field_names(obj_key, hidden_keys)
            if audit_field_names:
                qs = qs.exclude(object_type=obj_key, field_name__in=audit_field_names)
            primary_field = PRIMARY_LABEL_FIELD_BY_OBJECT.get(obj_key)
            if primary_field and primary_field in hidden_keys:
                redacted_label_objects.add(obj_key)

    # 4. Owner / role sharing visibility on live CRM records (including Activity parent records)
    vis_q = build_record_visibility_q(user, workspace)
    user_role = resolve_effective_user_role(user, workspace)
    user_role_id = user_role.id if user_role is not None else None

    hidden_live_ids_by_obj: dict[str, set[str]] = {}
    for obj_key in ('account', 'contact', 'lead', 'deal', 'job', 'appointment', 'visit', 'ticket'):
        if obj_key not in readable_crm_keys:
            continue
        model_cls = crm_models[obj_key]
        hidden_ids = {
            str(pk)
            for pk in model_cls.objects.filter(workspace=workspace)
            .exclude(vis_q)
            .values_list('id', flat=True)
        }
        hidden_live_ids_by_obj[obj_key] = hidden_ids
        if hidden_ids:
            qs = qs.exclude(object_type=obj_key, object_id__in=hidden_ids)

    if 'activity' in readable_crm_keys:
        hidden_act_ids = {
            str(pk)
            for pk in Activity.objects.filter(workspace=workspace)
            .exclude(vis_q)
            .values_list('id', flat=True)
        }
        hidden_act_ids.update(
            str(pk)
            for pk in Activity.objects.filter(workspace=workspace)
            .exclude(target_object_type__in=readable_crm_keys)
            .values_list('id', flat=True)
        )
        for target_key, target_hidden_ids in hidden_live_ids_by_obj.items():
            if not target_hidden_ids:
                continue
            int_ids = [int(val) for val in target_hidden_ids if val.isdigit()]
            if int_ids:
                hidden_act_ids.update(
                    str(pk)
                    for pk in Activity.objects.filter(
                        workspace=workspace,
                        target_object_type=target_key,
                        target_object_id__in=int_ids,
                    ).values_list('id', flat=True)
                )
        if hidden_act_ids:
            qs = qs.exclude(object_type='activity', object_id__in=hidden_act_ids)

    # 5. Deleted CRM records: evaluate full history using final visibility at deletion time
    if readable_crm_keys:
        deleted_restricted_rows = (
            AuditLog.objects.filter(
                workspace=workspace,
                object_type__in=readable_crm_keys,
                action=AuditLog.ACTION_DELETE,
                record_visibility__in=('owner', 'role'),
            )
            .values_list(
                'object_type',
                'object_id',
                'record_visibility',
                'record_owner_id',
                'record_shared_role_id',
            )
        )
        hidden_deleted_by_obj: dict[str, set[str]] = {}
        for del_obj_type, del_obj_id, del_vis, del_owner_id, del_role_id in deleted_restricted_rows:
            if not del_obj_id:
                continue
            can_see_deleted = False
            if del_owner_id is not None and del_owner_id == user.id:
                can_see_deleted = True
            elif (
                del_vis == 'role'
                and del_role_id is not None
                and user_role_id is not None
                and del_role_id == user_role_id
            ):
                can_see_deleted = True
            if not can_see_deleted:
                hidden_deleted_by_obj.setdefault(del_obj_type, set()).add(del_obj_id)

        for del_obj_type, del_ids in hidden_deleted_by_obj.items():
            if del_ids:
                qs = qs.exclude(object_type=del_obj_type, object_id__in=del_ids)

    deal_title_hidden = 'title' in hidden_fields_by_obj.get('deal', set())
    return qs, {
        'is_owner': False,
        'can_manage_users': can_manage_users,
        'enabled_keys': enabled_keys,
        'readable_crm_keys': readable_crm_keys,
        'hidden_fields_by_obj': hidden_fields_by_obj,
        'redacted_label_objects': redacted_label_objects,
        'deal_readable': deal_readable,
        'deal_title_hidden': deal_title_hidden,
    }


def _apply_safe_audit_search(qs, query_text: str, scope_ctx: dict):
    """Filter audit queryset by search term without letting callers probe redacted hidden labels."""
    q = (query_text or '').strip()
    if not q:
        return qs

    redacted_objects = scope_ctx['redacted_label_objects']
    deal_title_hidden = scope_ctx['deal_title_hidden']

    qs = qs.annotate(
        _prev_val_text=Cast('previous_value', TextField()),
        _new_val_text=Cast('new_value', TextField()),
    )

    search_q = (
        Q(actor_label__icontains=q)
        | Q(field_name__icontains=q)
        | Q(object_type__icontains=q)
        | Q(object_id__icontains=q)
    )

    if redacted_objects:
        search_q |= Q(record_label__icontains=q) & ~Q(object_type__in=redacted_objects)
    else:
        search_q |= Q(record_label__icontains=q)

    value_match_q = Q(_prev_val_text__icontains=q) | Q(_new_val_text__icontains=q)
    value_exclude_q = Q()
    if redacted_objects:
        value_exclude_q |= Q(object_type__in=redacted_objects, field_name='record')
    if deal_title_hidden:
        value_exclude_q |= Q(object_type='lead', field_name='converted_deal')

    if value_exclude_q:
        search_q |= value_match_q & ~value_exclude_q
    else:
        search_q |= value_match_q

    return qs.filter(search_q)


def _find_inspectable_live_record_ids(workspace, user, entries: list[AuditLog], scope_ctx: dict) -> dict[str, set[str]]:
    """Batch-check which inspectable CRM records referenced on the current page still exist & are visible."""
    from .role_service import build_record_visibility_q

    crm_models = _get_crm_models_by_key()
    readable_keys = scope_ctx['readable_crm_keys']
    candidate_ids_by_obj: dict[str, set[int]] = {}
    for entry in entries:
        obj_type = entry.object_type
        if (
            obj_type in INSPECTABLE_CRM_PANE_BY_KEY
            and obj_type in readable_keys
            and entry.object_id
            and entry.object_id.isdigit()
        ):
            candidate_ids_by_obj.setdefault(obj_type, set()).add(int(entry.object_id))

    if not candidate_ids_by_obj:
        return {}

    vis_q = Q() if scope_ctx['is_owner'] else build_record_visibility_q(user, workspace)
    live_ids_by_obj: dict[str, set[str]] = {}
    for obj_type, ids in candidate_ids_by_obj.items():
        model_cls = crm_models[obj_type]
        found = (
            model_cls.objects.filter(workspace=workspace, id__in=ids)
            .filter(vis_q)
            .values_list('id', flat=True)
        )
        live_ids_by_obj[obj_type] = {str(pk) for pk in found}
    return live_ids_by_obj


def _serialize_scoped_audit_entry(
    entry: AuditLog,
    scope_ctx: dict,
    live_ids_by_obj: dict[str, set[str]],
) -> dict:
    payload = serialize_audit_entry(entry)
    obj_type = entry.object_type
    prefix = RECORD_CODE_PREFIX_BY_KEY.get(obj_type, obj_type.upper()[:3])
    code = f'{prefix}-{entry.object_id}' if entry.object_id else prefix

    if obj_type in scope_ctx['redacted_label_objects']:
        payload['record_label'] = code
        if entry.field_name == 'record':
            if payload['previous_value'] is not None:
                payload['previous_value'] = code
            if payload['new_value'] is not None:
                payload['new_value'] = code

    if (
        obj_type == 'lead'
        and entry.field_name == 'converted_deal'
        and scope_ctx['deal_title_hidden']
        and isinstance(payload['new_value'], str)
        and ' (' in payload['new_value']
    ):
        payload['new_value'] = payload['new_value'].split(' (', 1)[0]

    can_inspect = bool(
        obj_type in INSPECTABLE_CRM_PANE_BY_KEY
        and entry.object_id in live_ids_by_obj.get(obj_type, set())
    )
    payload['can_inspect'] = can_inspect
    payload['inspect_pane_id'] = INSPECTABLE_CRM_PANE_BY_KEY[obj_type] if can_inspect else None
    return payload


def _build_available_object_types(workspace, scope_ctx: dict) -> list[dict[str, str]]:
    """Return filterable audit object types adapted to workspace packs, caller rights, and contact label."""
    contact_label = (getattr(workspace, 'contact_label', '') or 'Contact').strip() or 'Contact'
    readable_crm = scope_ctx['readable_crm_keys']
    options: list[dict[str, str]] = []

    crm_order = ('account', 'contact', 'lead', 'deal', 'job', 'appointment', 'visit', 'ticket', 'activity')
    for key in crm_order:
        if key in readable_crm:
            label = contact_label if key == 'contact' else OBJECTS_BY_KEY[key].label
            options.append({'key': key, 'label': label})

    if scope_ctx['deal_readable']:
        options.append({'key': 'pipeline', 'label': SYSTEM_OBJECT_LABELS['pipeline']})
        options.append({'key': 'pipeline_stage', 'label': SYSTEM_OBJECT_LABELS['pipeline_stage']})

    for key in ('workspace', 'workspace_pack', 'workspace_table', 'role'):
        options.append({'key': key, 'label': SYSTEM_OBJECT_LABELS[key]})

    options.append({'key': 'user', 'label': SYSTEM_OBJECT_LABELS['user']})
    if scope_ctx['can_manage_users']:
        for key in ('workspace_invite', 'external_identity', 'passkey'):
            options.append({'key': key, 'label': SYSTEM_OBJECT_LABELS[key]})

    return options


def _validate_common_audit_filters(*, actor_kind: str = '', action: str = '', object_type: str = ''):
    norm_actor = (actor_kind or '').strip().lower()
    if norm_actor and norm_actor not in VALID_ACTOR_KINDS:
        raise AuditQueryError('Invalid actor_kind filter.', status_code=400)

    norm_action = (action or '').strip().lower()
    if norm_action and norm_action not in VALID_AUDIT_ACTIONS:
        raise AuditQueryError('Invalid action filter. Choose create, update, or delete.', status_code=400)

    norm_obj = (object_type or '').strip().lower()
    if norm_obj:
        norm_obj = AUDIT_OBJECT_TYPE_ALIASES.get(norm_obj, norm_obj)
        if norm_obj not in KNOWN_AUDIT_OBJECT_TYPES:
            raise AuditQueryError('Invalid object_type filter.', status_code=400)

    return norm_actor, norm_action, norm_obj


def list_workspace_audit_entries(
    workspace,
    user,
    *,
    page: int = 1,
    page_size: int = DEFAULT_AUDIT_PAGE_SIZE,
    actor_kind: str = '',
    object_type: str = '',
    action: str = '',
    q: str = '',
) -> dict:
    """Return paginated, filtered, strictly S2.4-scoped workspace audit log entries."""
    norm_actor, norm_action, norm_obj = _validate_common_audit_filters(
        actor_kind=actor_kind,
        action=action,
        object_type=object_type,
    )

    qs, scope_ctx = _build_scoped_audit_queryset(workspace, user)

    if norm_actor:
        qs = qs.filter(actor_kind=norm_actor)
    if norm_action:
        qs = qs.filter(action=norm_action)
    if norm_obj:
        qs = qs.filter(object_type=norm_obj)
    if q and q.strip():
        qs = _apply_safe_audit_search(qs, q, scope_ctx)

    qs = qs.order_by('-created_at', '-id')
    total_count = qs.count()
    total_pages = max(1, math.ceil(total_count / page_size)) if total_count else 1
    offset = (page - 1) * page_size
    entries = list(qs[offset : offset + page_size])
    live_ids_by_obj = _find_inspectable_live_record_ids(workspace, user, entries, scope_ctx)

    return {
        'results': [
            _serialize_scoped_audit_entry(entry, scope_ctx, live_ids_by_obj)
            for entry in entries
        ],
        'total_count': total_count,
        'page': page,
        'page_size': page_size,
        'total_pages': total_pages,
        'has_next': page < total_pages,
        'has_previous': page > 1,
        'available_object_types': _build_available_object_types(workspace, scope_ctx),
    }


def list_record_audit_entries(
    workspace,
    user,
    object_key: str,
    record_id: int,
    *,
    page: int = 1,
    page_size: int = DEFAULT_RECORD_AUDIT_PAGE_SIZE,
    actor_kind: str = '',
    action: str = '',
    q: str = '',
) -> dict:
    """Return paginated, strictly S2.4-scoped audit change history for a single CRM record."""
    from .crm_service import (
        CRM_MODELS_BY_KEY,
        ensure_object_permission,
        resolve_object_key,
    )
    from .role_service import build_record_visibility_q
    from ..helpers.crm_errors import CrmDomainError

    canonical_key = resolve_object_key(object_key)
    ensure_object_permission(user, workspace, canonical_key, 'read')

    model_cls = CRM_MODELS_BY_KEY[canonical_key]
    if (
        not model_cls.objects.filter(workspace=workspace, id=record_id)
        .filter(build_record_visibility_q(user, workspace))
        .exists()
    ):
        raise CrmDomainError(f'{OBJECTS_BY_KEY[canonical_key].label} not found.', status_code=404)

    norm_actor, norm_action, _ = _validate_common_audit_filters(
        actor_kind=actor_kind,
        action=action,
    )

    qs, scope_ctx = _build_scoped_audit_queryset(workspace, user)
    qs = qs.filter(object_type=canonical_key, object_id=str(record_id))
    if norm_actor:
        qs = qs.filter(actor_kind=norm_actor)
    if norm_action:
        qs = qs.filter(action=norm_action)
    if q and q.strip():
        qs = _apply_safe_audit_search(qs, q, scope_ctx)

    qs = qs.order_by('-created_at', '-id')
    total_count = qs.count()
    total_pages = max(1, math.ceil(total_count / page_size)) if total_count else 1
    offset = (page - 1) * page_size
    entries = list(qs[offset : offset + page_size])
    live_ids_by_obj = {canonical_key: {str(record_id)}}

    return {
        'results': [
            _serialize_scoped_audit_entry(entry, scope_ctx, live_ids_by_obj)
            for entry in entries
        ],
        'total_count': total_count,
        'page': page,
        'page_size': page_size,
        'total_pages': total_pages,
        'has_next': page < total_pages,
        'has_previous': page > 1,
    }
