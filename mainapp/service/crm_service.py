from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import math
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from ..catalog import OBJECTS_BY_KEY
from ..helpers.crm_errors import CrmDomainError
from ..models import (
    Account,
    Activity,
    Appointment,
    AuditLog,
    Contact,
    Deal,
    Job,
    Lead,
    PipelineStage,
    Role,
    Ticket,
    User,
    Visit,
    WorkspaceRecordBase,
    WorkspaceTable,
)
from .audit_service import list_workspace_audit_entries, record_audit_event
from .cache_service import (
    DOMAIN_CATALOG,
    DOMAIN_LOOKUP_RECORDS,
    DOMAIN_MEMBERS,
    DOMAIN_RECORDS,
    DOMAIN_ROLES,
    build_caller_cache_scope,
    get_or_set_workspace_cache_bundle,
)
from .catalog_service import ensure_workspace_catalog, get_workspace_table_record_counts
from .pipeline_service import (
    ensure_workspace_pipeline,
    get_ordered_pipeline_stages,
    resolve_deal_stage,
    serialize_pipeline_stage,
)
from .role_service import (
    FIELD_RIGHT_EDITABLE,
    FIELD_RIGHT_HIDDEN,
    FIELD_RIGHT_READ_ONLY,
    OBJECT_FIELD_SCHEMAS,
    PRIMARY_LABEL_FIELD_BY_OBJECT,
    build_record_visibility_q,
    ensure_workspace_default_roles,
    get_field_right,
    get_user_field_rights,
    is_workspace_owner,
    user_can_manage_record_sharing,
    user_can_manage_sales_settings,
    user_can_see_record,
    user_has_object_right,
)

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
DEFAULT_BOARD_PER_STAGE_LIMIT = 100

OBJECT_KEY_ALIASES: dict[str, str] = {
    'account': 'account',
    'accounts': 'account',
    'contact': 'contact',
    'contacts': 'contact',
    'lead': 'lead',
    'leads': 'lead',
    'deal': 'deal',
    'deals': 'deal',
    'activity': 'activity',
    'activities': 'activity',
    'job': 'job',
    'jobs': 'job',
    'appointment': 'appointment',
    'appointments': 'appointment',
    'visit': 'visit',
    'visits': 'visit',
    'ticket': 'ticket',
    'tickets': 'ticket',
}

