from dataclasses import dataclass
from datetime import datetime, time
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
from ..models import (
    Account,
    Activity,
    Appointment,
    AuditLog,
    Contact,
    Deal,
    Job,
    Lead,
    Ticket,
    User,
    WorkspaceTable,
)
from .audit_service import record_audit_event
from .catalog_service import ensure_workspace_catalog, get_workspace_table_record_counts
from .role_service import user_has_object_right

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100

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
    'ticket': Ticket,
}


@dataclass(frozen=True)
class CrmDomainError(Exception):
    """Domain validation, permission, or conflict error for CRM CRUD operations."""

    message: str
    status_code: int = 400

    def __str__(self) -> str:
        return self.message


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


def _resolve_account(workspace, payload: dict, *, existing=None) -> tuple[Account | None, bool]:
    explicit = _has_any_key(payload, 'account_id', 'account')
    if not explicit:
        return (existing.account if existing is not None else None), False
    raw = _first_present(payload, 'account_id', 'account')
    pk = _parse_fk_id(raw, field_name='Account')
    if pk is None:
        return None, True
    account = Account.objects.filter(workspace=workspace, id=pk).first()
    if account is None:
        raise CrmDomainError('Selected Account was not found in this workspace.', status_code=400)
    return account, True


def _resolve_contact(workspace, payload: dict, *, existing=None) -> tuple[Contact | None, bool]:
    contact_label = _get_contact_screen_label(workspace)
    explicit = _has_any_key(payload, 'contact_id', 'contact')
    if not explicit:
        return (existing.contact if existing is not None else None), False
    raw = _first_present(payload, 'contact_id', 'contact')
    pk = _parse_fk_id(raw, field_name=contact_label)
    if pk is None:
        return None, True
    contact = (
        Contact.objects.select_related('account')
        .filter(workspace=workspace, id=pk)
        .first()
    )
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
) -> tuple[Account | None, Contact | None]:
    """Resolve linked Account and Contact with auto-fill and mismatch enforcement."""
    contact_label = _get_contact_screen_label(workspace)
    account, account_explicit = _resolve_account(workspace, payload, existing=existing)
    contact, contact_explicit = _resolve_contact(workspace, payload, existing=existing)

    if contact is not None and contact.account_id is not None:
        if not account_explicit:
            # Auto-fill Account from Contact when omitted or when Contact changed without specifying Account.
            if existing is None or contact_explicit or account is None:
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
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)


def _resolve_activity_target(workspace, user, target_type_raw, target_id_raw):
    """Validate an Activity target record inside the same workspace."""
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
    target_record = target_model.objects.filter(workspace=workspace, id=target_id).first()
    if target_record is None:
        raise CrmDomainError('Target record not found in this workspace.', status_code=404)

    return normalized_type, target_id, target_record


def _serialize_user_ref(user_obj: User | None) -> dict | None:
    if user_obj is None:
        return None
    return {'id': user_obj.id, 'email': user_obj.email}


def _serialize_account_ref(account: Account | None) -> dict | None:
    if account is None:
        return None
    return {'id': account.id, 'name': account.name, 'city': account.city}


def _serialize_contact_ref(contact: Contact | None) -> dict | None:
    if contact is None:
        return None
    return {
        'id': contact.id,
        'name': contact.name,
        'email': contact.email,
        'phone': contact.phone,
        'account_id': contact.account_id,
    }


