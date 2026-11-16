from django.utils import timezone

from ..models import AuditLog

VALID_ACTOR_KINDS = frozenset(
    {
        AuditLog.ACTOR_KIND_PERSON,
        AuditLog.ACTOR_KIND_SCRIPT,
        AuditLog.ACTOR_KIND_AUTOMATION,
        AuditLog.ACTOR_KIND_AGENT,
    }
)


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