RECORD_CODE_PREFIXES: dict[str, str] = {
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

CRM_MODELS_BY_KEY = {
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


def resolve_object_key(raw_key: str) -> str:
    """Resolve singular or plural S2.1 CRM object key or raise 404."""
    normalized = (raw_key or '').strip().lower()
    canonical = OBJECT_KEY_ALIASES.get(normalized)
    if canonical is None:
        raise CrmDomainError('CRM object not found.', status_code=404)
    return canonical


def ensure_object_table_enabled(workspace, object_key: str) -> None:
    """Ensure the requested CRM object table is active in this workspace."""
    ensure_workspace_catalog(workspace)
    is_enabled = WorkspaceTable.objects.filter(
        workspace=workspace,
        object_key=object_key,
        is_enabled=True,
    ).exists()
    if not is_enabled:
        label = OBJECTS_BY_KEY[object_key].label
        raise CrmDomainError(
            f'{label} table is not enabled in this workspace.',
            status_code=404,
        )


def ensure_object_permission(user, workspace, object_key: str, action: str) -> None:
    """Enforce table enablement first, then caller role CRUD permission."""
    ensure_object_table_enabled(workspace, object_key)
    if not user_has_object_right(user, workspace, object_key, action):
        label = OBJECTS_BY_KEY[object_key].label
        raise CrmDomainError(
            f'Your role does not have {action} permission for {label}.',
            status_code=403,
        )


def _get_contact_screen_label(workspace) -> str:
    return (getattr(workspace, 'contact_label', '') or 'Contact').strip() or 'Contact'


def _has_any_key(payload: dict, *keys: str) -> bool:
    return any(k in payload for k in keys)


def _first_present(payload: dict, *keys: str, default=None):
    for key in keys:
        if key in payload:
            return payload[key]
    return default


def _parse_required_str(payload: dict, *keys: str, label: str, max_length: int = 255) -> str:
    raw = _first_present(payload, *keys)
    if not isinstance(raw, str) or not raw.strip():
        raise CrmDomainError(f'{label} is required.', status_code=400)
    cleaned = ' '.join(raw.strip().split())
    if len(cleaned) > max_length:
        raise CrmDomainError(
            f'{label} must be {max_length} characters or fewer.',
            status_code=400,
        )
    return cleaned


def _parse_optional_str(
    payload: dict,
    *keys: str,
    label: str,
    max_length: int | None = None,
    preserve_newlines: bool = False,
) -> str:
    raw = _first_present(payload, *keys)
    if raw is None:
        return ''
    if not isinstance(raw, str):
        raise CrmDomainError(f'{label} must be text.', status_code=400)
    cleaned = raw.strip() if preserve_newlines else ' '.join(raw.strip().split())
    if max_length is not None and len(cleaned) > max_length:
        raise CrmDomainError(
            f'{label} must be {max_length} characters or fewer.',
            status_code=400,
        )
    return cleaned


def _parse_optional_email(payload: dict, *keys: str) -> str:
    raw = _first_present(payload, *keys)
    if raw is None:
        return ''
    if not isinstance(raw, str):
        raise CrmDomainError('Email must be a string.', status_code=400)
    cleaned = raw.strip().lower()
    if not cleaned:
        return ''
    try:
        validate_email(cleaned)
    except ValidationError as exc:
        raise CrmDomainError('Enter a valid email address.', status_code=400) from exc
    return cleaned


def _parse_bool(raw, *, field_name: str, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int) and raw in (0, 1):
        return bool(raw)
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in ('true', '1', 'yes', 'on'):
            return True
        if lowered in ('false', '0', 'no', 'off', ''):
            return False
    raise CrmDomainError(f'{field_name} must be a boolean.', status_code=400)


def _parse_decimal(
    raw,
    *,
    field_name: str,
    allow_null: bool = False,
    default: Decimal | None = None,
) -> Decimal | None:
    if raw is None or raw == '':
        if allow_null:
            return None
        if default is not None:
            return default
        raise CrmDomainError(f'{field_name} is required.', status_code=400)
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise CrmDomainError(f'{field_name} must be a valid number.', status_code=400) from exc
    if not value.is_finite() or value < Decimal('0'):
        raise CrmDomainError(f'{field_name} must be zero or greater.', status_code=400)
    return value.quantize(Decimal('0.01'))


def _parse_int_range(
    raw,
    *,
    field_name: str,
    min_val: int = 0,
    max_val: int = 100,
    allow_null: bool = True,
) -> int | None:
    if raw is None or raw == '':
        if allow_null:
            return None
        raise CrmDomainError(f'{field_name} is required.', status_code=400)
    if isinstance(raw, bool):
        raise CrmDomainError(f'{field_name} must be an integer.', status_code=400)
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise CrmDomainError(f'{field_name} must be an integer.', status_code=400) from exc
    if parsed < min_val or parsed > max_val:
        raise CrmDomainError(
            f'{field_name} must be between {min_val} and {max_val}.',
            status_code=400,
        )
    return parsed


def _parse_optional_date(raw, *, field_name: str):
    if raw is None or raw == '':
        return None
    if not isinstance(raw, str):
        raise CrmDomainError(f'{field_name} must be a YYYY-MM-DD date string.', status_code=400)
    parsed = parse_date(raw.strip())
    if parsed is None:
        raise CrmDomainError(f'{field_name} must be formatted as YYYY-MM-DD.', status_code=400)
    return parsed


def _parse_datetime_value(
    raw,
    *,
    field_name: str,
    user_timezone: str = 'Africa/Accra',
    required: bool = False,
):
    if raw is None or raw == '':
        if required:
            raise CrmDomainError(f'{field_name} is required.', status_code=400)
        return None
    if not isinstance(raw, str):
        raise CrmDomainError(f'{field_name} must be an ISO date/time string.', status_code=400)
    cleaned = raw.strip()
    if not cleaned:
        if required:
            raise CrmDomainError(f'{field_name} is required.', status_code=400)
        return None

    dt = parse_datetime(cleaned)
    if dt is None:
        d = parse_date(cleaned)
        if d is not None:
            dt = datetime.combine(d, time(9, 0))
    if dt is None:
        raise CrmDomainError(f'{field_name} must be a valid date and time.', status_code=400)

    if timezone.is_naive(dt):
        try:
            tz = ZoneInfo(user_timezone or 'Africa/Accra')
        except Exception:
            tz = ZoneInfo('Africa/Accra')
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(ZoneInfo('UTC'))


def _parse_fk_id(raw, *, field_name: str) -> int | None:
    if raw is None or raw == '':
        return None
    if isinstance(raw, dict):
        raw = raw.get('id')
        if raw is None or raw == '':
            return None
    if isinstance(raw, bool):
        raise CrmDomainError(f'{field_name} must be a valid ID.', status_code=400)
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise CrmDomainError(f'{field_name} must be a valid ID.', status_code=400) from exc
    if parsed < 1:
        raise CrmDomainError(f'{field_name} must be a valid ID.', status_code=400)
    return parsed


VALID_VISIBILITY_VALUES: tuple[str, ...] = (
    WorkspaceRecordBase.VISIBILITY_WORKSPACE,
    WorkspaceRecordBase.VISIBILITY_ROLE,
    WorkspaceRecordBase.VISIBILITY_OWNER,
)

REQUIRED_CREATE_FIELDS_BY_OBJECT: dict[str, tuple[str, ...]] = {
    'account': ('name',),
    'contact': ('name',),
    'lead': ('name',),
    'deal': ('title',),
    'job': ('title',),
    'appointment': ('contact', 'scheduled_at'),
    'visit': ('contact', 'date', 'reason'),
    'ticket': ('title',),
    'activity': ('activity_type', 'summary'),
}

FORBIDDEN_VISIT_CLINICAL_KEYS: frozenset[str] = frozenset(
    {
        'diagnosis',
        'diagnosis_code',
        'icd_code',
        'chart',
        'medical_chart',
        'lab',
        'labs',
        'lab_result',
        'lab_results',
        'prescription',
        'prescriptions',
        'medication',
        'medications',
    }
)


def _reject_forbidden_visit_fields(payload: dict) -> None:
    """Explicitly refuse clinical/EHR keys on Visit payloads (S2.6 guardrail)."""
    for key in payload.keys():
        if isinstance(key, str) and key.strip().lower() in FORBIDDEN_VISIT_CLINICAL_KEYS:
            raise CrmDomainError(
                'Visits do not store diagnosis codes, charts, lab results, or prescriptions.',
                status_code=400,
            )


def _ensure_writable_fields(
    user,
    workspace,
    object_key: str,
    payload: dict,
    *,
    is_create: bool = False,
) -> None:
    """Reject writes to hidden or read-only fields for non-Owner roles."""
    if is_workspace_owner(user, workspace):
        return

    field_rights = get_user_field_rights(user, workspace, object_key)
    schemas = OBJECT_FIELD_SCHEMAS.get(object_key, ())
    contact_label = _get_contact_screen_label(workspace)

    for schema in schemas:
        level = field_rights.get(schema.key, FIELD_RIGHT_EDITABLE)
        if level == FIELD_RIGHT_EDITABLE:
            continue
        label = contact_label if schema.uses_contact_label else schema.label
        level_label = 'hidden' if level == FIELD_RIGHT_HIDDEN else 'read-only'
        if any(k in payload for k in schema.payload_keys):
            raise CrmDomainError(
                f'Your role cannot modify {label} because this field is {level_label}.',
                status_code=403,
            )
        if is_create and schema.key in REQUIRED_CREATE_FIELDS_BY_OBJECT.get(object_key, ()):
            raise CrmDomainError(
                f'Your role cannot set required field {label} because it is {level_label}.',
                status_code=403,
            )


def _resolve_sharing_fields(
    workspace,
    payload: dict,
    *,
    existing=None,
    user=None,
) -> tuple[str, Role | None, bool]:
    """Parse and authorize record visibility and shared_role."""
    explicit = _has_any_key(payload, 'visibility', 'shared_role_id', 'shared_role')
    if not explicit:
        if existing is not None:
            return existing.visibility, existing.shared_role, False
        return WorkspaceRecordBase.VISIBILITY_WORKSPACE, None, False

    raw_vis = payload.get('visibility')
    if raw_vis is None or raw_vis == '':
        visibility = existing.visibility if existing is not None else WorkspaceRecordBase.VISIBILITY_WORKSPACE
    else:
        if not isinstance(raw_vis, str):
            raise CrmDomainError('Visibility must be owner, role, or workspace.', status_code=400)
        visibility = raw_vis.strip().lower()
        if visibility not in VALID_VISIBILITY_VALUES:
            raise CrmDomainError(
                'Visibility must be one of: owner, role, workspace.',
                status_code=400,
            )

    shared_role = None
    if visibility == WorkspaceRecordBase.VISIBILITY_ROLE:
        if _has_any_key(payload, 'shared_role_id', 'shared_role'):
            raw_role = _first_present(payload, 'shared_role_id', 'shared_role')
            role_id = _parse_fk_id(raw_role, field_name='Shared role')
        elif existing is not None and existing.shared_role_id is not None:
            role_id = existing.shared_role_id
        else:
            role_id = None

        if role_id is None:
            raise CrmDomainError(
                'Select a workspace role when sharing a record with one role.',
                status_code=400,
            )
        shared_role = Role.objects.filter(workspace=workspace, id=role_id).first()
        if shared_role is None:
            raise CrmDomainError(
                'Selected sharing role was not found in this workspace.',
                status_code=400,
            )

    if existing is not None and user is not None:
        next_role_id = shared_role.id if shared_role is not None else None
        if visibility != existing.visibility or next_role_id != existing.shared_role_id:
            if not user_can_manage_record_sharing(user, workspace, existing):
                raise CrmDomainError(
                    'Only the record owner, workspace Owner, or a role with users-and-roles access can change sharing settings.',
                    status_code=403,
                )

    return visibility, shared_role, True


def _resolve_account(
    workspace,
    payload: dict,
    *,
    existing=None,
    user=None,
) -> tuple[Account | None, bool]:
    explicit = _has_any_key(payload, 'account_id', 'account')
    if not explicit:
        return (existing.account if existing is not None else None), False
    raw = _first_present(payload, 'account_id', 'account')
    pk = _parse_fk_id(raw, field_name='Account')
    if pk is None:
        return None, True
    qs = Account.objects.filter(workspace=workspace, id=pk)
    if user is not None:
        qs = qs.filter(build_record_visibility_q(user, workspace))
    account = qs.first()
    if account is None:
        raise CrmDomainError('Selected Account was not found in this workspace.', status_code=400)
    return account, True


def _resolve_contact(
    workspace,
    payload: dict,
    *,
    existing=None,
    user=None,
) -> tuple[Contact | None, bool]:
    contact_label = _get_contact_screen_label(workspace)
    explicit = _has_any_key(payload, 'contact_id', 'contact')
    if not explicit:
        return (existing.contact if existing is not None else None), False
    raw = _first_present(payload, 'contact_id', 'contact')
    pk = _parse_fk_id(raw, field_name=contact_label)
    if pk is None:
        return None, True
    qs = Contact.objects.select_related('account').filter(workspace=workspace, id=pk)
    if user is not None:
        qs = qs.filter(build_record_visibility_q(user, workspace))
    contact = qs.first()
    if contact is None:
        raise CrmDomainError(
            f'Selected {contact_label} was not found in this workspace.',
            status_code=400,
        )
    return contact, True


def _resolve_assignee(workspace, payload: dict, *, existing=None) -> User | None:
    if not _has_any_key(payload, 'assignee_id', 'assignee'):
        return existing.assignee if existing is not None else None
    raw = _first_present(payload, 'assignee_id', 'assignee')
    pk = _parse_fk_id(raw, field_name='Assignee')
    if pk is None:
        return None
    member = User.objects.filter(
        workspace=workspace,
        id=pk,
        is_active=True,
        email_verified=True,
    ).first()
    if member is None:
        raise CrmDomainError(
            'Selected assignee must be an active verified member of this workspace.',
            status_code=400,
        )
    return member


def _resolve_account_and_contact_pair(
    workspace,
    payload: dict,
    *,
    existing=None,
    user=None,
) -> tuple[Account | None, Contact | None]:
    """Resolve linked Account and Contact with auto-fill and mismatch enforcement."""
    contact_label = _get_contact_screen_label(workspace)
    account, account_explicit = _resolve_account(workspace, payload, existing=existing, user=user)
    contact, contact_explicit = _resolve_contact(workspace, payload, existing=existing, user=user)

    if contact is not None and contact.account_id is not None:
        if not account_explicit:
            # Auto-fill Account from Contact when omitted or when Contact changed without specifying Account.
            if existing is None or contact_explicit or account is None:
                if user is None or user_can_see_record(user, workspace, contact.account):
                    account = contact.account
        elif account is None:
            raise CrmDomainError(
                f'Cannot clear Account while the linked {contact_label} belongs to {contact.account.name}.',
                status_code=400,
            )
        elif account.id != contact.account_id:
            raise CrmDomainError(
                f'Selected {contact_label} belongs to {contact.account.name}, not {account.name}.',
                status_code=400,
            )

    return account, contact


def _validate_choice(value: str, valid_choices: tuple[str, ...], *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CrmDomainError(f'{field_name} is required.', status_code=400)
    cleaned = ' '.join(value.strip().split())
    by_lower = {choice.lower(): choice for choice in valid_choices}
    canonical = by_lower.get(cleaned.lower())
    if canonical is None:
        raise CrmDomainError(
            f'{field_name} must be one of: {", ".join(valid_choices)}.',
            status_code=400,
        )
    return canonical


def get_record_label(record, object_key: str) -> str:
    """Return a concise human-readable label for a CRM record."""
    if object_key in ('account', 'contact', 'lead'):
        return record.name
    if object_key in ('deal', 'job', 'appointment', 'ticket'):
        return record.title
    if object_key == 'visit':
        return record.reason
    if object_key == 'activity':
        snippet = (record.summary or '').strip()
        if len(snippet) > 60:
            snippet = snippet[:57] + '...'
        return f"{record.activity_type.title()}: {snippet}"
    return str(record)


def _format_audit_field_value(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return f'{value:.2f}'
    if isinstance(value, Account):
        return value.name
    if isinstance(value, Contact):
        return value.name
    if isinstance(value, User):
        return value.email
    if isinstance(value, Role):
        return value.name
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)


def _resolve_activity_target(workspace, user, target_type_raw, target_id_raw):
    """Validate an Activity target record inside the same workspace and caller visibility."""
    if not isinstance(target_type_raw, str) or not target_type_raw.strip():
        raise CrmDomainError('target_object_type is required for an Activity.', status_code=400)
    normalized_type = OBJECT_KEY_ALIASES.get(target_type_raw.strip().lower())
    if normalized_type is None or normalized_type not in Activity.VALID_TARGET_OBJECTS:
        raise CrmDomainError(
            f'Activity target_object_type must be one of: {", ".join(Activity.VALID_TARGET_OBJECTS)}.',
            status_code=400,
        )

    ensure_object_table_enabled(workspace, normalized_type)
    if not user_has_object_right(user, workspace, normalized_type, 'read'):
        raise CrmDomainError(
            f'Your role does not have read permission for {OBJECTS_BY_KEY[normalized_type].label}.',
            status_code=403,
        )

    target_id = _parse_fk_id(target_id_raw, field_name='target_object_id')
    if target_id is None:
        raise CrmDomainError('target_object_id is required for an Activity.', status_code=400)

    target_model = CRM_MODELS_BY_KEY[normalized_type]
    target_record = (
        target_model.objects.filter(workspace=workspace, id=target_id)
        .filter(build_record_visibility_q(user, workspace))
        .first()
    )
    if target_record is None:
        raise CrmDomainError('Target record not found in this workspace.', status_code=404)

    return normalized_type, target_id, target_record


def _serialize_user_ref(user_obj: User | None) -> dict | None:
    if user_obj is None:
        return None
    return {'id': user_obj.id, 'email': user_obj.email}


def _serialize_role_ref(role_obj: Role | None) -> dict | None:
    if role_obj is None:
        return None
    return {
        'id': role_obj.id,
        'name': role_obj.name,
        'is_owner': bool(role_obj.is_owner),
        'is_default_staff': bool(role_obj.is_default_staff),
    }


def _get_visible_record_label(record, object_key: str, *, user=None, workspace=None) -> str:
    """Return display label or record_code fallback when the primary label field is hidden."""
    primary_field = PRIMARY_LABEL_FIELD_BY_OBJECT.get(object_key)
    if (
        primary_field
        and user is not None
        and workspace is not None
        and get_field_right(user, workspace, object_key, primary_field) == FIELD_RIGHT_HIDDEN
    ):
        return f'{RECORD_CODE_PREFIXES[object_key]}-{record.id}'
    return get_record_label(record, object_key)


def _strip_hidden_fields(
    data: dict,
    object_key: str,
    *,
    user=None,
    workspace=None,
) -> dict:
    """Remove any field keys marked hidden for the caller's role on object_key."""
    if user is None or workspace is None or is_workspace_owner(user, workspace):
        return data
    field_rights = get_user_field_rights(user, workspace, object_key)
    for schema in OBJECT_FIELD_SCHEMAS.get(object_key, ()):
        if field_rights.get(schema.key) == FIELD_RIGHT_HIDDEN:
            for key in schema.serialized_keys:
                data.pop(key, None)
    return data


def _serialize_account_ref(account: Account | None, *, user=None, workspace=None) -> dict | None:
    if account is None:
        return None
    if user is not None and workspace is not None:
        if not user_can_see_record(user, workspace, account):
            return None
        ref = {'id': account.id}
        if get_field_right(user, workspace, 'account', 'name') != FIELD_RIGHT_HIDDEN:
            ref['name'] = account.name
        else:
            ref['name'] = f'{RECORD_CODE_PREFIXES["account"]}-{account.id}'
        if get_field_right(user, workspace, 'account', 'city') != FIELD_RIGHT_HIDDEN:
            ref['city'] = account.city
        return ref
    return {'id': account.id, 'name': account.name, 'city': account.city}


def _serialize_contact_ref(contact: Contact | None, *, user=None, workspace=None) -> dict | None:
    if contact is None:
        return None
    if user is not None and workspace is not None:
        if not user_can_see_record(user, workspace, contact):
            return None
        ref = {'id': contact.id}
        if get_field_right(user, workspace, 'contact', 'name') != FIELD_RIGHT_HIDDEN:
            ref['name'] = contact.name
        else:
            ref['name'] = f'{RECORD_CODE_PREFIXES["contact"]}-{contact.id}'
        if get_field_right(user, workspace, 'contact', 'email') != FIELD_RIGHT_HIDDEN:
            ref['email'] = contact.email
        if get_field_right(user, workspace, 'contact', 'phone') != FIELD_RIGHT_HIDDEN:
            ref['phone'] = contact.phone
        if get_field_right(user, workspace, 'contact', 'account') != FIELD_RIGHT_HIDDEN:
            ref['account_id'] = contact.account_id
        return ref
    return {
        'id': contact.id,
        'name': contact.name,
        'email': contact.email,
        'phone': contact.phone,
        'account_id': contact.account_id,
    }


def _serialize_lead_ref(lead: Lead | None, *, user=None, workspace=None) -> dict | None:
    if lead is None:
        return None
    if user is not None and workspace is not None:
        if not user_can_see_record(user, workspace, lead):
            return None
        ref = {
            'id': lead.id,
            'record_code': f'{RECORD_CODE_PREFIXES["lead"]}-{lead.id}',
            'name': (
                lead.name
                if get_field_right(user, workspace, 'lead', 'name') != FIELD_RIGHT_HIDDEN
                else f'{RECORD_CODE_PREFIXES["lead"]}-{lead.id}'
            ),
        }
        if get_field_right(user, workspace, 'lead', 'company') != FIELD_RIGHT_HIDDEN:
            ref['company'] = lead.company
        if get_field_right(user, workspace, 'lead', 'email') != FIELD_RIGHT_HIDDEN:
            ref['email'] = lead.email
        if get_field_right(user, workspace, 'lead', 'phone') != FIELD_RIGHT_HIDDEN:
            ref['phone'] = lead.phone
        if get_field_right(user, workspace, 'lead', 'status') != FIELD_RIGHT_HIDDEN:
            ref['status'] = lead.status
        return ref
    return {
        'id': lead.id,
        'record_code': f'{RECORD_CODE_PREFIXES["lead"]}-{lead.id}',
        'name': lead.name,
        'company': lead.company,
        'email': lead.email,
        'phone': lead.phone,
        'status': lead.status,
    }


_STARTER_STAGE_TONES = {
    name.lower(): tone for name, tone in PipelineStage.STARTER_STAGE_SPECS
}


def _resolve_deal_stage_tone(deal: Deal) -> str:
    if getattr(deal, 'pipeline_stage_id', None) and getattr(deal, 'pipeline_stage', None):
        return deal.pipeline_stage.tone
    return _STARTER_STAGE_TONES.get((deal.stage or '').strip().lower(), PipelineStage.TONE_NEUTRAL)


def _serialize_deal_ref(deal: Deal | None, *, user=None, workspace=None) -> dict | None:
    if deal is None:
        return None
    if user is not None and workspace is not None:
        if not user_can_see_record(user, workspace, deal):
            return None
        ref = {
            'id': deal.id,
            'record_code': f'{RECORD_CODE_PREFIXES["deal"]}-{deal.id}',
            'title': (
                deal.title
                if get_field_right(user, workspace, 'deal', 'title') != FIELD_RIGHT_HIDDEN
                else f'{RECORD_CODE_PREFIXES["deal"]}-{deal.id}'
            ),
        }
        if get_field_right(user, workspace, 'deal', 'stage') != FIELD_RIGHT_HIDDEN:
            ref['stage'] = deal.stage
            ref['stage_tone'] = _resolve_deal_stage_tone(deal)
        if get_field_right(user, workspace, 'deal', 'amount') != FIELD_RIGHT_HIDDEN:
            ref['amount'] = f'{deal.amount:.2f}'
        return ref
    return {
        'id': deal.id,
        'record_code': f'{RECORD_CODE_PREFIXES["deal"]}-{deal.id}',
        'title': deal.title,
        'stage': deal.stage,
        'stage_tone': _resolve_deal_stage_tone(deal),
        'amount': f'{deal.amount:.2f}',
    }


def is_lead_conversion_available(workspace) -> bool:
    """Return True when the workspace has Account, Contact, Lead, and Deal enabled."""
    if workspace is None:
        return False
    ensure_workspace_catalog(workspace)
    enabled = set(
        WorkspaceTable.objects.filter(
            workspace=workspace,
            object_key__in=('account', 'contact', 'lead', 'deal'),
            is_enabled=True,
        ).values_list('object_key', flat=True)
    )
    return enabled == {'account', 'contact', 'lead', 'deal'}


def get_lead_conversion_capability(user, workspace) -> dict:
    """Compute whether the workspace supports lead conversion and what the caller role can do."""
    available = is_lead_conversion_available(workspace)
    if not available:
        return {
            'conversion_available': False,
            'can_convert_lead': False,
            'can_update_lead': False,
            'can_create_account': False,
            'can_reuse_account': False,
            'can_create_contact': False,
            'can_reuse_contact': False,
            'can_update_contact': False,
            'can_create_deal': False,
        }

    can_update_lead = user_has_object_right(user, workspace, 'lead', 'update')
    can_create_account = user_has_object_right(user, workspace, 'account', 'create')
    can_reuse_account = user_has_object_right(user, workspace, 'account', 'read')
    can_create_contact = user_has_object_right(user, workspace, 'contact', 'create')
    can_reuse_contact = user_has_object_right(user, workspace, 'contact', 'read')
    can_update_contact = user_has_object_right(user, workspace, 'contact', 'update')
    can_create_deal = user_has_object_right(user, workspace, 'deal', 'create')
    can_convert = bool(
        can_update_lead
        and can_create_deal
        and (can_create_account or can_reuse_account)
        and (can_create_contact or can_reuse_contact)
    )
    return {
        'conversion_available': True,
        'can_convert_lead': can_convert,
        'can_update_lead': can_update_lead,
        'can_create_account': can_create_account,
        'can_reuse_account': can_reuse_account,
        'can_create_contact': can_create_contact,
        'can_reuse_contact': can_reuse_contact,
        'can_update_contact': can_update_contact,
        'can_create_deal': can_create_deal,
    }


def _resolve_target_record_label(workspace, target_type: str, target_id: int, *, user=None) -> str:
    model_cls = CRM_MODELS_BY_KEY.get(target_type)
    if model_cls is None:
        return f'{target_type} #{target_id}'
    qs = model_cls.objects.filter(workspace=workspace, id=target_id)
    if user is not None:
        qs = qs.filter(build_record_visibility_q(user, workspace))
    row = qs.first()
    if row is None:
        return f'{RECORD_CODE_PREFIXES.get(target_type, target_type)}-{target_id}'
    return _get_visible_record_label(row, target_type, user=user, workspace=workspace)


def _is_enabled_and_readable(user, workspace, object_key: str) -> bool:
    if not WorkspaceTable.objects.filter(
        workspace=workspace,
        object_key=object_key,
        is_enabled=True,
    ).exists():
        return False
    return user_has_object_right(user, workspace, object_key, 'read')


def serialize_crm_record(
    record,
    object_key: str,
    *,
    include_detail: bool = False,
    user=None,
    workspace=None,
) -> dict:
    """Serialize any S2.1 CRM record for list or detail views, enforcing field rights and sharing."""
    ws = workspace or getattr(record, 'workspace', None)
    prefix = RECORD_CODE_PREFIXES[object_key]
    data = {
        'id': record.id,
        'object_key': object_key,
        'record_code': f'{prefix}-{record.id}',
        'display_label': _get_visible_record_label(record, object_key, user=user, workspace=ws),
        'visibility': record.visibility,
        'shared_role_id': record.shared_role_id,
        'shared_role': _serialize_role_ref(record.shared_role),
        'can_manage_sharing': (
            user_can_manage_record_sharing(user, ws, record)
            if user is not None and ws is not None
            else False
        ),
        'owner': _serialize_user_ref(record.owner),
        'created_by': _serialize_user_ref(record.created_by),
        'created_at': record.created_at.isoformat(),
        'updated_at': record.updated_at.isoformat(),
    }

    if object_key == 'account':
        source_lead_ref = _serialize_lead_ref(record.source_lead, user=user, workspace=ws)
        data.update(
            {
                'name': record.name,
                'city': record.city,
                'phone': record.phone,
                'email': record.email,
                'notes': record.notes,
                'contacts_count': getattr(record, 'contacts_count', record.contacts.count()),
                'source_lead_id': source_lead_ref['id'] if source_lead_ref else None,
                'lead_id': source_lead_ref['id'] if source_lead_ref else None,
                'source_lead': source_lead_ref,
                'lead': source_lead_ref,
            }
        )
    elif object_key == 'contact':
        source_lead_ref = _serialize_lead_ref(record.source_lead, user=user, workspace=ws)
        account_ref = _serialize_account_ref(record.account, user=user, workspace=ws)
        data.update(
            {
                'name': record.name,
                'title': record.title,
                'account_id': account_ref['id'] if account_ref else None,
                'account': account_ref,
                'email': record.email,
                'phone': record.phone,
                'sms_consent': bool(record.sms_consent),
                'notes': record.notes,
                'source_lead_id': source_lead_ref['id'] if source_lead_ref else None,
                'lead_id': source_lead_ref['id'] if source_lead_ref else None,
                'source_lead': source_lead_ref,
                'lead': source_lead_ref,
            }
        )
    elif object_key == 'lead':
        converted_account_ref = _serialize_account_ref(record.converted_account, user=user, workspace=ws)
        converted_contact_ref = _serialize_contact_ref(record.converted_contact, user=user, workspace=ws)
        converted_deal_ref = _serialize_deal_ref(record.converted_deal, user=user, workspace=ws)
        data.update(
            {
                'name': record.name,
                'company': record.company,
                'email': record.email,
                'phone': record.phone,
                'sms_consent': bool(record.sms_consent),
                'status': record.status,
                'converted': record.status == Lead.STATUS_CONVERTED,
                'converted_at': record.converted_at.isoformat() if record.converted_at else None,
                'converted_by': _serialize_user_ref(record.converted_by),
                'converted_account_id': converted_account_ref['id'] if converted_account_ref else None,
                'converted_contact_id': converted_contact_ref['id'] if converted_contact_ref else None,
                'converted_deal_id': converted_deal_ref['id'] if converted_deal_ref else None,
                'converted_account': converted_account_ref,
                'converted_contact': converted_contact_ref,
                'converted_deal': converted_deal_ref,
                'notes': record.notes,
            }
        )
    elif object_key == 'deal':
        source_lead_ref = _serialize_lead_ref(record.source_lead, user=user, workspace=ws)
        account_ref = _serialize_account_ref(record.account, user=user, workspace=ws)
        contact_ref = _serialize_contact_ref(record.contact, user=user, workspace=ws)
        data.update(
            {
                'title': record.title,
                'account_id': account_ref['id'] if account_ref else None,
                'account': account_ref,
                'contact_id': contact_ref['id'] if contact_ref else None,
                'contact': contact_ref,
                'amount': f'{record.amount:.2f}',
                'stage': record.stage,
                'stage_tone': _resolve_deal_stage_tone(record),
                'pipeline_id': record.pipeline_id,
                'pipeline_stage_id': record.pipeline_stage_id,
                'target_close_date': (
                    record.target_close_date.isoformat() if record.target_close_date else None
                ),
                'win_probability': record.win_probability,
                'estimated_margin': (
                    f'{record.estimated_margin:.2f}'
                    if record.estimated_margin is not None
                    else None
                ),
                'notes': record.notes,
                'source_lead_id': source_lead_ref['id'] if source_lead_ref else None,
                'lead_id': source_lead_ref['id'] if source_lead_ref else None,
                'source_lead': source_lead_ref,
                'lead': source_lead_ref,
            }
        )
    elif object_key == 'job':
        account_ref = _serialize_account_ref(record.account, user=user, workspace=ws)
        contact_ref = _serialize_contact_ref(record.contact, user=user, workspace=ws)
        data.update(
            {
                'title': record.title,
                'status': record.status,
                'assignee_id': record.assignee_id,
                'assignee': _serialize_user_ref(record.assignee),
                'due_date': record.due_date.isoformat() if record.due_date else None,
                'account_id': account_ref['id'] if account_ref else None,
                'account': account_ref,
                'contact_id': contact_ref['id'] if contact_ref else None,
                'contact': contact_ref,
                'notes': record.notes,
            }
        )
    elif object_key == 'appointment':
        iso_time = record.scheduled_at.isoformat()
        contact_ref = _serialize_contact_ref(record.contact, user=user, workspace=ws)
        data.update(
            {
                'title': record.title,
                'scheduled_at': iso_time,
                'time': iso_time,
                'status': record.status,
                'assignee_id': record.assignee_id,
                'assignee': _serialize_user_ref(record.assignee),
                'contact_id': contact_ref['id'] if contact_ref else None,
                'contact': contact_ref,
                'notes': record.notes,
            }
        )
    elif object_key == 'visit':
        iso_date = record.date.isoformat()
        contact_ref = _serialize_contact_ref(record.contact, user=user, workspace=ws)
        data.update(
            {
                'date': iso_date,
                'visited_at': iso_date,
                'reason': record.reason,
                'notes': record.notes,
                'follow_up': record.follow_up,
                'assignee_id': record.assignee_id,
                'assignee': _serialize_user_ref(record.assignee),
                'contact_id': contact_ref['id'] if contact_ref else None,
                'contact': contact_ref,
            }
        )
    elif object_key == 'ticket':
        account_ref = _serialize_account_ref(record.account, user=user, workspace=ws)
        contact_ref = _serialize_contact_ref(record.contact, user=user, workspace=ws)
        data.update(
            {
                'title': record.title,
                'status': record.status,
                'priority': record.priority,
                'account_id': account_ref['id'] if account_ref else None,
                'account': account_ref,
                'contact_id': contact_ref['id'] if contact_ref else None,
                'contact': contact_ref,
                'assignee_id': record.assignee_id,
                'assignee': _serialize_user_ref(record.assignee),
                'description': record.description,
            }
        )
    elif object_key == 'activity':
        data.update(
            {
                'activity_type': record.activity_type,
                'type': record.activity_type,
                'target_object_type': record.target_object_type,
                'target_object_id': record.target_object_id,
                'target_record_label': _resolve_target_record_label(
                    ws,
                    record.target_object_type,
                    record.target_object_id,
                    user=user,
                ),
                'summary': record.summary,
                'text': record.summary,
                'due_at': record.due_at.isoformat() if record.due_at else None,
                'is_completed': bool(record.is_completed),
                'assignee_id': record.assignee_id,
                'assignee': _serialize_user_ref(record.assignee),
            }
        )

    _strip_hidden_fields(data, object_key, user=user, workspace=ws)

    if include_detail and ws is not None and user is not None:
        if object_key != 'activity':
            can_read_activities = _is_enabled_and_readable(user, ws, 'activity')
            data['activities_readable'] = can_read_activities
            if can_read_activities:
                activities_qs = (
                    Activity.objects.filter(
                        workspace=ws,
                        target_object_type=object_key,
                        target_object_id=record.id,
                    )
                    .filter(build_record_visibility_q(user, ws))
                    .select_related('owner', 'created_by', 'assignee', 'shared_role')
                    .order_by('-created_at', '-id')
                )
                activities_total_count = activities_qs.count()
                activities = list(activities_qs[:100])
                data['activities'] = [
                    serialize_crm_record(act, 'activity', user=user, workspace=ws)
                    for act in activities
                ]
                data['activities_total_count'] = activities_total_count
                data['has_more_activities'] = activities_total_count > len(activities)
            else:
                data['activities'] = []
                data['activities_total_count'] = 0
                data['has_more_activities'] = False

        data['linked_records'] = _serialize_linked_records(record, object_key, user=user, workspace=ws)

    return data


def _attach_source_leads(linked: dict, record, *, user, workspace) -> None:
    if not _is_enabled_and_readable(user, workspace, 'lead'):
        return
    leads_by_id: dict[int, Lead] = {}
    if getattr(record, 'source_lead_id', None) and getattr(record, 'source_lead', None):
        if user_can_see_record(user, workspace, record.source_lead):
            leads_by_id[record.source_lead_id] = record.source_lead
    if hasattr(record, 'source_leads'):
        for row in (
            record.source_leads.filter(workspace=workspace)
            .filter(build_record_visibility_q(user, workspace))
            .order_by('-converted_at', '-id')[:25]
        ):
            leads_by_id.setdefault(row.id, row)
    if leads_by_id:
        serialized = [
            ref
            for row in leads_by_id.values()
            if (ref := _serialize_lead_ref(row, user=user, workspace=workspace)) is not None
        ]
        if serialized:
            linked['lead'] = serialized[0]
            linked['source_leads'] = serialized


def _serialize_linked_records(record, object_key: str, *, user, workspace) -> dict:
    linked: dict = {}
    vis_q = build_record_visibility_q(user, workspace)

    def _linked_contact_item(c: Contact) -> dict:
        item = {'id': c.id, 'name': _get_visible_record_label(c, 'contact', user=user, workspace=workspace)}
        if get_field_right(user, workspace, 'contact', 'title') != FIELD_RIGHT_HIDDEN:
            item['title'] = c.title
        if get_field_right(user, workspace, 'contact', 'email') != FIELD_RIGHT_HIDDEN:
            item['email'] = c.email
        return item

    def _linked_deal_item(d: Deal) -> dict:
        item = {'id': d.id, 'title': _get_visible_record_label(d, 'deal', user=user, workspace=workspace)}
        if get_field_right(user, workspace, 'deal', 'stage') != FIELD_RIGHT_HIDDEN:
            item['stage'] = d.stage
        if get_field_right(user, workspace, 'deal', 'amount') != FIELD_RIGHT_HIDDEN:
            item['amount'] = f'{d.amount:.2f}'
        return item

    def _linked_job_item(j: Job) -> dict:
        item = {'id': j.id, 'title': _get_visible_record_label(j, 'job', user=user, workspace=workspace)}
        if get_field_right(user, workspace, 'job', 'status') != FIELD_RIGHT_HIDDEN:
            item['status'] = j.status
        if get_field_right(user, workspace, 'job', 'due_date') != FIELD_RIGHT_HIDDEN:
            item['due_date'] = j.due_date.isoformat() if j.due_date else None
        return item

    def _linked_ticket_item(t: Ticket) -> dict:
        item = {'id': t.id, 'title': _get_visible_record_label(t, 'ticket', user=user, workspace=workspace)}
        if get_field_right(user, workspace, 'ticket', 'status') != FIELD_RIGHT_HIDDEN:
            item['status'] = t.status
        if get_field_right(user, workspace, 'ticket', 'priority') != FIELD_RIGHT_HIDDEN:
            item['priority'] = t.priority
        return item

    def _linked_appointment_item(a: Appointment) -> dict:
        item = {'id': a.id, 'title': _get_visible_record_label(a, 'appointment', user=user, workspace=workspace)}
        if get_field_right(user, workspace, 'appointment', 'status') != FIELD_RIGHT_HIDDEN:
            item['status'] = a.status
        if get_field_right(user, workspace, 'appointment', 'scheduled_at') != FIELD_RIGHT_HIDDEN:
            item['scheduled_at'] = a.scheduled_at.isoformat()
        return item

    def _linked_visit_item(v: Visit) -> dict:
        item = {'id': v.id, 'reason': _get_visible_record_label(v, 'visit', user=user, workspace=workspace)}
        if get_field_right(user, workspace, 'visit', 'date') != FIELD_RIGHT_HIDDEN:
            item['date'] = v.date.isoformat()
        if get_field_right(user, workspace, 'visit', 'follow_up') != FIELD_RIGHT_HIDDEN:
            item['follow_up'] = v.follow_up
        return item

    if object_key == 'account':
        _attach_source_leads(linked, record, user=user, workspace=workspace)
        if _is_enabled_and_readable(user, workspace, 'contact'):
            linked['contacts'] = [
                _linked_contact_item(c)
                for c in record.contacts.filter(workspace=workspace).filter(vis_q).order_by('name', 'id')[:50]
            ]
        if _is_enabled_and_readable(user, workspace, 'deal'):
            linked['deals'] = [
                _linked_deal_item(d)
                for d in record.deals.filter(workspace=workspace).filter(vis_q).order_by('-updated_at', '-id')[:50]
            ]
        if _is_enabled_and_readable(user, workspace, 'job'):
            linked['jobs'] = [
                _linked_job_item(j)
                for j in record.jobs.filter(workspace=workspace).filter(vis_q).order_by('-updated_at', '-id')[:50]
            ]
        if _is_enabled_and_readable(user, workspace, 'ticket'):
            linked['tickets'] = [
                _linked_ticket_item(t)
                for t in record.tickets.filter(workspace=workspace).filter(vis_q).order_by('-updated_at', '-id')[:50]
            ]

    elif object_key == 'contact':
        _attach_source_leads(linked, record, user=user, workspace=workspace)
        if record.account_id and _is_enabled_and_readable(user, workspace, 'account'):
            acc_ref = _serialize_account_ref(record.account, user=user, workspace=workspace)
            if acc_ref is not None:
                linked['account'] = acc_ref
        if _is_enabled_and_readable(user, workspace, 'deal'):
            linked['deals'] = [
                _linked_deal_item(d)
                for d in record.deals.filter(workspace=workspace).filter(vis_q).order_by('-updated_at', '-id')[:50]
            ]
        if _is_enabled_and_readable(user, workspace, 'job'):
            linked['jobs'] = [
                _linked_job_item(j)
                for j in record.jobs.filter(workspace=workspace).filter(vis_q).order_by('-updated_at', '-id')[:50]
            ]
        if _is_enabled_and_readable(user, workspace, 'appointment'):
            linked['appointments'] = [
                _linked_appointment_item(a)
                for a in record.appointments.filter(workspace=workspace).filter(vis_q).order_by('scheduled_at', '-id')[:50]
            ]
        if _is_enabled_and_readable(user, workspace, 'visit'):
            linked['visits'] = [
                _linked_visit_item(v)
                for v in record.visits.filter(workspace=workspace).filter(vis_q).order_by('-date', '-id')[:50]
            ]
        if _is_enabled_and_readable(user, workspace, 'ticket'):
            linked['tickets'] = [
                _linked_ticket_item(t)
                for t in record.tickets.filter(workspace=workspace).filter(vis_q).order_by('-updated_at', '-id')[:50]
            ]

    elif object_key == 'lead':
        if record.converted_account_id and _is_enabled_and_readable(user, workspace, 'account'):
            acc_ref = _serialize_account_ref(record.converted_account, user=user, workspace=workspace)
            if acc_ref is not None:
                linked['account'] = acc_ref
        if record.converted_contact_id and _is_enabled_and_readable(user, workspace, 'contact'):
            con_ref = _serialize_contact_ref(record.converted_contact, user=user, workspace=workspace)
            if con_ref is not None:
                linked['contact'] = con_ref
        if record.converted_deal_id and _is_enabled_and_readable(user, workspace, 'deal'):
            deal_ref = _serialize_deal_ref(record.converted_deal, user=user, workspace=workspace)
            if deal_ref is not None:
                linked['deal'] = deal_ref
                linked['deals'] = [deal_ref]

    elif object_key in ('deal', 'job', 'ticket'):
        if object_key == 'deal':
            _attach_source_leads(linked, record, user=user, workspace=workspace)
        if record.account_id and _is_enabled_and_readable(user, workspace, 'account'):
            acc_ref = _serialize_account_ref(record.account, user=user, workspace=workspace)
            if acc_ref is not None:
                linked['account'] = acc_ref
        if record.contact_id and _is_enabled_and_readable(user, workspace, 'contact'):
            con_ref = _serialize_contact_ref(record.contact, user=user, workspace=workspace)
            if con_ref is not None:
                linked['contact'] = con_ref

    elif object_key in ('appointment', 'visit'):
        if record.contact_id and _is_enabled_and_readable(user, workspace, 'contact'):
            con_ref = _serialize_contact_ref(record.contact, user=user, workspace=workspace)
            if con_ref is not None:
                linked['contact'] = con_ref

    return linked


def _is_field_visible(user, workspace, object_key: str, field_name: str) -> bool:
    if user is None or workspace is None:
        return True
    return get_field_right(user, workspace, object_key, field_name) != FIELD_RIGHT_HIDDEN


def _apply_visibility_query_filters(qs, query_params):
    vis_filter = (query_params.get('visibility') or '').strip().lower()
    if vis_filter in VALID_VISIBILITY_VALUES:
        qs = qs.filter(visibility=vis_filter)
    raw_shared_role = query_params.get('shared_role_id')
    if raw_shared_role:
        role_id = _parse_fk_id(raw_shared_role, field_name='Shared role')
        qs = qs.filter(visibility=WorkspaceRecordBase.VISIBILITY_ROLE, shared_role_id=role_id)
    return qs


def _apply_deal_filters(qs, query_params, *, include_stage: bool = True, user=None, workspace=None):
    qs = _apply_visibility_query_filters(qs, query_params)
    q = (query_params.get('q') or '').strip()
    if q:
        search_q = Q()
        if _is_field_visible(user, workspace, 'deal', 'title'):
            search_q |= Q(title__icontains=q)
        if _is_field_visible(user, workspace, 'deal', 'account') and _is_field_visible(user, workspace, 'account', 'name'):
            search_q |= Q(account__name__icontains=q)
        if _is_field_visible(user, workspace, 'deal', 'contact') and _is_field_visible(user, workspace, 'contact', 'name'):
            search_q |= Q(contact__name__icontains=q)
        qs = qs.filter(search_q) if search_q else qs.none()
    if include_stage and _is_field_visible(user, workspace, 'deal', 'stage'):
        stage_filter = (query_params.get('stage') or '').strip()
        if stage_filter:
            qs = qs.filter(stage__iexact=stage_filter)
    if query_params.get('account_id') and _is_field_visible(user, workspace, 'deal', 'account'):
        qs = qs.filter(account_id=_parse_fk_id(query_params.get('account_id'), field_name='Account'))
    if query_params.get('contact_id') and _is_field_visible(user, workspace, 'deal', 'contact'):
        qs = qs.filter(contact_id=_parse_fk_id(query_params.get('contact_id'), field_name='Contact'))
    return qs


def _build_deal_stage_summary(workspace, qs, *, user=None) -> dict:
    pipeline = ensure_workspace_pipeline(workspace, require_enabled=False)
    stages = (
        list(PipelineStage.objects.filter(pipeline=pipeline).order_by('position', 'id'))
        if pipeline is not None
        else []
    )
    amount_visible = _is_field_visible(user, workspace, 'deal', 'amount')
    stage_visible = _is_field_visible(user, workspace, 'deal', 'stage')
    total_amount = (
        (qs.aggregate(total=Sum('amount'))['total'] or Decimal('0.00'))
        if amount_visible
        else None
    )

    counts_by_id: dict[int, int] = {stage.id: 0 for stage in stages}
    amounts_by_id: dict[int, Decimal] = {stage.id: Decimal('0.00') for stage in stages}
    stages_by_lower = {stage.name.lower(): stage for stage in stages}

    if stage_visible:
        for row in qs.values('pipeline_stage_id', 'stage').annotate(cnt=Count('id'), val=Sum('amount')):
            stage_id = row['pipeline_stage_id']
            if stage_id not in counts_by_id:
                matched = stages_by_lower.get((row['stage'] or '').strip().lower())
                stage_id = matched.id if matched else None
            if stage_id is not None:
                counts_by_id[stage_id] += int(row['cnt'] or 0)
                if amount_visible:
                    amounts_by_id[stage_id] += row['val'] or Decimal('0.00')

    stage_counts = {stage.name: counts_by_id[stage.id] for stage in stages} if stage_visible else {}
    stage_amounts = (
        {stage.name: f'{amounts_by_id[stage.id]:.2f}' for stage in stages}
        if stage_visible and amount_visible
        else {}
    )
    stage_tones = {stage.name: stage.tone for stage in stages}
    return {
        'pipeline': (
            {'id': pipeline.id, 'name': pipeline.name}
            if pipeline is not None
            else None
        ),
        'total_amount': f'{total_amount:.2f}' if total_amount is not None else None,
        'stages': [serialize_pipeline_stage(stage) for stage in stages],
        'stage_counts': stage_counts,
        'stage_amounts': stage_amounts,
        'stage_tones': stage_tones,
    }


def _build_base_queryset(workspace, object_key: str, *, user=None):
    model_cls = CRM_MODELS_BY_KEY[object_key]
    qs = model_cls.objects.filter(workspace=workspace).select_related(
        'owner', 'created_by', 'shared_role'
    )
    if user is not None:
        qs = qs.filter(build_record_visibility_q(user, workspace))
    if object_key == 'account':
        contact_count_filter = (
            build_record_visibility_q(user, workspace, prefix='contacts__')
            if user is not None
            else Q()
        )
        qs = qs.select_related('source_lead').annotate(
            contacts_count=Count('contacts', filter=contact_count_filter, distinct=True)
        )
    elif object_key == 'contact':
        qs = qs.select_related('account', 'source_lead')
    elif object_key == 'lead':
        qs = qs.select_related(
            'converted_by',
            'converted_account',
            'converted_contact',
            'converted_deal',
        )
    elif object_key == 'deal':
        qs = qs.select_related('account', 'contact', 'source_lead', 'pipeline', 'pipeline_stage')
    elif object_key == 'job':
        qs = qs.select_related('account', 'contact', 'assignee')
    elif object_key == 'appointment':
        qs = qs.select_related('contact', 'assignee')
    elif object_key == 'visit':
        qs = qs.select_related('contact', 'assignee')
    elif object_key == 'ticket':
        qs = qs.select_related('account', 'contact', 'assignee')
    elif object_key == 'activity':
        qs = qs.select_related('assignee')
    return qs


def list_crm_records(
    workspace,
    user,
    object_key: str,
    *,
    query_params,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict:
    """List paginated, filtered, and searched records for an enabled CRM object."""
    ensure_object_permission(user, workspace, object_key, 'read')

    qs = _build_base_queryset(workspace, object_key, user=user)
    if object_key != 'deal':
        qs = _apply_visibility_query_filters(qs, query_params)
    q = (query_params.get('q') or '').strip()

    if object_key == 'account':
        if q:
            search_q = Q()
            for f in ('name', 'city', 'email', 'phone'):
                if _is_field_visible(user, workspace, 'account', f):
                    search_q |= Q(**{f'{f}__icontains': q})
            qs = qs.filter(search_q) if search_q else qs.none()
        city = (query_params.get('city') or '').strip()
        if city and _is_field_visible(user, workspace, 'account', 'city'):
            qs = qs.filter(city__iexact=city)

    elif object_key == 'contact':
        if q:
            search_q = Q()
            for f in ('name', 'title', 'email', 'phone'):
                if _is_field_visible(user, workspace, 'contact', f):
                    search_q |= Q(**{f'{f}__icontains': q})
            if _is_field_visible(user, workspace, 'contact', 'account') and _is_field_visible(user, workspace, 'account', 'name'):
                search_q |= Q(account__name__icontains=q)
            qs = qs.filter(search_q) if search_q else qs.none()
        raw_acc = query_params.get('account_id')
        if raw_acc and _is_field_visible(user, workspace, 'contact', 'account'):
            acc_id = _parse_fk_id(raw_acc, field_name='Account')
            qs = qs.filter(account_id=acc_id)
        raw_compat_acc = query_params.get('compatible_account_id')
        if raw_compat_acc and _is_field_visible(user, workspace, 'contact', 'account'):
            compat_acc_id = _parse_fk_id(raw_compat_acc, field_name='Account')
            qs = qs.filter(Q(account_id=compat_acc_id) | Q(account_id__isnull=True))
        raw_consent = (query_params.get('sms_consent') or '').strip().lower()
        if _is_field_visible(user, workspace, 'contact', 'sms_consent'):
            if raw_consent in ('on', 'true', '1'):
                qs = qs.filter(sms_consent=True)
            elif raw_consent in ('off', 'false', '0'):
                qs = qs.filter(sms_consent=False)

    elif object_key == 'lead':
        if q:
            search_q = Q()
            for f in ('name', 'company', 'email', 'phone'):
                if _is_field_visible(user, workspace, 'lead', f):
                    search_q |= Q(**{f'{f}__icontains': q})
            qs = qs.filter(search_q) if search_q else qs.none()
        status_filter = (query_params.get('status') or '').strip().lower()
        if status_filter in (Lead.STATUS_OPEN, Lead.STATUS_CONVERTED) and _is_field_visible(user, workspace, 'lead', 'status'):
            qs = qs.filter(status=status_filter)

    elif object_key == 'deal':
        qs = _apply_deal_filters(qs, query_params, include_stage=True, user=user, workspace=workspace)

    elif object_key == 'job':
        if q:
            search_q = Q()
            if _is_field_visible(user, workspace, 'job', 'title'):
                search_q |= Q(title__icontains=q)
            if _is_field_visible(user, workspace, 'job', 'account') and _is_field_visible(user, workspace, 'account', 'name'):
                search_q |= Q(account__name__icontains=q)
            if _is_field_visible(user, workspace, 'job', 'contact') and _is_field_visible(user, workspace, 'contact', 'name'):
                search_q |= Q(contact__name__icontains=q)
            if _is_field_visible(user, workspace, 'job', 'assignee'):
                search_q |= Q(assignee__email__icontains=q)
            qs = qs.filter(search_q) if search_q else qs.none()
        status_filter = (query_params.get('status') or '').strip()
        if status_filter and _is_field_visible(user, workspace, 'job', 'status'):
            qs = qs.filter(status__iexact=status_filter)
        if query_params.get('assignee_id') and _is_field_visible(user, workspace, 'job', 'assignee'):
            qs = qs.filter(assignee_id=_parse_fk_id(query_params.get('assignee_id'), field_name='Assignee'))
        if query_params.get('account_id') and _is_field_visible(user, workspace, 'job', 'account'):
            qs = qs.filter(account_id=_parse_fk_id(query_params.get('account_id'), field_name='Account'))
        if query_params.get('contact_id') and _is_field_visible(user, workspace, 'job', 'contact'):
            qs = qs.filter(contact_id=_parse_fk_id(query_params.get('contact_id'), field_name='Contact'))

    elif object_key == 'appointment':
        if q:
            search_q = Q()
            if _is_field_visible(user, workspace, 'appointment', 'title'):
                search_q |= Q(title__icontains=q)
            if _is_field_visible(user, workspace, 'appointment', 'contact') and _is_field_visible(user, workspace, 'contact', 'name'):
                search_q |= Q(contact__name__icontains=q)
            if _is_field_visible(user, workspace, 'appointment', 'assignee'):
                search_q |= Q(assignee__email__icontains=q)
            qs = qs.filter(search_q) if search_q else qs.none()
        status_filter = (query_params.get('status') or '').strip()
        if status_filter and _is_field_visible(user, workspace, 'appointment', 'status'):
            qs = qs.filter(status__iexact=status_filter)
        if query_params.get('assignee_id') and _is_field_visible(user, workspace, 'appointment', 'assignee'):
            qs = qs.filter(assignee_id=_parse_fk_id(query_params.get('assignee_id'), field_name='Assignee'))
        if query_params.get('contact_id') and _is_field_visible(user, workspace, 'appointment', 'contact'):
            qs = qs.filter(contact_id=_parse_fk_id(query_params.get('contact_id'), field_name='Contact'))

    elif object_key == 'visit':
        if q:
            search_q = Q()
            if _is_field_visible(user, workspace, 'visit', 'reason'):
                search_q |= Q(reason__icontains=q)
            if _is_field_visible(user, workspace, 'visit', 'notes'):
                search_q |= Q(notes__icontains=q)
            if _is_field_visible(user, workspace, 'visit', 'follow_up'):
                search_q |= Q(follow_up__icontains=q)
            if _is_field_visible(user, workspace, 'visit', 'contact') and _is_field_visible(user, workspace, 'contact', 'name'):
                search_q |= Q(contact__name__icontains=q)
            if _is_field_visible(user, workspace, 'visit', 'assignee'):
                search_q |= Q(assignee__email__icontains=q)
            qs = qs.filter(search_q) if search_q else qs.none()
        if query_params.get('contact_id') and _is_field_visible(user, workspace, 'visit', 'contact'):
            qs = qs.filter(contact_id=_parse_fk_id(query_params.get('contact_id'), field_name=_get_contact_screen_label(workspace)))
        if query_params.get('assignee_id') and _is_field_visible(user, workspace, 'visit', 'assignee'):
            qs = qs.filter(assignee_id=_parse_fk_id(query_params.get('assignee_id'), field_name='Assignee'))
        raw_follow_up = (query_params.get('has_follow_up') or '').strip().lower()
        if raw_follow_up and _is_field_visible(user, workspace, 'visit', 'follow_up'):
            if raw_follow_up in ('yes', 'true', '1', 'on'):
                qs = qs.exclude(follow_up='')
            elif raw_follow_up in ('no', 'false', '0', 'off'):
                qs = qs.filter(follow_up='')

    elif object_key == 'ticket':
        if q:
            search_q = Q()
            if _is_field_visible(user, workspace, 'ticket', 'title'):
                search_q |= Q(title__icontains=q)
            if _is_field_visible(user, workspace, 'ticket', 'account') and _is_field_visible(user, workspace, 'account', 'name'):
                search_q |= Q(account__name__icontains=q)
            if _is_field_visible(user, workspace, 'ticket', 'contact') and _is_field_visible(user, workspace, 'contact', 'name'):
                search_q |= Q(contact__name__icontains=q)
            if _is_field_visible(user, workspace, 'ticket', 'description'):
                search_q |= Q(description__icontains=q)
            qs = qs.filter(search_q) if search_q else qs.none()
        status_filter = (query_params.get('status') or '').strip()
        if status_filter and _is_field_visible(user, workspace, 'ticket', 'status'):
            qs = qs.filter(status__iexact=status_filter)
        priority_filter = (query_params.get('priority') or '').strip()
        if priority_filter and _is_field_visible(user, workspace, 'ticket', 'priority'):
            qs = qs.filter(priority__iexact=priority_filter)
        if query_params.get('assignee_id') and _is_field_visible(user, workspace, 'ticket', 'assignee'):
            qs = qs.filter(assignee_id=_parse_fk_id(query_params.get('assignee_id'), field_name='Assignee'))
        if query_params.get('account_id') and _is_field_visible(user, workspace, 'ticket', 'account'):
            qs = qs.filter(account_id=_parse_fk_id(query_params.get('account_id'), field_name='Account'))
        if query_params.get('contact_id') and _is_field_visible(user, workspace, 'ticket', 'contact'):
            qs = qs.filter(contact_id=_parse_fk_id(query_params.get('contact_id'), field_name='Contact'))

    elif object_key == 'activity':
        if q:
            if _is_field_visible(user, workspace, 'activity', 'summary'):
                qs = qs.filter(Q(summary__icontains=q))
            else:
                qs = qs.none()
        act_type = (query_params.get('activity_type') or '').strip().lower()
        if act_type and _is_field_visible(user, workspace, 'activity', 'activity_type'):
            qs = qs.filter(activity_type=act_type)
        target_type = (query_params.get('target_object_type') or '').strip().lower()
        if target_type:
            canonical_target = OBJECT_KEY_ALIASES.get(target_type, target_type)
            qs = qs.filter(target_object_type=canonical_target)
        if query_params.get('target_object_id'):
            qs = qs.filter(
                target_object_id=_parse_fk_id(
                    query_params.get('target_object_id'),
                    field_name='target_object_id',
                )
            )
        if query_params.get('assignee_id') and _is_field_visible(user, workspace, 'activity', 'assignee'):
            qs = qs.filter(assignee_id=_parse_fk_id(query_params.get('assignee_id'), field_name='Assignee'))

    summary = _compute_list_summary(workspace, object_key, qs, user=user)
    total_count = qs.count()
    total_pages = max(1, math.ceil(total_count / page_size)) if total_count else 1
    offset = (page - 1) * page_size
    rows = list(qs[offset : offset + page_size])

    permissions_payload = {
        'can_create': user_has_object_right(user, workspace, object_key, 'create'),
        'can_read': True,
        'can_update': user_has_object_right(user, workspace, object_key, 'update'),
        'can_delete': user_has_object_right(user, workspace, object_key, 'delete'),
        'field_rights': get_user_field_rights(user, workspace, object_key),
    }
    if object_key in ('lead', 'deal'):
        cap = get_lead_conversion_capability(user, workspace)
        permissions_payload['conversion_available'] = cap['conversion_available']
        permissions_payload['can_convert_lead'] = cap['can_convert_lead']

    return {
        'results': [
            serialize_crm_record(row, object_key, user=user, workspace=workspace)
            for row in rows
        ],
        'total_count': total_count,
        'page': page,
        'page_size': page_size,
        'total_pages': total_pages,
        'has_next': page < total_pages,
        'has_previous': page > 1,
        'summary': summary,
        'permissions': permissions_payload,
    }


def _compute_list_summary(workspace, object_key: str, qs, *, user=None) -> dict:
    if object_key == 'account':
        if not _is_field_visible(user, workspace, 'account', 'city'):
            return {'cities': [], 'city_counts': []}
        city_base = Account.objects.filter(workspace=workspace)
        if user is not None:
            city_base = city_base.filter(build_record_visibility_q(user, workspace))
        cities = list(
            city_base
            .exclude(city='')
            .values_list('city', flat=True)
            .distinct()
            .order_by('city')
        )
        city_counts = [
            {'city': row['city'] or 'No city', 'count': row['cnt']}
            for row in qs.values('city').annotate(cnt=Count('id')).order_by('-cnt', 'city')
        ]
        return {'cities': cities, 'city_counts': city_counts}
    if object_key == 'contact':
        if not _is_field_visible(user, workspace, 'contact', 'sms_consent'):
            return {}
        return {
            'sms_consented_count': qs.filter(sms_consent=True).count(),
        }
    if object_key == 'lead':
        if not _is_field_visible(user, workspace, 'lead', 'status'):
            return {}
        return {
            'open_count': qs.filter(status=Lead.STATUS_OPEN).count(),
            'converted_count': qs.filter(status=Lead.STATUS_CONVERTED).count(),
        }
    if object_key == 'deal':
        return _build_deal_stage_summary(workspace, qs, user=user)
    if object_key == 'job':
        if not _is_field_visible(user, workspace, 'job', 'status'):
            return {}
        return {
            'open_count': qs.filter(status__in=('Pending', 'In Progress')).count(),
            'completed_count': qs.filter(status='Completed').count(),
        }
    if object_key == 'appointment':
        if not _is_field_visible(user, workspace, 'appointment', 'status'):
            return {}
        return {
            'upcoming_count': qs.filter(status__in=('Scheduled', 'Confirmed')).count(),
            'completed_count': qs.filter(status='Completed').count(),
        }
    if object_key == 'visit':
        if not _is_field_visible(user, workspace, 'visit', 'follow_up'):
            return {}
        return {
            'with_follow_up_count': qs.exclude(follow_up='').count(),
        }
    if object_key == 'ticket':
        result = {}
        if _is_field_visible(user, workspace, 'ticket', 'status'):
            result['open_count'] = qs.filter(status__in=('New', 'Open', 'Pending')).count()
            result['resolved_count'] = qs.filter(status__in=('Resolved', 'Closed')).count()
        if _is_field_visible(user, workspace, 'ticket', 'priority'):
            priority_counts = {prio: 0 for prio in Ticket.DEFAULT_PRIORITIES}
            for row in qs.values('priority').annotate(cnt=Count('id')):
                priority_counts[row['priority']] = row['cnt']
            result['priority_counts'] = priority_counts
        return result
    return {}


def get_crm_record_detail(workspace, user, object_key: str, record_id: int) -> dict:
    """Fetch a single CRM record with linked summaries and activities."""
    ensure_object_permission(user, workspace, object_key, 'read')
    record = _build_base_queryset(workspace, object_key, user=user).filter(id=record_id).first()
    if record is None:
        raise CrmDomainError(f'{OBJECTS_BY_KEY[object_key].label} not found.', status_code=404)
    permissions_payload = {
        'can_create': user_has_object_right(user, workspace, object_key, 'create'),
        'can_read': True,
        'can_update': user_has_object_right(user, workspace, object_key, 'update'),
        'can_delete': user_has_object_right(user, workspace, object_key, 'delete'),
        'can_manage_sharing': user_can_manage_record_sharing(user, workspace, record),
        'field_rights': get_user_field_rights(user, workspace, object_key),
        'activity': {
            'can_create': user_has_object_right(user, workspace, 'activity', 'create'),
            'can_read': user_has_object_right(user, workspace, 'activity', 'read'),
            'can_update': user_has_object_right(user, workspace, 'activity', 'update'),
            'can_delete': user_has_object_right(user, workspace, 'activity', 'delete'),
            'field_rights': get_user_field_rights(user, workspace, 'activity'),
        },
    }
    if object_key in ('lead', 'deal'):
        cap = get_lead_conversion_capability(user, workspace)
        permissions_payload['conversion_available'] = cap['conversion_available']
        permissions_payload['can_convert_lead'] = cap['can_convert_lead']

    return {
        'record': serialize_crm_record(
            record,
            object_key,
            include_detail=True,
            user=user,
            workspace=workspace,
        ),
        'permissions': permissions_payload,
    }


def create_crm_record(workspace, user, object_key: str, payload: dict) -> dict:
    """Validate and create a new S2.1 CRM record with audit logging."""
    ensure_object_permission(user, workspace, object_key, 'create')
    _ensure_writable_fields(user, workspace, object_key, payload, is_create=True)
    user_tz = getattr(user, 'timezone', 'Africa/Accra') or 'Africa/Accra'
    visibility, shared_role, _ = _resolve_sharing_fields(
        workspace,
        payload,
        existing=None,
        user=user,
    )

    with transaction.atomic():
        if object_key == 'account':
            name = _parse_required_str(payload, 'name', label='Company name', max_length=255)
            record = Account.objects.create(
                workspace=workspace,
                owner=user,
                created_by=user,
                visibility=visibility,
                shared_role=shared_role,
                name=name,
                city=_parse_optional_str(payload, 'city', label='City', max_length=120),
                phone=_parse_optional_str(payload, 'phone', label='Phone', max_length=64),
                email=_parse_optional_email(payload, 'email'),
                notes=_parse_optional_str(payload, 'notes', label='Notes', preserve_newlines=True),
            )

        elif object_key == 'contact':
            name = _parse_required_str(
                payload,
                'name',
                label=_get_contact_screen_label(workspace) + ' name',
                max_length=255,
            )
            account, _ = _resolve_account(workspace, payload, user=user)
            record = Contact.objects.create(
                workspace=workspace,
                owner=user,
                created_by=user,
                visibility=visibility,
                shared_role=shared_role,
                account=account,
                name=name,
                title=_parse_optional_str(payload, 'title', label='Title', max_length=150),
                email=_parse_optional_email(payload, 'email'),
                phone=_parse_optional_str(payload, 'phone', label='Phone', max_length=64),
                sms_consent=_parse_bool(
                    payload.get('sms_consent'),
                    field_name='SMS consent',
                    default=False,
                ),
                notes=_parse_optional_str(payload, 'notes', label='Notes', preserve_newlines=True),
            )

        elif object_key == 'lead':
            name = _parse_required_str(payload, 'name', label='Lead name', max_length=255)
            raw_status = payload.get('status')
            if raw_status is not None and str(raw_status).strip().lower() != Lead.STATUS_OPEN:
                raise CrmDomainError(
                    'New leads start as open. Use lead conversion to convert a lead.',
                    status_code=400,
                )
            record = Lead.objects.create(
                workspace=workspace,
                owner=user,
                created_by=user,
                visibility=visibility,
                shared_role=shared_role,
                name=name,
                company=_parse_optional_str(payload, 'company', label='Company', max_length=255),
                email=_parse_optional_email(payload, 'email'),
                phone=_parse_optional_str(payload, 'phone', label='Phone', max_length=64),
                sms_consent=_parse_bool(
                    payload.get('sms_consent'),
                    field_name='SMS consent',
                    default=False,
                ),
                status=Lead.STATUS_OPEN,
                notes=_parse_optional_str(payload, 'notes', label='Notes', preserve_newlines=True),
            )

        elif object_key == 'deal':
            title = _parse_required_str(payload, 'title', 'name', label='Deal title', max_length=255)
            account, contact = _resolve_account_and_contact_pair(workspace, payload, user=user)
            stage_obj = resolve_deal_stage(
                workspace,
                payload.get('stage'),
                stage_id=_first_present(payload, 'stage_id', 'pipeline_stage_id'),
                allow_default=True,
            )
            amount = _parse_decimal(
                payload.get('amount'),
                field_name='Amount',
                allow_null=False,
                default=Decimal('0.00'),
            )
            record = Deal.objects.create(
                workspace=workspace,
                owner=user,
                created_by=user,
                visibility=visibility,
                shared_role=shared_role,
                pipeline=stage_obj.pipeline,
                pipeline_stage=stage_obj,
                title=title,
                account=account,
                contact=contact,
                amount=amount,
                stage=stage_obj.name,
                target_close_date=_parse_optional_date(
                    payload.get('target_close_date'),
                    field_name='Target close date',
                ),
                win_probability=_parse_int_range(
                    payload.get('win_probability'),
                    field_name='Win probability',
                    min_val=0,
                    max_val=100,
                    allow_null=True,
                ),
                estimated_margin=_parse_decimal(
                    payload.get('estimated_margin'),
                    field_name='Estimated margin',
                    allow_null=True,
                ),
                notes=_parse_optional_str(payload, 'notes', label='Notes', preserve_newlines=True),
            )

        elif object_key == 'job':
            title = _parse_required_str(payload, 'title', 'name', label='Job title', max_length=255)
            account, contact = _resolve_account_and_contact_pair(workspace, payload, user=user)
            if account is None and contact is None:
                contact_label = _get_contact_screen_label(workspace)
                raise CrmDomainError(
                    f'A Job must link to an Account or a {contact_label}.',
                    status_code=400,
                )
            status = _validate_choice(
                payload.get('status', 'Pending'),
                Job.DEFAULT_STATUSES,
                field_name='Status',
            )
            assignee = _resolve_assignee(workspace, payload)
            record = Job.objects.create(
                workspace=workspace,
                owner=user,
                created_by=user,
                visibility=visibility,
                shared_role=shared_role,
                title=title,
                status=status,
                assignee=assignee,
                due_date=_parse_optional_date(payload.get('due_date'), field_name='Due date'),
                account=account,
                contact=contact,
                notes=_parse_optional_str(payload, 'notes', label='Notes', preserve_newlines=True),
            )

        elif object_key == 'appointment':
            contact_label = _get_contact_screen_label(workspace)
            contact, _ = _resolve_contact(workspace, payload, user=user)
            if contact is None:
                raise CrmDomainError(
                    f'An Appointment must link to a {contact_label}.',
                    status_code=400,
                )
            raw_title = _first_present(payload, 'title', 'name')
            if isinstance(raw_title, str) and raw_title.strip():
                title = _parse_required_str(payload, 'title', 'name', label='Title', max_length=255)
            elif raw_title is not None and not isinstance(raw_title, str):
                raise CrmDomainError('Title must be text.', status_code=400)
            else:
                title = f'Appointment with {contact.name}'
            scheduled_at = _parse_datetime_value(
                _first_present(payload, 'scheduled_at', 'time'),
                field_name='Appointment time',
                user_timezone=user_tz,
                required=True,
            )
            status = _validate_choice(
                payload.get('status', 'Scheduled'),
                Appointment.DEFAULT_STATUSES,
                field_name='Status',
            )
            assignee = _resolve_assignee(workspace, payload)
            record = Appointment.objects.create(
                workspace=workspace,
                owner=user,
                created_by=user,
                visibility=visibility,
                shared_role=shared_role,
                title=title,
                scheduled_at=scheduled_at,
                status=status,
                assignee=assignee,
                contact=contact,
                notes=_parse_optional_str(payload, 'notes', label='Notes', preserve_newlines=True),
            )

        elif object_key == 'visit':
            _reject_forbidden_visit_fields(payload)
            contact_label = _get_contact_screen_label(workspace)
            contact, _ = _resolve_contact(workspace, payload, user=user)
            if contact is None:
                raise CrmDomainError(
                    f'A Visit must link to a {contact_label}.',
                    status_code=400,
                )
            visited_at = _parse_datetime_value(
                _first_present(payload, 'date', 'visited_at'),
                field_name='Visit date',
                user_timezone=user_tz,
                required=True,
            )
            reason = _parse_required_str(payload, 'reason', label='Reason', max_length=255)
            assignee = _resolve_assignee(workspace, payload)
            record = Visit.objects.create(
                workspace=workspace,
                owner=user,
                created_by=user,
                visibility=visibility,
                shared_role=shared_role,
                contact=contact,
                date=visited_at,
                reason=reason,
                notes=_parse_optional_str(payload, 'notes', label='Notes', preserve_newlines=True),
                follow_up=_parse_optional_str(
                    payload,
                    'follow_up',
                    label='Follow-up',
                    preserve_newlines=True,
                ),
                assignee=assignee,
            )

        elif object_key == 'ticket':
            title = _parse_required_str(
                payload,
                'title',
                'subject',
                label='Ticket title',
                max_length=255,
            )
            account, contact = _resolve_account_and_contact_pair(workspace, payload, user=user)
            status = _validate_choice(
                payload.get('status', 'New'),
                Ticket.DEFAULT_STATUSES,
                field_name='Status',
            )
            priority = _validate_choice(
                payload.get('priority', 'Normal'),
                Ticket.DEFAULT_PRIORITIES,
                field_name='Priority',
            )
            assignee = _resolve_assignee(workspace, payload)
            record = Ticket.objects.create(
                workspace=workspace,
                owner=user,
                created_by=user,
                visibility=visibility,
                shared_role=shared_role,
                title=title,
                status=status,
                priority=priority,
                account=account,
                contact=contact,
                assignee=assignee,
                description=_parse_optional_str(
                    payload,
                    'description',
                    'notes',
                    label='Description',
                    preserve_newlines=True,
                ),
            )

        elif object_key == 'activity':
            raw_type = _first_present(payload, 'activity_type', 'type')
            if not isinstance(raw_type, str) or not raw_type.strip():
                raise CrmDomainError('Activity type is required.', status_code=400)
            act_type = raw_type.strip().lower()
            if act_type not in Activity.VALID_SPRINT2_TYPES:
                raise CrmDomainError(
                    'Activity type must be note, task, call, or meeting.',
                    status_code=400,
                )
            summary = _parse_required_str(
                payload,
                'summary',
                'text',
                'body',
                'notes',
                label='Summary',
                max_length=4000,
            )
            target_type, target_id, _target_record = _resolve_activity_target(
                workspace,
                user,
                _first_present(payload, 'target_object_type', 'record_type', 'object_type'),
                _first_present(payload, 'target_object_id', 'record_id', 'object_id'),
            )
            due_at = _parse_datetime_value(
                _first_present(payload, 'due_at', 'due_date'),
                field_name='Due date/time',
                user_timezone=user_tz,
                required=False,
            )
            is_completed = _parse_bool(
                _first_present(payload, 'is_completed', 'done'),
                field_name='Completed',
                default=False,
            )
            assignee = _resolve_assignee(workspace, payload)
            record = Activity.objects.create(
                workspace=workspace,
                owner=user,
                created_by=user,
                visibility=visibility,
                shared_role=shared_role,
                activity_type=act_type,
                target_object_type=target_type,
                target_object_id=target_id,
                summary=summary,
                due_at=due_at,
                is_completed=is_completed,
                assignee=assignee,
            )

        record_label = get_record_label(record, object_key)
        record_audit_event(
            workspace=workspace,
            object_type=object_key,
            object_id=record.id,
            record_label=record_label,
            action=AuditLog.ACTION_CREATE,
            field_name='record',
            previous_value=None,
            new_value=record_label,
            actor_kind=AuditLog.ACTOR_KIND_PERSON,
            actor_user=user,
            record=record,
        )

    fresh = _build_base_queryset(workspace, object_key, user=user).get(pk=record.pk)
    return {
        'record': serialize_crm_record(
            fresh,
            object_key,
            include_detail=True,
            user=user,
            workspace=workspace,
        ),
        'message': f'{OBJECTS_BY_KEY[object_key].label} created.',
    }


def _apply_field_update(record, field_name: str, new_value, changes: list[tuple[str, object, object]]) -> None:
    old_value = getattr(record, field_name)
    if old_value == new_value:
        return
    old_display = _format_audit_field_value(old_value)
    new_display = _format_audit_field_value(new_value)
    setattr(record, field_name, new_value)
    if old_display != new_display:
        changes.append((field_name, old_display, new_display))


def update_crm_record(workspace, user, object_key: str, record_id: int, payload: dict) -> dict:
    """Validate and update a CRM record with per-field audit logging."""
    ensure_object_permission(user, workspace, object_key, 'update')
    _ensure_writable_fields(user, workspace, object_key, payload, is_create=False)
    user_tz = getattr(user, 'timezone', 'Africa/Accra') or 'Africa/Accra'
    model_cls = CRM_MODELS_BY_KEY[object_key]

    with transaction.atomic():
        record = (
            model_cls.objects.select_for_update(of=('self',))
            .select_related('shared_role')
            .filter(workspace=workspace, id=record_id)
            .first()
        )
        if record is None or not user_can_see_record(user, workspace, record):
            raise CrmDomainError(f'{OBJECTS_BY_KEY[object_key].label} not found.', status_code=404)

        changes: list[tuple[str, object, object]] = []
        link_synced = False

        if _has_any_key(payload, 'visibility', 'shared_role_id', 'shared_role'):
            next_visibility, next_shared_role, _ = _resolve_sharing_fields(
                workspace,
                payload,
                existing=record,
                user=user,
            )
            _apply_field_update(record, 'visibility', next_visibility, changes)
            _apply_field_update(record, 'shared_role', next_shared_role, changes)

        if object_key == 'account':
            if 'name' in payload:
                _apply_field_update(
                    record,
                    'name',
                    _parse_required_str(payload, 'name', label='Company name', max_length=255),
                    changes,
                )
            if 'city' in payload:
                _apply_field_update(
                    record,
                    'city',
                    _parse_optional_str(payload, 'city', label='City', max_length=120),
                    changes,
                )
            if 'phone' in payload:
                _apply_field_update(
                    record,
                    'phone',
                    _parse_optional_str(payload, 'phone', label='Phone', max_length=64),
                    changes,
                )
            if 'email' in payload:
                _apply_field_update(
                    record,
                    'email',
                    _parse_optional_email(payload, 'email'),
                    changes,
                )
            if 'notes' in payload:
                _apply_field_update(
                    record,
                    'notes',
                    _parse_optional_str(payload, 'notes', label='Notes', preserve_newlines=True),
                    changes,
                )

        elif object_key == 'contact':
            contact_label = _get_contact_screen_label(workspace)
            if 'name' in payload:
                _apply_field_update(
                    record,
                    'name',
                    _parse_required_str(
                        payload,
                        'name',
                        label=f'{contact_label} name',
                        max_length=255,
                    ),
                    changes,
                )
            if 'title' in payload:
                _apply_field_update(
                    record,
                    'title',
                    _parse_optional_str(payload, 'title', label='Title', max_length=150),
                    changes,
                )
            if 'phone' in payload:
                _apply_field_update(
                    record,
                    'phone',
                    _parse_optional_str(payload, 'phone', label='Phone', max_length=64),
                    changes,
                )
            if 'email' in payload:
                _apply_field_update(
                    record,
                    'email',
                    _parse_optional_email(payload, 'email'),
                    changes,
                )
            if 'sms_consent' in payload:
                _apply_field_update(
                    record,
                    'sms_consent',
                    _parse_bool(payload.get('sms_consent'), field_name='SMS consent'),
                    changes,
                )
            if 'notes' in payload:
                _apply_field_update(
                    record,
                    'notes',
                    _parse_optional_str(payload, 'notes', label='Notes', preserve_newlines=True),
                    changes,
                )
            if _has_any_key(payload, 'account_id', 'account'):
                next_account, _ = _resolve_account(workspace, payload, existing=record, user=user)
                old_account_id = record.account_id
                next_account_id = next_account.id if next_account else None
                if old_account_id is not None and next_account_id != old_account_id:
                    has_paired_links = (
                        Deal.objects.filter(
                            workspace=workspace,
                            contact=record,
                            account_id=old_account_id,
                        ).exists()
                        or Job.objects.filter(
                            workspace=workspace,
                            contact=record,
                            account_id=old_account_id,
                        ).exists()
                        or Ticket.objects.filter(
                            workspace=workspace,
                            contact=record,
                            account_id=old_account_id,
                        ).exists()
                    )
                    if has_paired_links:
                        raise CrmDomainError(
                            f'Cannot change Account for this {contact_label} while linked Deals, Jobs, or Tickets pair them with {record.account.name}.',
                            status_code=409,
                        )
                _apply_field_update(record, 'account', next_account, changes)

        elif object_key == 'lead':
            is_converted = record.status == Lead.STATUS_CONVERTED
            next_name = (
                _parse_required_str(payload, 'name', label='Lead name', max_length=255)
                if 'name' in payload
                else record.name
            )
            next_company = (
                _parse_optional_str(payload, 'company', label='Company', max_length=255)
                if 'company' in payload
                else record.company
            )
            next_phone = (
                _parse_optional_str(payload, 'phone', label='Phone', max_length=64)
                if 'phone' in payload
                else record.phone
            )
            next_email = (
                _parse_optional_email(payload, 'email')
                if 'email' in payload
                else record.email
            )
            if is_converted and (
                next_name != record.name
                or next_company != record.company
                or next_phone != record.phone
                or next_email != record.email
            ):
                contact_label = _get_contact_screen_label(workspace)
                raise CrmDomainError(
                    f'Name, company, email, and phone stay with the Account and {contact_label} created from this converted lead.',
                    status_code=400,
                )
            if 'name' in payload:
                _apply_field_update(record, 'name', next_name, changes)
            if 'company' in payload:
                _apply_field_update(record, 'company', next_company, changes)
            if 'phone' in payload:
                _apply_field_update(record, 'phone', next_phone, changes)
            if 'email' in payload:
                _apply_field_update(record, 'email', next_email, changes)
            if 'sms_consent' in payload:
                _apply_field_update(
                    record,
                    'sms_consent',
                    _parse_bool(payload.get('sms_consent'), field_name='SMS consent'),
                    changes,
                )
            if 'notes' in payload:
                _apply_field_update(
                    record,
                    'notes',
                    _parse_optional_str(payload, 'notes', label='Notes', preserve_newlines=True),
                    changes,
                )
            if 'status' in payload:
                requested = str(payload.get('status') or '').strip().lower()
                if requested != record.status:
                    raise CrmDomainError(
                        'Lead conversion status cannot be changed directly via edit.',
                        status_code=400,
                    )

        elif object_key == 'deal':
            if _has_any_key(payload, 'title', 'name'):
                _apply_field_update(
                    record,
                    'title',
                    _parse_required_str(payload, 'title', 'name', label='Deal title', max_length=255),
                    changes,
                )
            if _has_any_key(payload, 'stage', 'stage_id', 'pipeline_stage_id'):
                stage_obj = resolve_deal_stage(
                    workspace,
                    payload.get('stage'),
                    stage_id=_first_present(payload, 'stage_id', 'pipeline_stage_id'),
                    allow_default=False,
                )
                if (
                    record.pipeline_id != stage_obj.pipeline_id
                    or record.pipeline_stage_id != stage_obj.id
                ):
                    record.pipeline = stage_obj.pipeline
                    record.pipeline_stage = stage_obj
                    link_synced = True
                _apply_field_update(
                    record,
                    'stage',
                    stage_obj.name,
                    changes,
                )
            if 'amount' in payload:
                _apply_field_update(
                    record,
                    'amount',
                    _parse_decimal(
                        payload.get('amount'),
                        field_name='Amount',
                        allow_null=False,
                        default=Decimal('0.00'),
                    ),
                    changes,
                )
            if 'target_close_date' in payload:
                _apply_field_update(
                    record,
                    'target_close_date',
                    _parse_optional_date(
                        payload.get('target_close_date'),
                        field_name='Target close date',
                    ),
                    changes,
                )
            if 'win_probability' in payload:
                _apply_field_update(
                    record,
                    'win_probability',
                    _parse_int_range(
                        payload.get('win_probability'),
                        field_name='Win probability',
                        min_val=0,
                        max_val=100,
                        allow_null=True,
                    ),
                    changes,
                )
            if 'estimated_margin' in payload:
                _apply_field_update(
                    record,
                    'estimated_margin',
                    _parse_decimal(
                        payload.get('estimated_margin'),
                        field_name='Estimated margin',
                        allow_null=True,
                    ),
                    changes,
                )
            if 'notes' in payload:
                _apply_field_update(
                    record,
                    'notes',
                    _parse_optional_str(payload, 'notes', label='Notes', preserve_newlines=True),
                    changes,
                )
            if _has_any_key(payload, 'account_id', 'account', 'contact_id', 'contact'):
                next_account, next_contact = _resolve_account_and_contact_pair(
                    workspace,
                    payload,
                    existing=record,
                    user=user,
                )
                _apply_field_update(record, 'account', next_account, changes)
                _apply_field_update(record, 'contact', next_contact, changes)

        elif object_key == 'job':
            if _has_any_key(payload, 'title', 'name'):
                _apply_field_update(
                    record,
                    'title',
                    _parse_required_str(payload, 'title', 'name', label='Job title', max_length=255),
                    changes,
                )
            if 'status' in payload:
                _apply_field_update(
                    record,
                    'status',
                    _validate_choice(payload.get('status'), Job.DEFAULT_STATUSES, field_name='Status'),
                    changes,
                )
            if _has_any_key(payload, 'assignee_id', 'assignee'):
                _apply_field_update(
                    record,
                    'assignee',
                    _resolve_assignee(workspace, payload, existing=record),
                    changes,
                )
            if 'due_date' in payload:
                _apply_field_update(
                    record,
                    'due_date',
                    _parse_optional_date(payload.get('due_date'), field_name='Due date'),
                    changes,
                )
            if 'notes' in payload:
                _apply_field_update(
                    record,
                    'notes',
                    _parse_optional_str(payload, 'notes', label='Notes', preserve_newlines=True),
                    changes,
                )
            if _has_any_key(payload, 'account_id', 'account', 'contact_id', 'contact'):
                next_account, next_contact = _resolve_account_and_contact_pair(
                    workspace,
                    payload,
                    existing=record,
                    user=user,
                )
                if next_account is None and next_contact is None:
                    contact_label = _get_contact_screen_label(workspace)
                    raise CrmDomainError(
                        f'A Job must link to an Account or a {contact_label}.',
                        status_code=400,
                    )
                _apply_field_update(record, 'account', next_account, changes)
                _apply_field_update(record, 'contact', next_contact, changes)

        elif object_key == 'appointment':
            contact_label = _get_contact_screen_label(workspace)
            if _has_any_key(payload, 'title', 'name'):
                _apply_field_update(
                    record,
                    'title',
                    _parse_required_str(payload, 'title', 'name', label='Title', max_length=255),
                    changes,
                )
            if _has_any_key(payload, 'scheduled_at', 'time'):
                _apply_field_update(
                    record,
                    'scheduled_at',
                    _parse_datetime_value(
                        _first_present(payload, 'scheduled_at', 'time'),
                        field_name='Appointment time',
                        user_timezone=user_tz,
                        required=True,
                    ),
                    changes,
                )
            if 'status' in payload:
                _apply_field_update(
                    record,
                    'status',
                    _validate_choice(
                        payload.get('status'),
                        Appointment.DEFAULT_STATUSES,
                        field_name='Status',
                    ),
                    changes,
                )
            if _has_any_key(payload, 'assignee_id', 'assignee'):
                _apply_field_update(
                    record,
                    'assignee',
                    _resolve_assignee(workspace, payload, existing=record),
                    changes,
                )
            if _has_any_key(payload, 'contact_id', 'contact'):
                next_contact, _ = _resolve_contact(workspace, payload, existing=record, user=user)
                if next_contact is None:
                    raise CrmDomainError(
                        f'An Appointment must link to a {contact_label}.',
                        status_code=400,
                    )
                _apply_field_update(record, 'contact', next_contact, changes)
            if 'notes' in payload:
                _apply_field_update(
                    record,
                    'notes',
                    _parse_optional_str(payload, 'notes', label='Notes', preserve_newlines=True),
                    changes,
                )

        elif object_key == 'visit':
            _reject_forbidden_visit_fields(payload)
            contact_label = _get_contact_screen_label(workspace)
            if _has_any_key(payload, 'date', 'visited_at'):
                _apply_field_update(
                    record,
                    'date',
                    _parse_datetime_value(
                        _first_present(payload, 'date', 'visited_at'),
                        field_name='Visit date',
                        user_timezone=user_tz,
                        required=True,
                    ),
                    changes,
                )
            if 'reason' in payload:
                _apply_field_update(
                    record,
                    'reason',
                    _parse_required_str(payload, 'reason', label='Reason', max_length=255),
                    changes,
                )
            if 'notes' in payload:
                _apply_field_update(
                    record,
                    'notes',
                    _parse_optional_str(payload, 'notes', label='Notes', preserve_newlines=True),
                    changes,
                )
            if 'follow_up' in payload:
                _apply_field_update(
                    record,
                    'follow_up',
                    _parse_optional_str(
                        payload,
                        'follow_up',
                        label='Follow-up',
                        preserve_newlines=True,
                    ),
                    changes,
                )
            if _has_any_key(payload, 'assignee_id', 'assignee'):
                _apply_field_update(
                    record,
                    'assignee',
                    _resolve_assignee(workspace, payload, existing=record),
                    changes,
                )
            if _has_any_key(payload, 'contact_id', 'contact'):
                next_contact, _ = _resolve_contact(workspace, payload, existing=record, user=user)
                if next_contact is None:
                    raise CrmDomainError(
                        f'A Visit must link to a {contact_label}.',
                        status_code=400,
                    )
                _apply_field_update(record, 'contact', next_contact, changes)

        elif object_key == 'ticket':
            if _has_any_key(payload, 'title', 'subject'):
                _apply_field_update(
                    record,
                    'title',
                    _parse_required_str(
                        payload,
                        'title',
                        'subject',
                        label='Ticket title',
                        max_length=255,
                    ),
                    changes,
                )
            if 'status' in payload:
                _apply_field_update(
                    record,
                    'status',
                    _validate_choice(
                        payload.get('status'),
                        Ticket.DEFAULT_STATUSES,
                        field_name='Status',
                    ),
                    changes,
                )
            if 'priority' in payload:
                _apply_field_update(
                    record,
                    'priority',
                    _validate_choice(
                        payload.get('priority'),
                        Ticket.DEFAULT_PRIORITIES,
                        field_name='Priority',
                    ),
                    changes,
                )
            if _has_any_key(payload, 'assignee_id', 'assignee'):
                _apply_field_update(
                    record,
                    'assignee',
                    _resolve_assignee(workspace, payload, existing=record),
                    changes,
                )
            if _has_any_key(payload, 'description', 'notes'):
                _apply_field_update(
                    record,
                    'description',
                    _parse_optional_str(
                        payload,
                        'description',
                        'notes',
                        label='Description',
                        preserve_newlines=True,
                    ),
                    changes,
                )
            if _has_any_key(payload, 'account_id', 'account', 'contact_id', 'contact'):
                next_account, next_contact = _resolve_account_and_contact_pair(
                    workspace,
                    payload,
                    existing=record,
                    user=user,
                )
                _apply_field_update(record, 'account', next_account, changes)
                _apply_field_update(record, 'contact', next_contact, changes)

        elif object_key == 'activity':
            if _has_any_key(payload, 'target_object_type', 'record_type', 'object_type', 'target_object_id', 'record_id', 'object_id'):
                req_type = _first_present(payload, 'target_object_type', 'record_type', 'object_type')
                req_id = _first_present(payload, 'target_object_id', 'record_id', 'object_id')
                norm_req_type = (
                    OBJECT_KEY_ALIASES.get(req_type.strip().lower(), req_type.strip().lower())
                    if isinstance(req_type, str) and req_type.strip()
                    else record.target_object_type
                )
                norm_req_id = (
                    _parse_fk_id(req_id, field_name='target_object_id')
                    if req_id is not None and req_id != ''
                    else record.target_object_id
                )
                if norm_req_type != record.target_object_type or norm_req_id != record.target_object_id:
                    raise CrmDomainError(
                        'An Activity cannot be moved to a different parent record.',
                        status_code=400,
                    )
            if _has_any_key(payload, 'activity_type', 'type'):
                raw_type = _first_present(payload, 'activity_type', 'type')
                if not isinstance(raw_type, str) or not raw_type.strip():
                    raise CrmDomainError('Activity type is required.', status_code=400)
                act_type = raw_type.strip().lower()
                if act_type not in Activity.VALID_SPRINT2_TYPES:
                    raise CrmDomainError(
                        'Activity type must be note, task, call, or meeting.',
                        status_code=400,
                    )
                _apply_field_update(record, 'activity_type', act_type, changes)
            if _has_any_key(payload, 'summary', 'text', 'body', 'notes'):
                _apply_field_update(
                    record,
                    'summary',
                    _parse_required_str(
                        payload,
                        'summary',
                        'text',
                        'body',
                        'notes',
                        label='Summary',
                        max_length=4000,
                    ),
                    changes,
                )
            if _has_any_key(payload, 'due_at', 'due_date'):
                _apply_field_update(
                    record,
                    'due_at',
                    _parse_datetime_value(
                        _first_present(payload, 'due_at', 'due_date'),
                        field_name='Due date/time',
                        user_timezone=user_tz,
                        required=False,
                    ),
                    changes,
                )
            if _has_any_key(payload, 'is_completed', 'done'):
                _apply_field_update(
                    record,
                    'is_completed',
                    _parse_bool(
                        _first_present(payload, 'is_completed', 'done'),
                        field_name='Completed',
                    ),
                    changes,
                )
            if _has_any_key(payload, 'assignee_id', 'assignee'):
                _apply_field_update(
                    record,
                    'assignee',
                    _resolve_assignee(workspace, payload, existing=record),
                    changes,
                )

        if changes or link_synced:
            record.save()
        if changes:
            record_label = get_record_label(record, object_key)
            for field_name, prev_val, next_val in changes:
                record_audit_event(
                    workspace=workspace,
                    object_type=object_key,
                    object_id=record.id,
                    record_label=record_label,
                    action=AuditLog.ACTION_UPDATE,
                    field_name=field_name,
                    previous_value=prev_val,
                    new_value=next_val,
                    actor_kind=AuditLog.ACTOR_KIND_PERSON,
                    actor_user=user,
                    record=record,
                )

    fresh = _build_base_queryset(workspace, object_key).get(pk=record.pk)
    return {
        'record': serialize_crm_record(
            fresh,
            object_key,
            include_detail=True,
            user=user,
            workspace=workspace,
        ),
        'changed': bool(changes),
        'message': (
            f'{OBJECTS_BY_KEY[object_key].label} updated.'
            if changes
            else f'No changes made to {OBJECTS_BY_KEY[object_key].label}.'
        ),
    }


def delete_crm_record(workspace, user, object_key: str, record_id: int) -> dict:
    """Delete a CRM record if relational guards allow, cleaning up attached activities."""
    ensure_object_permission(user, workspace, object_key, 'delete')
    model_cls = CRM_MODELS_BY_KEY[object_key]
    contact_label = _get_contact_screen_label(workspace)

    with transaction.atomic():
        record = (
            model_cls.objects.select_for_update(of=('self',))
            .filter(workspace=workspace, id=record_id)
            .first()
        )
        if record is None or not user_can_see_record(user, workspace, record):
            raise CrmDomainError(f'{OBJECTS_BY_KEY[object_key].label} not found.', status_code=404)

        if object_key == 'account':
            if Job.objects.filter(
                workspace=workspace,
                account=record,
                contact__isnull=True,
            ).exists():
                raise CrmDomainError(
                    'Cannot delete this Account because one or more Jobs link only to it.',
                    status_code=409,
                )
        elif object_key == 'contact':
            if Appointment.objects.filter(workspace=workspace, contact=record).exists():
                raise CrmDomainError(
                    f'Cannot delete this {contact_label} because one or more Appointments are linked to it.',
                    status_code=409,
                )
            if Visit.objects.filter(workspace=workspace, contact=record).exists():
                raise CrmDomainError(
                    f'Cannot delete this {contact_label} because one or more Visits are linked to it.',
                    status_code=409,
                )
            if Job.objects.filter(
                workspace=workspace,
                contact=record,
                account__isnull=True,
            ).exists():
                raise CrmDomainError(
                    f'Cannot delete this {contact_label} because one or more Jobs link only to it.',
                    status_code=409,
                )
        elif object_key == 'lead':
            if record.status == Lead.STATUS_CONVERTED or record.converted_at is not None:
                raise CrmDomainError(
                    'Cannot delete a converted lead. Converted leads remain on record.',
                    status_code=409,
                )

        record_label = get_record_label(record, object_key)
        deleted_id = record.id
        deleted_visibility = record.visibility
        deleted_owner = record.owner
        deleted_shared_role = record.shared_role

        if object_key != 'activity':
            Activity.objects.filter(
                workspace=workspace,
                target_object_type=object_key,
                target_object_id=deleted_id,
            ).delete()

        record.delete()
        record_audit_event(
            workspace=workspace,
            object_type=object_key,
            object_id=deleted_id,
            record_label=record_label,
            action=AuditLog.ACTION_DELETE,
            field_name='record',
            previous_value=record_label,
            new_value=None,
            actor_kind=AuditLog.ACTOR_KIND_PERSON,
            actor_user=user,
            record_visibility=deleted_visibility,
            record_owner=deleted_owner,
            record_shared_role=deleted_shared_role,
        )

    return {
        'ok': True,
        'deleted_id': deleted_id,
        'message': f'{OBJECTS_BY_KEY[object_key].label} deleted.',
    }


def _compute_crm_options(workspace, user) -> dict:
    """Compute workspace assignees, roles, readable Accounts/Contacts summaries, and choice constants."""
    ensure_workspace_default_roles(workspace)
    vis_q = build_record_visibility_q(user, workspace)

    assignees = [
        {
            'id': member.id,
            'email': member.email,
            'role_name': member.role.name if member.role else 'Staff',
        }
        for member in workspace.users.select_related('role')
        .filter(is_active=True, email_verified=True)
        .order_by('email', 'id')
    ]

    roles = [
        {
            'id': role.id,
            'name': role.name,
            'is_owner': bool(role.is_owner),
            'is_default_staff': bool(role.is_default_staff),
        }
        for role in workspace.roles.order_by('-is_owner', '-is_default_staff', 'name', 'id')
    ]

    accounts = []
    accounts_total_count = 0
    if _is_enabled_and_readable(user, workspace, 'account'):
        acc_qs = Account.objects.filter(workspace=workspace).filter(vis_q).order_by('name', 'id')
        accounts_total_count = acc_qs.count()
        show_acc_city = _is_field_visible(user, workspace, 'account', 'city')
        accounts = [
            {
                'id': acc.id,
                'name': _get_visible_record_label(acc, 'account', user=user, workspace=workspace),
                **({'city': acc.city} if show_acc_city else {}),
            }
            for acc in acc_qs[:250]
        ]

    contacts = []
    contacts_total_count = 0
    if _is_enabled_and_readable(user, workspace, 'contact'):
        con_qs = (
            Contact.objects.select_related('account')
            .filter(workspace=workspace)
            .filter(vis_q)
            .order_by('name', 'id')
        )
        contacts_total_count = con_qs.count()
        show_con_email = _is_field_visible(user, workspace, 'contact', 'email')
        show_con_account = _is_field_visible(user, workspace, 'contact', 'account')
        contacts = []
        for con in con_qs[:250]:
            item = {
                'id': con.id,
                'name': _get_visible_record_label(con, 'contact', user=user, workspace=workspace),
            }
            if show_con_email:
                item['email'] = con.email
            if show_con_account and con.account_id and con.account and user_can_see_record(user, workspace, con.account):
                item['account_id'] = con.account_id
                item['account_name'] = _get_visible_record_label(con.account, 'account', user=user, workspace=workspace)
            else:
                item['account_id'] = None
                item['account_name'] = ''
            contacts.append(item)

    conversion_cap = get_lead_conversion_capability(user, workspace)
    pipeline = ensure_workspace_pipeline(workspace, require_enabled=False)
    pipeline_stages = (
        list(PipelineStage.objects.filter(pipeline=pipeline).order_by('position', 'id'))
        if pipeline is not None
        else []
    )
    return {
        'assignees': assignees,
        'roles': roles,
        'accounts': accounts,
        'accounts_total_count': accounts_total_count,
        'accounts_truncated': accounts_total_count > len(accounts),
        'contacts': contacts,
        'contacts_total_count': contacts_total_count,
        'contacts_truncated': contacts_total_count > len(contacts),
        'lead_conversion_available': conversion_cap['conversion_available'],
        'can_convert_lead': conversion_cap['can_convert_lead'],
        'constants': {
            'pipeline': (
                {'id': pipeline.id, 'name': pipeline.name}
                if pipeline is not None
                else None
            ),
            'deal_stages': [stage.name for stage in pipeline_stages],
            'deal_pipeline_stages': [serialize_pipeline_stage(stage) for stage in pipeline_stages],
            'job_statuses': list(Job.DEFAULT_STATUSES),
            'appointment_statuses': list(Appointment.DEFAULT_STATUSES),
            'ticket_statuses': list(Ticket.DEFAULT_STATUSES),
            'ticket_priorities': list(Ticket.DEFAULT_PRIORITIES),
            'activity_types': list(Activity.VALID_SPRINT2_TYPES),
        },
    }


def get_cached_crm_options_bundle(workspace, user) -> dict:
    """Return cached CRM lookup options with serialized JSON text and ETag, isolated by workspace and caller."""
    caller_scope = build_caller_cache_scope(user, workspace)
    return get_or_set_workspace_cache_bundle(
        workspace.id,
        domains=(DOMAIN_CATALOG, DOMAIN_LOOKUP_RECORDS, DOMAIN_MEMBERS, DOMAIN_ROLES),
        key_suffix=f'crm_options:{caller_scope}',
        scope=f'crm_options:{caller_scope}',
        loader=lambda: _compute_crm_options(workspace, user),
    )


def get_crm_options(workspace, user) -> dict:
    """Return workspace assignees, roles, readable Accounts/Contacts summaries, and choice constants."""
    return dict(get_cached_crm_options_bundle(workspace, user)['payload'])


_GHANA_CITY_TO_REGION: tuple[tuple[str, str], ...] = (
    ('accra', 'Greater Accra'),
    ('tema', 'Greater Accra'),
    ('madina', 'Greater Accra'),
    ('kasoa', 'Central'),
    ('spintex', 'Greater Accra'),
    ('east legon', 'Greater Accra'),
    ('cantonments', 'Greater Accra'),
    ('osu', 'Greater Accra'),
    ('labadi', 'Greater Accra'),
    ('kumasi', 'Ashanti'),
    ('obuasi', 'Ashanti'),
    ('suame', 'Ashanti'),
    ('ejisu', 'Ashanti'),
    ('takoradi', 'Western'),
    ('sekondi', 'Western'),
    ('tarkwa', 'Western'),
    ('sefwi', 'Western North'),
    ('bibiani', 'Western North'),
    ('tamale', 'Northern'),
    ('yendi', 'Northern'),
    ('koforidua', 'Eastern'),
    ('nsawam', 'Eastern'),
    ('nkawkaw', 'Eastern'),
    ('cape coast', 'Central'),
    ('winneba', 'Central'),
    ('ho', 'Volta'),
    ('hohoe', 'Volta'),
    ('keta', 'Volta'),
    ('sunyani', 'Bono'),
    ('berekum', 'Bono'),
    ('techiman', 'Bono East'),
    ('kintampo', 'Bono East'),
    ('goaso', 'Ahafo'),
    ('bolgatanga', 'Upper East'),
    ('navrongo', 'Upper East'),
    ('wa', 'Upper West'),
    ('damongo', 'Savannah'),
    ('nalerigu', 'North East'),
    ('dambai', 'Oti'),
)

_DASHBOARD_PANE_MAP: dict[str, str] = {
    'account': 'accounts',
    'contact': 'contacts',
    'lead': 'leads',
    'deal': 'deals',
    'job': 'jobs',
    'appointment': 'appointments',
    'visit': 'visits',
    'ticket': 'tickets',
}


def _map_city_to_ghana_region(city: str | None) -> str | None:
    cleaned = ' '.join((city or '').strip().lower().split())
    if not cleaned:
        return None
    for token, region in _GHANA_CITY_TO_REGION:
        if token in cleaned:
            return region
    return None


def _build_dashboard_ghana_hubs(workspace, user, vis_q, readable_object_keys) -> dict | None:
    if (
        'account' not in readable_object_keys
        or not _is_field_visible(user, workspace, 'account', 'city')
    ):
        return None

    rows = (
        Account.objects.filter(workspace=workspace)
        .filter(vis_q)
        .exclude(city='')
        .values_list('city', flat=True)
    )
    city_counts: dict[str, tuple[str, int]] = {}
    region_counts: dict[str, int] = {}
    total_with_city = 0

    for raw_city in rows:
        city_name = ' '.join((raw_city or '').strip().split())
        if not city_name:
            continue
        total_with_city += 1
        region = _map_city_to_ghana_region(city_name)
        key = city_name.lower()
        prev = city_counts.get(key)
        if prev is None:
            city_counts[key] = (city_name, 1)
        else:
            city_counts[key] = (prev[0], prev[1] + 1)
        if region:
            region_counts[region] = region_counts.get(region, 0) + 1

    sorted_cities = sorted(
        city_counts.values(),
        key=lambda item: (-item[1], item[0].lower()),
    )[:4]
    cities_payload = [
        {
            'name': name,
            'region': _map_city_to_ghana_region(name) or 'Other',
            'count': cnt,
            'percent': round((cnt / total_with_city) * 100) if total_with_city else 0,
        }
        for name, cnt in sorted_cities
    ]
    regions_payload = {
        region: {
            'count': cnt,
            'percent': round((cnt / total_with_city) * 100) if total_with_city else 0,
        }
        for region, cnt in sorted(
            region_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    }
    return {
        'total_accounts_with_city': total_with_city,
        'cities': cities_payload,
        'regions': regions_payload,
    }


def _build_dashboard_velocity_series(
    *,
    deals_qs,
    deal_readable: bool,
    amount_visible: bool,
    visit_qs,
    appointment_qs,
    activity_qs,
    today: date,
) -> dict[str, list[dict]]:
    """Aggregate real record dates into week/month/year/all buckets for SVG charts."""
    week_buckets: list[tuple[str, date, date]] = []
    for offset in range(6, -1, -1):
        d = today - timedelta(days=offset)
        week_buckets.append((d.strftime('%a'), d, d))

    month_buckets: list[tuple[str, date, date]] = []
    for idx in range(3, -1, -1):
        end_d = today - timedelta(days=idx * 7)
        start_d = end_d - timedelta(days=6)
        month_buckets.append((f'Wk {4 - idx}', start_d, end_d))

    def _shift_month(base: date, delta_months: int) -> tuple[int, int]:
        zero_m = (base.year * 12 + (base.month - 1)) + delta_months
        return zero_m // 12, (zero_m % 12) + 1

    year_buckets: list[tuple[str, date, date]] = []
    for m_off in range(-5, 1):
        yr, mo = _shift_month(today, m_off)
        start_d = date(yr, mo, 1)
        next_yr, next_mo = _shift_month(start_d, 1)
        end_d = date(next_yr, next_mo, 1) - timedelta(days=1)
        year_buckets.append((start_d.strftime('%b'), start_d, end_d))

    records_with_dates: list[tuple[date, float]] = []
    if deal_readable and deals_qs is not None:
        for close_d, created_dt, amt in deals_qs.values_list('target_close_date', 'created_at', 'amount'):
            ref_d = close_d or timezone.localdate(created_dt)
            records_with_dates.append((ref_d, float(amt or 0) if amount_visible else 1.0))
    else:
        if visit_qs is not None:
            for dt in visit_qs.values_list('date', flat=True):
                if dt:
                    records_with_dates.append((timezone.localdate(dt), 1.0))
        if appointment_qs is not None:
            for dt in appointment_qs.values_list('scheduled_at', flat=True):
                if dt:
                    records_with_dates.append((timezone.localdate(dt), 1.0))
        if activity_qs is not None:
            for due_dt, created_dt in activity_qs.values_list('due_at', 'created_at'):
                ref_dt = due_dt or created_dt
                if ref_dt:
                    records_with_dates.append((timezone.localdate(ref_dt), 1.0))

    def _fill(buckets: list[tuple[str, date, date]]) -> list[dict]:
        series = []
        for label, start_d, end_d in buckets:
            cnt = 0
            val = 0.0
            for rec_d, metric in records_with_dates:
                if start_d <= rec_d <= end_d:
                    cnt += 1
                    val += metric
            series.append({'label': label, 'count': cnt, 'value': round(val, 2)})
        return series

    return {
        'week': _fill(week_buckets),
        'month': _fill(month_buckets),
        'year': _fill(year_buckets),
        'all': _fill(year_buckets),
    }


def _compute_workspace_dashboard_summary(workspace, user) -> dict:
    """Compute role-, visibility-, and pack-aware record counts, pipeline metrics, and bento widgets."""
    ensure_workspace_catalog(workspace)
    vis_q = build_record_visibility_q(user, workspace)
    now = timezone.now()
    today = timezone.localdate(now)
    enabled_keys = set(
        WorkspaceTable.objects.filter(workspace=workspace, is_enabled=True)
        .values_list('object_key', flat=True)
    )

    s21_keys = ('account', 'contact', 'lead', 'deal', 'job', 'appointment', 'visit', 'ticket', 'activity')
    readable_object_keys = [
        obj_key
        for obj_key in s21_keys
        if obj_key in enabled_keys and user_has_object_right(user, workspace, obj_key, 'read')
    ]
    if is_workspace_owner(user, workspace):
        counts_by_object = (
            get_workspace_table_record_counts(workspace, readable_object_keys)
            if readable_object_keys
            else {}
        )
    else:
        counts_by_object = {
            obj_key: CRM_MODELS_BY_KEY[obj_key].objects.filter(workspace=workspace).filter(vis_q).count()
            for obj_key in readable_object_keys
        }
    total_records = sum(counts_by_object.values())

    open_counts: dict[str, int] = {}
    if 'lead' in readable_object_keys:
        open_counts['lead_open'] = Lead.objects.filter(
            workspace=workspace,
            status=Lead.STATUS_OPEN,
        ).filter(vis_q).count()
    if 'job' in readable_object_keys:
        open_counts['job_open'] = Job.objects.filter(
            workspace=workspace,
            status__in=('Pending', 'In Progress'),
        ).filter(vis_q).count()
    if 'appointment' in readable_object_keys:
        open_counts['appointment_upcoming'] = Appointment.objects.filter(
            workspace=workspace,
            status__in=('Scheduled', 'Confirmed'),
        ).filter(vis_q).count()
    if 'visit' in readable_object_keys and _is_field_visible(user, workspace, 'visit', 'follow_up'):
        open_counts['visit_with_follow_up'] = Visit.objects.filter(
            workspace=workspace,
        ).exclude(follow_up='').filter(vis_q).count()
    if 'ticket' in readable_object_keys:
        open_counts['ticket_open'] = Ticket.objects.filter(
            workspace=workspace,
            status__in=('New', 'Open', 'Pending'),
        ).filter(vis_q).count()
    if 'activity' in readable_object_keys:
        open_counts['task_open'] = Activity.objects.filter(
            workspace=workspace,
            activity_type=Activity.TYPE_TASK,
            is_completed=False,
        ).filter(vis_q).count()

    deal_summary = None
    active_deals: list[dict] = []
    deal_qs = None
    if 'deal' in readable_object_keys:
        deal_qs = Deal.objects.filter(workspace=workspace).filter(vis_q)
        stage_summary = _build_deal_stage_summary(workspace, deal_qs, user=user)
        stage_counts = stage_summary.get('stage_counts') or {}
        won_count = stage_counts.get('Won', 0)
        lost_count = stage_counts.get('Lost', 0)
        closed_count = won_count + lost_count
        total_deal_count = counts_by_object.get('deal', 0)
        open_deal_count = max(0, total_deal_count - closed_count)
        win_rate = round((won_count / closed_count) * 100) if closed_count else None
        deal_summary = {
            'total_count': total_deal_count,
            'open_count': open_deal_count,
            'won_count': won_count,
            'lost_count': lost_count,
            'win_rate': win_rate,
            **stage_summary,
        }

        acc_visible = _is_field_visible(user, workspace, 'deal', 'account')
        con_visible = _is_field_visible(user, workspace, 'deal', 'contact')
        amt_visible = _is_field_visible(user, workspace, 'deal', 'amount')
        stage_visible = _is_field_visible(user, workspace, 'deal', 'stage')
        prob_visible = _is_field_visible(user, workspace, 'deal', 'win_probability')
        close_visible = _is_field_visible(user, workspace, 'deal', 'target_close_date')

        for deal in deal_qs.select_related('account', 'contact', 'pipeline_stage').order_by('-updated_at', '-id')[:6]:
            row = {
                'id': deal.id,
                'record_code': f'{RECORD_CODE_PREFIXES["deal"]}-{deal.id}',
                'title': _get_visible_record_label(deal, 'deal', user=user, workspace=workspace),
                'updated_at': deal.updated_at.isoformat(),
            }
            if acc_visible and deal.account_id and deal.account and user_can_see_record(user, workspace, deal.account):
                row['account_id'] = deal.account_id
                row['account_name'] = _get_visible_record_label(deal.account, 'account', user=user, workspace=workspace)
            if con_visible and deal.contact_id and deal.contact and user_can_see_record(user, workspace, deal.contact):
                row['contact_id'] = deal.contact_id
                row['contact_name'] = _get_visible_record_label(deal.contact, 'contact', user=user, workspace=workspace)
            if amt_visible and deal.amount is not None:
                row['amount'] = f'{deal.amount:.2f}'
            if stage_visible:
                row['stage'] = deal.stage
                row['stage_tone'] = _resolve_deal_stage_tone(deal)
            if prob_visible and deal.win_probability is not None:
                row['win_probability'] = int(deal.win_probability)
            if close_visible and deal.target_close_date is not None:
                row['target_close_date'] = deal.target_close_date.isoformat()
            active_deals.append(row)

    # Activity / Task Summary (Row 1 KPI & Row 2 Action Items Checklist)
    task_summary = None
    activity_breakdown = None
    activity_qs = None
    if 'activity' in readable_object_keys:
        activity_qs = Activity.objects.filter(workspace=workspace).filter(vis_q)
        type_rows = activity_qs.values('activity_type').annotate(cnt=Count('id'))
        counts_by_type = {'note': 0, 'task': 0, 'call': 0, 'meeting': 0}
        for row in type_rows:
            t_key = row['activity_type']
            if t_key in counts_by_type:
                counts_by_type[t_key] = int(row['cnt'] or 0)
        activity_breakdown = {
            **counts_by_type,
            'total': sum(counts_by_type.values()),
        }

        tasks_qs = activity_qs.filter(activity_type=Activity.TYPE_TASK)
        task_total = tasks_qs.count()
        completed_visible = _is_field_visible(user, workspace, 'activity', 'is_completed')
        due_visible = _is_field_visible(user, workspace, 'activity', 'due_at')
        completed_count = tasks_qs.filter(is_completed=True).count() if completed_visible else 0
        open_task_count = max(0, task_total - completed_count) if completed_visible else task_total
        overdue_count = (
            tasks_qs.filter(is_completed=False, due_at__lt=now).count()
            if (completed_visible and due_visible)
            else 0
        )
        can_toggle_tasks = (
            user_has_object_right(user, workspace, 'activity', 'update')
            and (
                is_workspace_owner(user, workspace)
                or get_user_field_rights(user, workspace, 'activity').get('is_completed') == FIELD_RIGHT_EDITABLE
            )
        )

        ordered_tasks = list(tasks_qs.order_by('is_completed', 'due_at', '-updated_at', '-id')[:6])
        task_items: list[dict] = []
        for act in ordered_tasks:
            item = {
                'id': act.id,
                'record_code': f'{RECORD_CODE_PREFIXES["activity"]}-{act.id}',
                'summary': _get_visible_record_label(act, 'activity', user=user, workspace=workspace),
                'target_object_type': act.target_object_type,
                'target_pane_id': _DASHBOARD_PANE_MAP.get(act.target_object_type),
                'target_object_id': act.target_object_id,
                'target_record_label': _resolve_target_record_label(
                    workspace,
                    act.target_object_type,
                    act.target_object_id,
                    user=user,
                ),
            }
            if completed_visible:
                item['is_completed'] = bool(act.is_completed)
            if due_visible and act.due_at is not None:
                item['due_at'] = act.due_at.isoformat()
                item['is_overdue'] = bool((not act.is_completed) and act.due_at < now)
            else:
                item['is_overdue'] = False
            task_items.append(item)

        task_summary = {
            'total_count': task_total,
            'completed_count': completed_count,
            'open_count': open_task_count,
            'overdue_count': overdue_count,
            'can_toggle': can_toggle_tasks,
            'items': task_items,
        }

    # Visits & Appointments for Healthcare / service timelines
    recent_visits: list[dict] = []
    visit_qs = None
    if 'visit' in readable_object_keys:
        visit_qs = Visit.objects.filter(workspace=workspace).filter(vis_q)
        v_con_vis = _is_field_visible(user, workspace, 'visit', 'contact')
        v_date_vis = _is_field_visible(user, workspace, 'visit', 'date')
        v_fu_vis = _is_field_visible(user, workspace, 'visit', 'follow_up')
        for v in visit_qs.select_related('contact').order_by('-date', '-id')[:6]:
            row = {
                'id': v.id,
                'record_code': f'{RECORD_CODE_PREFIXES["visit"]}-{v.id}',
                'reason': _get_visible_record_label(v, 'visit', user=user, workspace=workspace),
            }
            if v_con_vis and v.contact_id and v.contact and user_can_see_record(user, workspace, v.contact):
                row['contact_id'] = v.contact_id
                row['contact_name'] = _get_visible_record_label(v.contact, 'contact', user=user, workspace=workspace)
            if v_date_vis and v.date:
                row['date'] = v.date.isoformat()
            if v_fu_vis and v.follow_up:
                row['follow_up'] = v.follow_up
            recent_visits.append(row)

    upcoming_appointments: list[dict] = []
    appointment_qs = None
    if 'appointment' in readable_object_keys:
        appointment_qs = Appointment.objects.filter(workspace=workspace).filter(vis_q)
        a_con_vis = _is_field_visible(user, workspace, 'appointment', 'contact')
        a_sched_vis = _is_field_visible(user, workspace, 'appointment', 'scheduled_at')
        a_status_vis = _is_field_visible(user, workspace, 'appointment', 'status')
        for a in appointment_qs.select_related('contact').order_by('-scheduled_at', '-id')[:6]:
            row = {
                'id': a.id,
                'record_code': f'{RECORD_CODE_PREFIXES["appointment"]}-{a.id}',
                'title': _get_visible_record_label(a, 'appointment', user=user, workspace=workspace),
            }
            if a_con_vis and a.contact_id and a.contact and user_can_see_record(user, workspace, a.contact):
                row['contact_id'] = a.contact_id
                row['contact_name'] = _get_visible_record_label(a.contact, 'contact', user=user, workspace=workspace)
            if a_sched_vis and a.scheduled_at:
                row['scheduled_at'] = a.scheduled_at.isoformat()
            if a_status_vis:
                row['status'] = a.status
            upcoming_appointments.append(row)

    # Active Tickets SLA Queue (Row 3 Right Bottom)
    active_tickets: list[dict] = []
    if 'ticket' in readable_object_keys:
        t_qs = Ticket.objects.filter(workspace=workspace).filter(vis_q).select_related('account', 'contact')
        open_t = list(t_qs.filter(status__in=('New', 'Open', 'Pending')).order_by('-updated_at', '-id')[:4])
        if len(open_t) < 4:
            seen_ids = {t.id for t in open_t}
            extra_t = list(t_qs.exclude(id__in=seen_ids).order_by('-updated_at', '-id')[: 4 - len(open_t)])
            open_t.extend(extra_t)
        t_status_vis = _is_field_visible(user, workspace, 'ticket', 'status')
        t_prio_vis = _is_field_visible(user, workspace, 'ticket', 'priority')
        t_acc_vis = _is_field_visible(user, workspace, 'ticket', 'account')
        for t in open_t:
            row = {
                'id': t.id,
                'record_code': f'{RECORD_CODE_PREFIXES["ticket"]}-{t.id}',
                'title': _get_visible_record_label(t, 'ticket', user=user, workspace=workspace),
                'updated_at': t.updated_at.isoformat(),
            }
            if t_status_vis:
                row['status'] = t.status
            if t_prio_vis:
                row['priority'] = t.priority
            if t_acc_vis and t.account_id and t.account and user_can_see_record(user, workspace, t.account):
                row['account_name'] = _get_visible_record_label(t.account, 'account', user=user, workspace=workspace)
            active_tickets.append(row)

    ghana_hubs = _build_dashboard_ghana_hubs(workspace, user, vis_q, readable_object_keys)
    velocity_series = _build_dashboard_velocity_series(
        deals_qs=deal_qs,
        deal_readable='deal' in readable_object_keys,
        amount_visible=_is_field_visible(user, workspace, 'deal', 'amount'),
        visit_qs=visit_qs,
        appointment_qs=appointment_qs,
        activity_qs=activity_qs,
        today=today,
    )

    latest_audit_res = list_workspace_audit_entries(workspace, user, page=1, page_size=1)
    latest_audit = latest_audit_res['results'][0] if latest_audit_res.get('results') else None

    recent_candidates: list[tuple[datetime, dict]] = []
    for obj_key, pane_id in _DASHBOARD_PANE_MAP.items():
        if obj_key not in readable_object_keys:
            continue
        model_cls = CRM_MODELS_BY_KEY[obj_key]
        row_qs = model_cls.objects.filter(workspace=workspace).filter(vis_q)
        if obj_key == 'visit':
            row_qs = row_qs.select_related('contact')
        rows = row_qs.order_by('-updated_at', '-id')[:4]
        for row in rows:
            meta_parts: list[str] = []
            if obj_key == 'account':
                if _is_field_visible(user, workspace, 'account', 'city') and row.city:
                    meta_parts.append(row.city)
                elif _is_field_visible(user, workspace, 'account', 'email') and row.email:
                    meta_parts.append(row.email)
                else:
                    meta_parts.append('Account')
            elif obj_key == 'contact':
                if _is_field_visible(user, workspace, 'contact', 'title') and row.title:
                    meta_parts.append(row.title)
                elif _is_field_visible(user, workspace, 'contact', 'email') and row.email:
                    meta_parts.append(row.email)
                else:
                    meta_parts.append(_get_contact_screen_label(workspace))
            elif obj_key == 'lead':
                if _is_field_visible(user, workspace, 'lead', 'company') and row.company:
                    meta_parts.append(row.company)
                if _is_field_visible(user, workspace, 'lead', 'status'):
                    meta_parts.append('Converted' if row.status == Lead.STATUS_CONVERTED else row.status.title())
            elif obj_key == 'deal':
                if _is_field_visible(user, workspace, 'deal', 'stage'):
                    meta_parts.append(row.stage)
                if _is_field_visible(user, workspace, 'deal', 'amount'):
                    meta_parts.append(f'GHS {row.amount:.2f}')
            elif obj_key == 'job':
                if _is_field_visible(user, workspace, 'job', 'status'):
                    meta_parts.append(row.status)
            elif obj_key == 'appointment':
                if _is_field_visible(user, workspace, 'appointment', 'status'):
                    meta_parts.append(row.status)
            elif obj_key == 'visit':
                if (
                    _is_field_visible(user, workspace, 'visit', 'contact')
                    and row.contact_id
                    and row.contact
                    and user_can_see_record(user, workspace, row.contact)
                ):
                    meta_parts.append(
                        _get_visible_record_label(row.contact, 'contact', user=user, workspace=workspace)
                    )
                if _is_field_visible(user, workspace, 'visit', 'date') and row.date:
                    meta_parts.append(row.date.strftime('%Y-%m-%d'))
            elif obj_key == 'ticket':
                if _is_field_visible(user, workspace, 'ticket', 'status'):
                    meta_parts.append(row.status)
                if _is_field_visible(user, workspace, 'ticket', 'priority'):
                    meta_parts.append(row.priority)

            meta = ' · '.join(meta_parts) or OBJECTS_BY_KEY[obj_key].label
            recent_candidates.append(
                (
                    row.updated_at,
                    {
                        'id': row.id,
                        'object_key': obj_key,
                        'pane_id': pane_id,
                        'record_code': f'{RECORD_CODE_PREFIXES[obj_key]}-{row.id}',
                        'display_label': _get_visible_record_label(row, obj_key, user=user, workspace=workspace),
                        'meta': meta,
                        'updated_at': row.updated_at.isoformat(),
                    },
                )
            )

    recent_candidates.sort(key=lambda item: (item[0], item[1]['id']), reverse=True)
    recent_records = [item[1] for item in recent_candidates[:8]]

    return {
        'total_records': total_records,
        'counts_by_object': counts_by_object,
        'readable_object_keys': readable_object_keys,
        'open_counts': open_counts,
        'deal_summary': deal_summary,
        'task_summary': task_summary,
        'activity_breakdown': activity_breakdown,
        'active_deals': active_deals,
        'recent_visits': recent_visits,
        'upcoming_appointments': upcoming_appointments,
        'active_tickets': active_tickets,
        'ghana_hubs': ghana_hubs,
        'velocity_series': velocity_series,
        'latest_audit': latest_audit,
        'recent_records': recent_records,
    }


def get_cached_dashboard_summary_bundle(workspace, user) -> dict:
    """Return cached dashboard summary with serialized JSON text and ETag, isolated by workspace and caller."""
    caller_scope = build_caller_cache_scope(user, workspace)
    return get_or_set_workspace_cache_bundle(
        workspace.id,
        domains=(DOMAIN_CATALOG, DOMAIN_RECORDS, DOMAIN_ROLES, DOMAIN_MEMBERS),
        key_suffix=f'dashboard:{caller_scope}',
        scope=f'dashboard:{caller_scope}',
        loader=lambda: _compute_workspace_dashboard_summary(workspace, user),
    )


def get_workspace_dashboard_summary(workspace, user) -> dict:
    """Return role-, visibility-, and pack-aware record counts, pipeline metrics, and recent records."""
    return dict(get_cached_dashboard_summary_bundle(workspace, user)['payload'])


def get_deals_board(
    workspace,
    user,
    *,
    query_params,
    per_stage_limit: int = DEFAULT_BOARD_PER_STAGE_LIMIT,
) -> dict:
    """Return ordered pipeline columns with per-stage bounded Deal slices and SQL totals."""
    ensure_object_permission(user, workspace, 'deal', 'read')
    pipeline = ensure_workspace_pipeline(workspace, require_enabled=True)
    stages = list(PipelineStage.objects.filter(pipeline=pipeline).order_by('position', 'id'))

    base_qs = _apply_deal_filters(
        _build_base_queryset(workspace, 'deal', user=user),
        query_params,
        include_stage=False,
        user=user,
        workspace=workspace,
    )
    summary = _build_deal_stage_summary(workspace, base_qs, user=user)
    total_count = base_qs.count()
    amount_visible = _is_field_visible(user, workspace, 'deal', 'amount')

    columns = []
    for stage in stages:
        col_qs = base_qs.filter(
            Q(pipeline_stage=stage)
            | Q(pipeline_stage__isnull=True, stage__iexact=stage.name)
        )
        col_deals = list(col_qs[:per_stage_limit])
        col_count = summary['stage_counts'].get(stage.name, 0) if summary['stage_counts'] else col_qs.count()
        col_amount = summary['stage_amounts'].get(stage.name, '0.00') if amount_visible else None
        columns.append(
            {
                **serialize_pipeline_stage(stage),
                'deal_count': col_count,
                'total_amount': col_amount,
                'has_more': col_count > len(col_deals),
                'deals': [
                    serialize_crm_record(deal, 'deal', user=user, workspace=workspace)
                    for deal in col_deals
                ],
            }
        )

    conversion_cap = get_lead_conversion_capability(user, workspace)
    return {
        'pipeline': {
            'id': pipeline.id,
            'name': pipeline.name,
            'is_default': bool(pipeline.is_default),
        },
        'stages': columns,
        'total_count': total_count,
        'per_stage_limit': per_stage_limit,
        'summary': summary,
        'permissions': {
            'can_create': user_has_object_right(user, workspace, 'deal', 'create'),
            'can_read': True,
            'can_update': user_has_object_right(user, workspace, 'deal', 'update'),
            'can_delete': user_has_object_right(user, workspace, 'deal', 'delete'),
            'can_manage_sales_settings': user_can_manage_sales_settings(user, workspace),
            'conversion_available': conversion_cap['conversion_available'],
            'can_convert_lead': conversion_cap['can_convert_lead'],
            'field_rights': get_user_field_rights(user, workspace, 'deal'),
        },
    }