def _resolve_target_record_label(workspace, target_type: str, target_id: int) -> str:
    model_cls = CRM_MODELS_BY_KEY.get(target_type)
    if model_cls is None:
        return f'{target_type} #{target_id}'
    row = model_cls.objects.filter(workspace=workspace, id=target_id).first()
    if row is None:
        return f'{target_type} #{target_id}'
    return get_record_label(row, target_type)


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
    """Serialize any S2.1 CRM record for list or detail views."""
    prefix = RECORD_CODE_PREFIXES[object_key]
    data = {
        'id': record.id,
        'object_key': object_key,
        'record_code': f'{prefix}-{record.id}',
        'display_label': get_record_label(record, object_key),
        'visibility': record.visibility,
        'owner': _serialize_user_ref(record.owner),
        'created_by': _serialize_user_ref(record.created_by),
        'created_at': record.created_at.isoformat(),
        'updated_at': record.updated_at.isoformat(),
    }

    if object_key == 'account':
        data.update(
            {
                'name': record.name,
                'city': record.city,
                'phone': record.phone,
                'email': record.email,
                'notes': record.notes,
                'contacts_count': getattr(record, 'contacts_count', record.contacts.count()),
            }
        )
    elif object_key == 'contact':
        data.update(
            {
                'name': record.name,
                'title': record.title,
                'account_id': record.account_id,
                'account': _serialize_account_ref(record.account),
                'email': record.email,
                'phone': record.phone,
                'sms_consent': bool(record.sms_consent),
                'notes': record.notes,
            }
        )
    elif object_key == 'lead':
        data.update(
            {
                'name': record.name,
                'company': record.company,
                'email': record.email,
                'phone': record.phone,
                'sms_consent': bool(record.sms_consent),
                'status': record.status,
                'notes': record.notes,
            }
        )
    elif object_key == 'deal':
        data.update(
            {
                'title': record.title,
                'account_id': record.account_id,
                'account': _serialize_account_ref(record.account),
                'contact_id': record.contact_id,
                'contact': _serialize_contact_ref(record.contact),
                'amount': f'{record.amount:.2f}',
                'stage': record.stage,
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
            }
        )
    elif object_key == 'job':
        data.update(
            {
                'title': record.title,
                'status': record.status,
                'assignee_id': record.assignee_id,
                'assignee': _serialize_user_ref(record.assignee),
                'due_date': record.due_date.isoformat() if record.due_date else None,
                'account_id': record.account_id,
                'account': _serialize_account_ref(record.account),
                'contact_id': record.contact_id,
                'contact': _serialize_contact_ref(record.contact),
                'notes': record.notes,
            }
        )
    elif object_key == 'appointment':
        iso_time = record.scheduled_at.isoformat()
        data.update(
            {
                'title': record.title,
                'scheduled_at': iso_time,
                'time': iso_time,
                'status': record.status,
                'assignee_id': record.assignee_id,
                'assignee': _serialize_user_ref(record.assignee),
                'contact_id': record.contact_id,
                'contact': _serialize_contact_ref(record.contact),
                'notes': record.notes,
            }
        )
    elif object_key == 'ticket':
        data.update(
            {
                'title': record.title,
                'status': record.status,
                'priority': record.priority,
                'account_id': record.account_id,
                'account': _serialize_account_ref(record.account),
                'contact_id': record.contact_id,
                'contact': _serialize_contact_ref(record.contact),
                'assignee_id': record.assignee_id,
                'assignee': _serialize_user_ref(record.assignee),
                'description': record.description,
            }
        )
    elif object_key == 'activity':
        ws = workspace or record.workspace
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
                ),
                'summary': record.summary,
                'text': record.summary,
                'due_at': record.due_at.isoformat() if record.due_at else None,
                'is_completed': bool(record.is_completed),
                'assignee_id': record.assignee_id,
                'assignee': _serialize_user_ref(record.assignee),
            }
        )

    if include_detail and workspace is not None and user is not None:
        if object_key != 'activity':
            can_read_activities = _is_enabled_and_readable(user, workspace, 'activity')
            data['activities_readable'] = can_read_activities
            if can_read_activities:
                activities_qs = (
                    Activity.objects.filter(
                        workspace=workspace,
                        target_object_type=object_key,
                        target_object_id=record.id,
                    )
                    .select_related('owner', 'created_by', 'assignee')
                    .order_by('-created_at', '-id')
                )
                activities_total_count = activities_qs.count()
                activities = list(activities_qs[:100])
                data['activities'] = [
                    serialize_crm_record(act, 'activity', workspace=workspace)
                    for act in activities
                ]
                data['activities_total_count'] = activities_total_count
                data['has_more_activities'] = activities_total_count > len(activities)
            else:
                data['activities'] = []
                data['activities_total_count'] = 0
                data['has_more_activities'] = False

        data['linked_records'] = _serialize_linked_records(record, object_key, user=user, workspace=workspace)

    return data


def _serialize_linked_records(record, object_key: str, *, user, workspace) -> dict:
    linked: dict = {}

    if object_key == 'account':
        if _is_enabled_and_readable(user, workspace, 'contact'):
            linked['contacts'] = [
                {
                    'id': c.id,
                    'name': c.name,
                    'title': c.title,
                    'email': c.email,
                }
                for c in record.contacts.filter(workspace=workspace).order_by('name', 'id')[:50]
            ]
        if _is_enabled_and_readable(user, workspace, 'deal'):
            linked['deals'] = [
                {
                    'id': d.id,
                    'title': d.title,
                    'stage': d.stage,
                    'amount': f'{d.amount:.2f}',
                }
                for d in record.deals.filter(workspace=workspace).order_by('-updated_at', '-id')[:50]
            ]
        if _is_enabled_and_readable(user, workspace, 'job'):
            linked['jobs'] = [
                {
                    'id': j.id,
                    'title': j.title,
                    'status': j.status,
                    'due_date': j.due_date.isoformat() if j.due_date else None,
                }
                for j in record.jobs.filter(workspace=workspace).order_by('-updated_at', '-id')[:50]
            ]
        if _is_enabled_and_readable(user, workspace, 'ticket'):
            linked['tickets'] = [
                {
                    'id': t.id,
                    'title': t.title,
                    'status': t.status,
                    'priority': t.priority,
                }
                for t in record.tickets.filter(workspace=workspace).order_by('-updated_at', '-id')[:50]
            ]

    elif object_key == 'contact':
        if record.account_id and _is_enabled_and_readable(user, workspace, 'account'):
            linked['account'] = _serialize_account_ref(record.account)
        if _is_enabled_and_readable(user, workspace, 'deal'):
            linked['deals'] = [
                {
                    'id': d.id,
                    'title': d.title,
                    'stage': d.stage,
                    'amount': f'{d.amount:.2f}',
                }
                for d in record.deals.filter(workspace=workspace).order_by('-updated_at', '-id')[:50]
            ]
        if _is_enabled_and_readable(user, workspace, 'job'):
            linked['jobs'] = [
                {
                    'id': j.id,
                    'title': j.title,
                    'status': j.status,
                    'due_date': j.due_date.isoformat() if j.due_date else None,
                }
                for j in record.jobs.filter(workspace=workspace).order_by('-updated_at', '-id')[:50]
            ]
        if _is_enabled_and_readable(user, workspace, 'appointment'):
            linked['appointments'] = [
                {
                    'id': a.id,
                    'title': a.title,
                    'status': a.status,
                    'scheduled_at': a.scheduled_at.isoformat(),
                }
                for a in record.appointments.filter(workspace=workspace).order_by('scheduled_at', '-id')[:50]
            ]
        if _is_enabled_and_readable(user, workspace, 'ticket'):
            linked['tickets'] = [
                {
                    'id': t.id,
                    'title': t.title,
                    'status': t.status,
                    'priority': t.priority,
                }
                for t in record.tickets.filter(workspace=workspace).order_by('-updated_at', '-id')[:50]
            ]

    elif object_key in ('deal', 'job', 'ticket'):
        if record.account_id and _is_enabled_and_readable(user, workspace, 'account'):
            linked['account'] = _serialize_account_ref(record.account)
        if record.contact_id and _is_enabled_and_readable(user, workspace, 'contact'):
            linked['contact'] = _serialize_contact_ref(record.contact)

    elif object_key == 'appointment':
        if record.contact_id and _is_enabled_and_readable(user, workspace, 'contact'):
            linked['contact'] = _serialize_contact_ref(record.contact)

    return linked


def _build_base_queryset(workspace, object_key: str):
    model_cls = CRM_MODELS_BY_KEY[object_key]
    qs = model_cls.objects.filter(workspace=workspace).select_related('owner', 'created_by')
    if object_key == 'account':
        qs = qs.annotate(contacts_count=Count('contacts', distinct=True))
    elif object_key == 'contact':
        qs = qs.select_related('account')
    elif object_key == 'deal':
        qs = qs.select_related('account', 'contact')
    elif object_key == 'job':
        qs = qs.select_related('account', 'contact', 'assignee')
    elif object_key == 'appointment':
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

    qs = _build_base_queryset(workspace, object_key)
    q = (query_params.get('q') or '').strip()

    if object_key == 'account':
        if q:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(city__icontains=q)
                | Q(email__icontains=q)
                | Q(phone__icontains=q)
            )
        city = (query_params.get('city') or '').strip()
        if city:
            qs = qs.filter(city__iexact=city)

    elif object_key == 'contact':
        if q:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(title__icontains=q)
                | Q(email__icontains=q)
                | Q(phone__icontains=q)
                | Q(account__name__icontains=q)
            )
        raw_acc = query_params.get('account_id')
        if raw_acc:
            acc_id = _parse_fk_id(raw_acc, field_name='Account')
            qs = qs.filter(account_id=acc_id)
        raw_consent = (query_params.get('sms_consent') or '').strip().lower()
        if raw_consent in ('on', 'true', '1'):
            qs = qs.filter(sms_consent=True)
        elif raw_consent in ('off', 'false', '0'):
            qs = qs.filter(sms_consent=False)

    elif object_key == 'lead':
        if q:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(company__icontains=q)
                | Q(email__icontains=q)
                | Q(phone__icontains=q)
            )
        status_filter = (query_params.get('status') or '').strip().lower()
        if status_filter in (Lead.STATUS_OPEN, Lead.STATUS_CONVERTED):
            qs = qs.filter(status=status_filter)

    elif object_key == 'deal':
        if q:
            qs = qs.filter(
                Q(title__icontains=q)
                | Q(account__name__icontains=q)
                | Q(contact__name__icontains=q)
            )
        stage_filter = (query_params.get('stage') or '').strip()
        if stage_filter:
            qs = qs.filter(stage__iexact=stage_filter)
        if query_params.get('account_id'):
            qs = qs.filter(account_id=_parse_fk_id(query_params.get('account_id'), field_name='Account'))
        if query_params.get('contact_id'):
            qs = qs.filter(contact_id=_parse_fk_id(query_params.get('contact_id'), field_name='Contact'))

    elif object_key == 'job':
        if q:
            qs = qs.filter(
                Q(title__icontains=q)
                | Q(account__name__icontains=q)
                | Q(contact__name__icontains=q)
                | Q(assignee__email__icontains=q)
            )
        status_filter = (query_params.get('status') or '').strip()
        if status_filter:
            qs = qs.filter(status__iexact=status_filter)
        if query_params.get('assignee_id'):
            qs = qs.filter(assignee_id=_parse_fk_id(query_params.get('assignee_id'), field_name='Assignee'))
        if query_params.get('account_id'):
            qs = qs.filter(account_id=_parse_fk_id(query_params.get('account_id'), field_name='Account'))
        if query_params.get('contact_id'):
            qs = qs.filter(contact_id=_parse_fk_id(query_params.get('contact_id'), field_name='Contact'))

    elif object_key == 'appointment':
        if q:
            qs = qs.filter(
                Q(title__icontains=q)
                | Q(contact__name__icontains=q)
                | Q(assignee__email__icontains=q)
            )
        status_filter = (query_params.get('status') or '').strip()
        if status_filter:
            qs = qs.filter(status__iexact=status_filter)
        if query_params.get('assignee_id'):
            qs = qs.filter(assignee_id=_parse_fk_id(query_params.get('assignee_id'), field_name='Assignee'))
        if query_params.get('contact_id'):
            qs = qs.filter(contact_id=_parse_fk_id(query_params.get('contact_id'), field_name='Contact'))

    elif object_key == 'ticket':
        if q:
            qs = qs.filter(
                Q(title__icontains=q)
                | Q(account__name__icontains=q)
                | Q(contact__name__icontains=q)
                | Q(description__icontains=q)
            )
        status_filter = (query_params.get('status') or '').strip()
        if status_filter:
            qs = qs.filter(status__iexact=status_filter)
        priority_filter = (query_params.get('priority') or '').strip()
        if priority_filter:
            qs = qs.filter(priority__iexact=priority_filter)
        if query_params.get('assignee_id'):
            qs = qs.filter(assignee_id=_parse_fk_id(query_params.get('assignee_id'), field_name='Assignee'))
        if query_params.get('account_id'):
            qs = qs.filter(account_id=_parse_fk_id(query_params.get('account_id'), field_name='Account'))
        if query_params.get('contact_id'):
            qs = qs.filter(contact_id=_parse_fk_id(query_params.get('contact_id'), field_name='Contact'))

    elif object_key == 'activity':
        if q:
            qs = qs.filter(Q(summary__icontains=q))
        act_type = (query_params.get('activity_type') or '').strip().lower()
        if act_type:
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
        if query_params.get('assignee_id'):
            qs = qs.filter(assignee_id=_parse_fk_id(query_params.get('assignee_id'), field_name='Assignee'))

    summary = _compute_list_summary(workspace, object_key, qs)
    total_count = qs.count()
    total_pages = max(1, math.ceil(total_count / page_size)) if total_count else 1
    offset = (page - 1) * page_size
    rows = list(qs[offset : offset + page_size])

    return {
        'results': [
            serialize_crm_record(row, object_key, workspace=workspace)
            for row in rows
        ],
        'total_count': total_count,
        'page': page,
        'page_size': page_size,
        'total_pages': total_pages,
        'has_next': page < total_pages,
        'has_previous': page > 1,
        'summary': summary,
        'permissions': {
            'can_create': user_has_object_right(user, workspace, object_key, 'create'),
            'can_read': True,
            'can_update': user_has_object_right(user, workspace, object_key, 'update'),
            'can_delete': user_has_object_right(user, workspace, object_key, 'delete'),
        },
    }


def _compute_list_summary(workspace, object_key: str, qs) -> dict:
    if object_key == 'account':
        cities = list(
            Account.objects.filter(workspace=workspace)
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
        return {
            'sms_consented_count': qs.filter(sms_consent=True).count(),
        }
    if object_key == 'lead':
        return {
            'open_count': qs.filter(status=Lead.STATUS_OPEN).count(),
            'converted_count': qs.filter(status=Lead.STATUS_CONVERTED).count(),
        }
    if object_key == 'deal':
        total_amount = qs.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        stage_counts = {stage: 0 for stage in Deal.DEFAULT_STAGES}
        stage_amounts = {stage: '0.00' for stage in Deal.DEFAULT_STAGES}
        for row in qs.values('stage').annotate(cnt=Count('id'), val=Sum('amount')):
            stage_counts[row['stage']] = row['cnt']
            stage_amounts[row['stage']] = f"{(row['val'] or Decimal('0.00')):.2f}"
        return {
            'total_amount': f'{total_amount:.2f}',
            'stage_counts': stage_counts,
            'stage_amounts': stage_amounts,
        }
    if object_key == 'job':
        return {
            'open_count': qs.filter(status__in=('Pending', 'In Progress')).count(),
            'completed_count': qs.filter(status='Completed').count(),
        }
    if object_key == 'appointment':
        return {
            'upcoming_count': qs.filter(status__in=('Scheduled', 'Confirmed')).count(),
            'completed_count': qs.filter(status='Completed').count(),
        }
    if object_key == 'ticket':
        priority_counts = {prio: 0 for prio in Ticket.DEFAULT_PRIORITIES}
        for row in qs.values('priority').annotate(cnt=Count('id')):
            priority_counts[row['priority']] = row['cnt']
        return {
            'open_count': qs.filter(status__in=('New', 'Open', 'Pending')).count(),
            'resolved_count': qs.filter(status__in=('Resolved', 'Closed')).count(),
            'priority_counts': priority_counts,
        }
    return {}


def get_crm_record_detail(workspace, user, object_key: str, record_id: int) -> dict:
    """Fetch a single CRM record with linked summaries and activities."""
    ensure_object_permission(user, workspace, object_key, 'read')
    record = _build_base_queryset(workspace, object_key).filter(id=record_id).first()
    if record is None:
        raise CrmDomainError(f'{OBJECTS_BY_KEY[object_key].label} not found.', status_code=404)
    return {
        'record': serialize_crm_record(
            record,
            object_key,
            include_detail=True,
            user=user,
            workspace=workspace,
        ),
        'permissions': {
            'can_create': user_has_object_right(user, workspace, object_key, 'create'),
            'can_read': True,
            'can_update': user_has_object_right(user, workspace, object_key, 'update'),
            'can_delete': user_has_object_right(user, workspace, object_key, 'delete'),
            'activity': {
                'can_create': user_has_object_right(user, workspace, 'activity', 'create'),
                'can_read': user_has_object_right(user, workspace, 'activity', 'read'),
                'can_update': user_has_object_right(user, workspace, 'activity', 'update'),
                'can_delete': user_has_object_right(user, workspace, 'activity', 'delete'),
            },
        },
    }


def create_crm_record(workspace, user, object_key: str, payload: dict) -> dict:
    """Validate and create a new S2.1 CRM record with audit logging."""
    ensure_object_permission(user, workspace, object_key, 'create')
    user_tz = getattr(user, 'timezone', 'Africa/Accra') or 'Africa/Accra'

    with transaction.atomic():
        if object_key == 'account':
            name = _parse_required_str(payload, 'name', label='Company name', max_length=255)
            record = Account.objects.create(
                workspace=workspace,
                owner=user,
                created_by=user,
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
            account, _ = _resolve_account(workspace, payload)
            record = Contact.objects.create(
                workspace=workspace,
                owner=user,
                created_by=user,
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
                    'New leads start as open. Lead conversion is not part of this workflow.',
                    status_code=400,
                )
            record = Lead.objects.create(
                workspace=workspace,
                owner=user,
                created_by=user,
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
            account, contact = _resolve_account_and_contact_pair(workspace, payload)
            stage_raw = payload.get('stage', 'New')
            stage = _validate_choice(stage_raw, Deal.DEFAULT_STAGES, field_name='Stage')
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
                title=title,
                account=account,
                contact=contact,
                amount=amount,
                stage=stage,
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
            account, contact = _resolve_account_and_contact_pair(workspace, payload)
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
            contact, _ = _resolve_contact(workspace, payload)
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
                title=title,
                scheduled_at=scheduled_at,
                status=status,
                assignee=assignee,
                contact=contact,
                notes=_parse_optional_str(payload, 'notes', label='Notes', preserve_newlines=True),
            )

        elif object_key == 'ticket':
            title = _parse_required_str(
                payload,
                'title',
                'subject',
                label='Ticket title',
                max_length=255,
            )
            account, contact = _resolve_account_and_contact_pair(workspace, payload)
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
    user_tz = getattr(user, 'timezone', 'Africa/Accra') or 'Africa/Accra'
    model_cls = CRM_MODELS_BY_KEY[object_key]

    with transaction.atomic():
        record = (
            model_cls.objects.select_for_update(of=('self',))
            .filter(workspace=workspace, id=record_id)
            .first()
        )
        if record is None:
            raise CrmDomainError(f'{OBJECTS_BY_KEY[object_key].label} not found.', status_code=404)

        changes: list[tuple[str, object, object]] = []

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
                next_account, _ = _resolve_account(workspace, payload, existing=record)
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
            if 'name' in payload:
                _apply_field_update(
                    record,
                    'name',
                    _parse_required_str(payload, 'name', label='Lead name', max_length=255),
                    changes,
                )
            if 'company' in payload:
                _apply_field_update(
                    record,
                    'company',
                    _parse_optional_str(payload, 'company', label='Company', max_length=255),
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
            if 'stage' in payload:
                _apply_field_update(
                    record,
                    'stage',
                    _validate_choice(payload.get('stage'), Deal.DEFAULT_STAGES, field_name='Stage'),
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
                next_contact, _ = _resolve_contact(workspace, payload, existing=record)
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

        if changes:
            record.save()
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
        if record is None:
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
            if Job.objects.filter(
                workspace=workspace,
                contact=record,
                account__isnull=True,
            ).exists():
                raise CrmDomainError(
                    f'Cannot delete this {contact_label} because one or more Jobs link only to it.',
                    status_code=409,
                )

        record_label = get_record_label(record, object_key)
        deleted_id = record.id

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
        )

    return {
        'ok': True,
        'deleted_id': deleted_id,
        'message': f'{OBJECTS_BY_KEY[object_key].label} deleted.',
    }


def get_crm_options(workspace, user) -> dict:
    """Return workspace assignees, readable Accounts/Contacts summaries, and S2.1 choice constants."""
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

    accounts = []
    accounts_total_count = 0
    if _is_enabled_and_readable(user, workspace, 'account'):
        acc_qs = Account.objects.filter(workspace=workspace).order_by('name', 'id')
        accounts_total_count = acc_qs.count()
        accounts = [
            {'id': acc.id, 'name': acc.name, 'city': acc.city}
            for acc in acc_qs[:250]
        ]

    contacts = []
    contacts_total_count = 0
    if _is_enabled_and_readable(user, workspace, 'contact'):
        con_qs = (
            Contact.objects.select_related('account')
            .filter(workspace=workspace)
            .order_by('name', 'id')
        )
        contacts_total_count = con_qs.count()
        contacts = [
            {
                'id': con.id,
                'name': con.name,
                'email': con.email,
                'account_id': con.account_id,
                'account_name': con.account.name if con.account else '',
            }
            for con in con_qs[:250]
        ]

    return {
        'assignees': assignees,
        'accounts': accounts,
        'accounts_total_count': accounts_total_count,
        'accounts_truncated': accounts_total_count > len(accounts),
        'contacts': contacts,
        'contacts_total_count': contacts_total_count,
        'contacts_truncated': contacts_total_count > len(contacts),
        'constants': {
            'deal_stages': list(Deal.DEFAULT_STAGES),
            'job_statuses': list(Job.DEFAULT_STATUSES),
            'appointment_statuses': list(Appointment.DEFAULT_STATUSES),
            'ticket_statuses': list(Ticket.DEFAULT_STATUSES),
            'ticket_priorities': list(Ticket.DEFAULT_PRIORITIES),
            'activity_types': list(Activity.VALID_SPRINT2_TYPES),
        },
    }


def get_workspace_dashboard_summary(workspace, user) -> dict:
    """Return role- and pack-aware S2.1 record counts, pipeline metrics, and recent records."""
    ensure_workspace_catalog(workspace)
    enabled_keys = set(
        WorkspaceTable.objects.filter(workspace=workspace, is_enabled=True)
        .values_list('object_key', flat=True)
    )

    s21_keys = ('account', 'contact', 'lead', 'deal', 'job', 'appointment', 'ticket', 'activity')
    readable_object_keys = [
        obj_key
        for obj_key in s21_keys
        if obj_key in enabled_keys and user_has_object_right(user, workspace, obj_key, 'read')
    ]
    counts_by_object = (
        get_workspace_table_record_counts(workspace, readable_object_keys)
        if readable_object_keys
        else {}
    )
    total_records = sum(counts_by_object.values())

    open_counts: dict[str, int] = {}
    if 'lead' in readable_object_keys:
        open_counts['lead_open'] = Lead.objects.filter(
            workspace=workspace,
            status=Lead.STATUS_OPEN,
        ).count()
    if 'job' in readable_object_keys:
        open_counts['job_open'] = Job.objects.filter(
            workspace=workspace,
            status__in=('Pending', 'In Progress'),
        ).count()
    if 'appointment' in readable_object_keys:
        open_counts['appointment_upcoming'] = Appointment.objects.filter(
            workspace=workspace,
            status__in=('Scheduled', 'Confirmed'),
        ).count()
    if 'ticket' in readable_object_keys:
        open_counts['ticket_open'] = Ticket.objects.filter(
            workspace=workspace,
            status__in=('New', 'Open', 'Pending'),
        ).count()
    if 'activity' in readable_object_keys:
        open_counts['task_open'] = Activity.objects.filter(
            workspace=workspace,
            activity_type=Activity.TYPE_TASK,
            is_completed=False,
        ).count()

    deal_summary = None
    if 'deal' in readable_object_keys:
        deal_qs = Deal.objects.filter(workspace=workspace)
        total_amount = deal_qs.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        stage_counts = {stage: 0 for stage in Deal.DEFAULT_STAGES}
        stage_amounts = {stage: '0.00' for stage in Deal.DEFAULT_STAGES}
        for row in deal_qs.values('stage').annotate(cnt=Count('id'), val=Sum('amount')):
            stage_counts[row['stage']] = row['cnt']
            stage_amounts[row['stage']] = f"{(row['val'] or Decimal('0.00')):.2f}"
        deal_summary = {
            'total_count': counts_by_object.get('deal', 0),
            'total_amount': f'{total_amount:.2f}',
            'stage_counts': stage_counts,
            'stage_amounts': stage_amounts,
        }

    recent_candidates: list[tuple[datetime, dict]] = []
    pane_map = {
        'account': 'accounts',
        'contact': 'contacts',
        'lead': 'leads',
        'deal': 'deals',
        'job': 'jobs',
        'appointment': 'appointments',
        'ticket': 'tickets',
    }
    for obj_key, pane_id in pane_map.items():
        if obj_key not in readable_object_keys:
            continue
        model_cls = CRM_MODELS_BY_KEY[obj_key]
        rows = model_cls.objects.filter(workspace=workspace).order_by('-updated_at', '-id')[:4]
        for row in rows:
            meta = ''
            if obj_key == 'account':
                meta = row.city or row.email or 'Account'
            elif obj_key == 'contact':
                meta = row.title or row.email or _get_contact_screen_label(workspace)
            elif obj_key == 'lead':
                meta = row.company or row.status.title()
            elif obj_key == 'deal':
                meta = f'{row.stage} · GHS {row.amount:.2f}'
            elif obj_key == 'job':
                meta = row.status
            elif obj_key == 'appointment':
                meta = row.status
            elif obj_key == 'ticket':
                meta = f'{row.status} · {row.priority}'

            recent_candidates.append(
                (
                    row.updated_at,
                    {
                        'id': row.id,
                        'object_key': obj_key,
                        'pane_id': pane_id,
                        'record_code': f'{RECORD_CODE_PREFIXES[obj_key]}-{row.id}',
                        'display_label': get_record_label(row, obj_key),
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
        'recent_records': recent_records,
    }
