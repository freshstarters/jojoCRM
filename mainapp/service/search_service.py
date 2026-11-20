from dataclasses import dataclass
import math
import re

from django.db.models import CharField, Q, Value

from ..catalog import OBJECTS_BY_KEY
from ..helpers.crm_errors import CrmDomainError
from ..models import (
    Account,
    Activity,
    Appointment,
    Contact,
    Deal,
    Job,
    Lead,
    Ticket,
    WorkspaceTable,
)
from .catalog_service import ensure_workspace_catalog
from .crm_service import (
    CRM_MODELS_BY_KEY,
    OBJECT_KEY_ALIASES,
    RECORD_CODE_PREFIXES,
    _build_base_queryset,
    _get_visible_record_label,
    _is_field_visible,
    _resolve_deal_stage_tone,
)
from .role_service import (
    build_record_visibility_q,
    is_workspace_owner,
    user_can_see_record,
    user_has_object_right,
)

DEFAULT_SEARCH_PAGE_SIZE = 20
MAX_SEARCH_PAGE_SIZE = 100

_RECORD_CODE_RE = re.compile(r'^([A-Za-z]{2,4})-(\d+)$')
_CODE_PREFIX_TO_OBJECT: dict[str, str] = {
    prefix.upper(): obj_key for obj_key, prefix in RECORD_CODE_PREFIXES.items()
}


@dataclass(frozen=True)
class SearchObjectSpec:
    key: str
    pane_id: str | None
    code_prefix: str
    uses_contact_label: bool = False


SEARCH_OBJECT_REGISTRY: tuple[SearchObjectSpec, ...] = (
    SearchObjectSpec('account', 'accounts', 'ACC'),
    SearchObjectSpec('contact', 'contacts', 'CON', uses_contact_label=True),
    SearchObjectSpec('lead', 'leads', 'LD'),
    SearchObjectSpec('deal', 'deals', 'DL'),
    SearchObjectSpec('job', 'jobs', 'JOB'),
    SearchObjectSpec('appointment', 'appointments', 'APT'),
    SearchObjectSpec('visit', 'visits', 'VIS'),
    SearchObjectSpec('ticket', 'tickets', 'TCK'),
    SearchObjectSpec('activity', None, 'ACT'),
)

SEARCH_SPEC_BY_KEY: dict[str, SearchObjectSpec] = {
    spec.key: spec for spec in SEARCH_OBJECT_REGISTRY
}


def _parse_record_code_match(query_text: str) -> tuple[str, int] | None:
    match = _RECORD_CODE_RE.match((query_text or '').strip())
    if not match:
        return None
    prefix = match.group(1).upper()
    obj_key = _CODE_PREFIX_TO_OBJECT.get(prefix)
    if not obj_key:
        return None
    return obj_key, int(match.group(2))


def _object_labels_for_workspace(workspace, spec: SearchObjectSpec) -> tuple[str, str]:
    if spec.uses_contact_label:
        singular = (getattr(workspace, 'contact_label', '') or 'Contact').strip() or 'Contact'
        plural = (
            (getattr(workspace, 'contact_plural_label', '') or '').strip()
            or (f'{singular}s' if not singular.endswith('s') else singular)
        )
        return singular, plural
    obj_def = OBJECTS_BY_KEY[spec.key]
    return obj_def.label, obj_def.plural_label


def _build_related_name_q(
    workspace,
    user,
    *,
    owner_object_key: str,
    rel_field: str,
    target_object_key: str,
    enabled_keys: set[str],
    readable_keys: set[str],
    query_text: str,
) -> Q:
    """Return a safe Q matching a related Account/Contact name only if visible & readable."""
    if (
        target_object_key not in enabled_keys
        or target_object_key not in readable_keys
        or not _is_field_visible(user, workspace, owner_object_key, rel_field)
        or not _is_field_visible(user, workspace, target_object_key, 'name')
    ):
        return Q()
    rel_q = Q(**{f'{rel_field}__name__icontains': query_text})
    if not is_workspace_owner(user, workspace):
        rel_q &= build_record_visibility_q(user, workspace, prefix=f'{rel_field}__')
    return rel_q


def _build_activity_scoped_queryset(
    workspace,
    user,
    *,
    readable_parent_keys: set[str],
):
    """Return Activity queryset scoped by both Activity visibility and parent record visibility."""
    base_qs = _build_base_queryset(workspace, 'activity', user=user)
    if not readable_parent_keys:
        return base_qs.none()

    vis_q = Q() if is_workspace_owner(user, workspace) else build_record_visibility_q(user, workspace)
    parent_q = Q()
    for target_key in readable_parent_keys:
        if target_key not in Activity.VALID_TARGET_OBJECTS:
            continue
        model_cls = CRM_MODELS_BY_KEY.get(target_key)
        if model_cls is None:
            continue
        visible_parent_ids = (
            model_cls.objects.filter(workspace=workspace)
            .filter(vis_q)
            .values('id')
        )
        parent_q |= Q(target_object_type=target_key, target_object_id__in=visible_parent_ids)

    if not parent_q:
        return base_qs.none()
    return base_qs.filter(parent_q)


def _build_object_search_queryset(
    workspace,
    user,
    object_key: str,
    *,
    query_text: str,
    code_match: tuple[str, int] | None,
    enabled_keys: set[str],
    readable_keys: set[str],
):
    """Build a strictly permission- and field-scoped search queryset for one CRM object."""
    if object_key == 'activity':
        readable_parents = {
            k for k in readable_keys if k != 'activity' and k in Activity.VALID_TARGET_OBJECTS
        }
        qs = _build_activity_scoped_queryset(
            workspace,
            user,
            readable_parent_keys=readable_parents,
        )
    else:
        qs = _build_base_queryset(workspace, object_key, user=user)

    if not query_text:
        return qs

    search_q = Q()
    if code_match is not None and code_match[0] == object_key:
        search_q |= Q(id=code_match[1])

    if object_key == 'account':
        for field in ('name', 'city', 'email', 'phone', 'notes'):
            if _is_field_visible(user, workspace, 'account', field):
                search_q |= Q(**{f'{field}__icontains': query_text})

    elif object_key == 'contact':
        for field in ('name', 'title', 'email', 'phone', 'notes'):
            if _is_field_visible(user, workspace, 'contact', field):
                search_q |= Q(**{f'{field}__icontains': query_text})
        search_q |= _build_related_name_q(
            workspace,
            user,
            owner_object_key='contact',
            rel_field='account',
            target_object_key='account',
            enabled_keys=enabled_keys,
            readable_keys=readable_keys,
            query_text=query_text,
        )

    elif object_key == 'lead':
        for field in ('name', 'company', 'email', 'phone', 'status', 'notes'):
            if _is_field_visible(user, workspace, 'lead', field):
                search_q |= Q(**{f'{field}__icontains': query_text})

    elif object_key == 'deal':
        for field in ('title', 'stage', 'notes'):
            if _is_field_visible(user, workspace, 'deal', field):
                search_q |= Q(**{f'{field}__icontains': query_text})
        search_q |= _build_related_name_q(
            workspace,
            user,
            owner_object_key='deal',
            rel_field='account',
            target_object_key='account',
            enabled_keys=enabled_keys,
            readable_keys=readable_keys,
            query_text=query_text,
        )
        search_q |= _build_related_name_q(
            workspace,
            user,
            owner_object_key='deal',
            rel_field='contact',
            target_object_key='contact',
            enabled_keys=enabled_keys,
            readable_keys=readable_keys,
            query_text=query_text,
        )

    elif object_key == 'job':
        for field in ('title', 'status', 'notes'):
            if _is_field_visible(user, workspace, 'job', field):
                search_q |= Q(**{f'{field}__icontains': query_text})
        if _is_field_visible(user, workspace, 'job', 'assignee'):
            search_q |= Q(assignee__email__icontains=query_text)
        search_q |= _build_related_name_q(
            workspace,
            user,
            owner_object_key='job',
            rel_field='account',
            target_object_key='account',
            enabled_keys=enabled_keys,
            readable_keys=readable_keys,
            query_text=query_text,
        )
        search_q |= _build_related_name_q(
            workspace,
            user,
            owner_object_key='job',
            rel_field='contact',
            target_object_key='contact',
            enabled_keys=enabled_keys,
            readable_keys=readable_keys,
            query_text=query_text,
        )

    elif object_key == 'appointment':
        for field in ('title', 'status', 'notes'):
            if _is_field_visible(user, workspace, 'appointment', field):
                search_q |= Q(**{f'{field}__icontains': query_text})
        if _is_field_visible(user, workspace, 'appointment', 'assignee'):
            search_q |= Q(assignee__email__icontains=query_text)
        search_q |= _build_related_name_q(
            workspace,
            user,
            owner_object_key='appointment',
            rel_field='contact',
            target_object_key='contact',
            enabled_keys=enabled_keys,
            readable_keys=readable_keys,
            query_text=query_text,
        )

    elif object_key == 'visit':
        for field in ('reason', 'notes', 'follow_up'):
            if _is_field_visible(user, workspace, 'visit', field):
                search_q |= Q(**{f'{field}__icontains': query_text})
        if _is_field_visible(user, workspace, 'visit', 'assignee'):
            search_q |= Q(assignee__email__icontains=query_text)
        search_q |= _build_related_name_q(
            workspace,
            user,
            owner_object_key='visit',
            rel_field='contact',
            target_object_key='contact',
            enabled_keys=enabled_keys,
            readable_keys=readable_keys,
            query_text=query_text,
        )

    elif object_key == 'ticket':
        for field in ('title', 'status', 'priority', 'description'):
            if _is_field_visible(user, workspace, 'ticket', field):
                search_q |= Q(**{f'{field}__icontains': query_text})
        if _is_field_visible(user, workspace, 'ticket', 'assignee'):
            search_q |= Q(assignee__email__icontains=query_text)
        search_q |= _build_related_name_q(
            workspace,
            user,
            owner_object_key='ticket',
            rel_field='account',
            target_object_key='account',
            enabled_keys=enabled_keys,
            readable_keys=readable_keys,
            query_text=query_text,
        )
        search_q |= _build_related_name_q(
            workspace,
            user,
            owner_object_key='ticket',
            rel_field='contact',
            target_object_key='contact',
            enabled_keys=enabled_keys,
            readable_keys=readable_keys,
            query_text=query_text,
        )

    elif object_key == 'activity':
        for field in ('summary', 'activity_type'):
            if _is_field_visible(user, workspace, 'activity', field):
                search_q |= Q(**{f'{field}__icontains': query_text})
        if _is_field_visible(user, workspace, 'activity', 'assignee'):
            search_q |= Q(assignee__email__icontains=query_text)

    return qs.filter(search_q) if search_q else qs.none()


def _visible_account_name(record, owner_key: str, *, user, workspace, readable_keys: set[str]) -> str:
    if (
        'account' not in readable_keys
        or not _is_field_visible(user, workspace, owner_key, 'account')
        or not getattr(record, 'account_id', None)
        or not getattr(record, 'account', None)
        or not user_can_see_record(user, workspace, record.account)
    ):
        return ''
    return _get_visible_record_label(record.account, 'account', user=user, workspace=workspace)


def _visible_contact_name(record, owner_key: str, *, user, workspace, readable_keys: set[str]) -> str:
    if (
        'contact' not in readable_keys
        or not _is_field_visible(user, workspace, owner_key, 'contact')
        or not getattr(record, 'contact_id', None)
        or not getattr(record, 'contact', None)
        or not user_can_see_record(user, workspace, record.contact)
    ):
        return ''
    return _get_visible_record_label(record.contact, 'contact', user=user, workspace=workspace)


def _batch_activity_parent_labels(
    workspace,
    user,
    activities: list[Activity],
) -> dict[tuple[str, int], str]:
    ids_by_type: dict[str, set[int]] = {}
    for act in activities:
        if act.target_object_type in CRM_MODELS_BY_KEY:
            ids_by_type.setdefault(act.target_object_type, set()).add(act.target_object_id)

    labels: dict[tuple[str, int], str] = {}
    vis_q = Q() if is_workspace_owner(user, workspace) else build_record_visibility_q(user, workspace)
    for target_type, ids in ids_by_type.items():
        model_cls = CRM_MODELS_BY_KEY[target_type]
        for row in model_cls.objects.filter(workspace=workspace, id__in=ids).filter(vis_q):
            labels[(target_type, row.id)] = _get_visible_record_label(
                row,
                target_type,
                user=user,
                workspace=workspace,
            )
    return labels


def _serialize_search_hit(
    record,
    object_key: str,
    *,
    workspace,
    user,
    readable_keys: set[str],
    activity_parent_labels: dict[tuple[str, int], str],
) -> dict:
    spec = SEARCH_SPEC_BY_KEY[object_key]
    obj_label, _ = _object_labels_for_workspace(workspace, spec)
    record_code = f'{spec.code_prefix}-{record.id}'
    title = _get_visible_record_label(record, object_key, user=user, workspace=workspace)
    subtitle_parts: list[str] = []
    badge = ''
    badge_tone = 'neutral'
    inspect_object_type = object_key
    inspect_pane_id = spec.pane_id
    inspect_record_id = record.id

    if object_key == 'account':
        if _is_field_visible(user, workspace, 'account', 'city') and record.city:
            subtitle_parts.append(record.city)
        if _is_field_visible(user, workspace, 'account', 'email') and record.email:
            subtitle_parts.append(record.email)
        elif _is_field_visible(user, workspace, 'account', 'phone') and record.phone:
            subtitle_parts.append(record.phone)

    elif object_key == 'contact':
        if _is_field_visible(user, workspace, 'contact', 'title') and record.title:
            subtitle_parts.append(record.title)
        acc_name = _visible_account_name(
            record, 'contact', user=user, workspace=workspace, readable_keys=readable_keys
        )
        if acc_name:
            subtitle_parts.append(acc_name)
        if _is_field_visible(user, workspace, 'contact', 'email') and record.email:
            subtitle_parts.append(record.email)

    elif object_key == 'lead':
        if _is_field_visible(user, workspace, 'lead', 'company') and record.company:
            subtitle_parts.append(record.company)
        if _is_field_visible(user, workspace, 'lead', 'email') and record.email:
            subtitle_parts.append(record.email)
        if _is_field_visible(user, workspace, 'lead', 'status') and record.status:
            badge = 'Converted' if record.status == Lead.STATUS_CONVERTED else 'Open'
            badge_tone = 'info' if record.status == Lead.STATUS_CONVERTED else 'success'

    elif object_key == 'deal':
        acc_name = _visible_account_name(
            record, 'deal', user=user, workspace=workspace, readable_keys=readable_keys
        )
        if acc_name:
            subtitle_parts.append(acc_name)
        if _is_field_visible(user, workspace, 'deal', 'amount'):
            subtitle_parts.append(f'GHS {record.amount:,.2f}')
        if _is_field_visible(user, workspace, 'deal', 'stage') and record.stage:
            badge = record.stage
            badge_tone = _resolve_deal_stage_tone(record)

    elif object_key == 'job':
        acc_name = _visible_account_name(
            record, 'job', user=user, workspace=workspace, readable_keys=readable_keys
        )
        con_name = _visible_contact_name(
            record, 'job', user=user, workspace=workspace, readable_keys=readable_keys
        )
        if acc_name:
            subtitle_parts.append(acc_name)
        elif con_name:
            subtitle_parts.append(con_name)
        if _is_field_visible(user, workspace, 'job', 'due_date') and record.due_date:
            subtitle_parts.append(f'Due {record.due_date.isoformat()}')
        if _is_field_visible(user, workspace, 'job', 'status') and record.status:
            badge = record.status
            badge_tone = (
                'success'
                if record.status == 'Completed'
                else 'danger'
                if record.status == 'Cancelled'
                else 'info'
                if record.status == 'In Progress'
                else 'warn'
            )

    elif object_key == 'appointment':
        con_name = _visible_contact_name(
            record, 'appointment', user=user, workspace=workspace, readable_keys=readable_keys
        )
        if con_name:
            subtitle_parts.append(con_name)
        if _is_field_visible(user, workspace, 'appointment', 'scheduled_at') and record.scheduled_at:
            subtitle_parts.append(record.scheduled_at.strftime('%Y-%m-%d %H:%M UTC'))
        if _is_field_visible(user, workspace, 'appointment', 'status') and record.status:
            badge = record.status
            badge_tone = (
                'success'
                if record.status == 'Completed'
                else 'danger'
                if record.status in ('Cancelled', 'No-show')
                else 'info'
            )

    elif object_key == 'visit':
        con_name = _visible_contact_name(
            record, 'visit', user=user, workspace=workspace, readable_keys=readable_keys
        )
        if con_name:
            subtitle_parts.append(con_name)
        if _is_field_visible(user, workspace, 'visit', 'date') and record.date:
            subtitle_parts.append(record.date.strftime('%Y-%m-%d %H:%M UTC'))
        if _is_field_visible(user, workspace, 'visit', 'follow_up') and record.follow_up:
            badge = 'Follow-up'
            badge_tone = 'info'

    elif object_key == 'ticket':
        acc_name = _visible_account_name(
            record, 'ticket', user=user, workspace=workspace, readable_keys=readable_keys
        )
        con_name = _visible_contact_name(
            record, 'ticket', user=user, workspace=workspace, readable_keys=readable_keys
        )
        if acc_name:
            subtitle_parts.append(acc_name)
        elif con_name:
            subtitle_parts.append(con_name)
        if _is_field_visible(user, workspace, 'ticket', 'priority') and record.priority:
            subtitle_parts.append(f'{record.priority} priority')
        if _is_field_visible(user, workspace, 'ticket', 'status') and record.status:
            badge = record.status
            badge_tone = (
                'success'
                if record.status in ('Resolved', 'Closed')
                else 'warn'
                if record.status == 'Pending'
                else 'info'
            )

    elif object_key == 'activity':
        parent_key = record.target_object_type
        parent_spec = SEARCH_SPEC_BY_KEY.get(parent_key)
        inspect_object_type = parent_key
        inspect_pane_id = parent_spec.pane_id if parent_spec else None
        inspect_record_id = record.target_object_id
        parent_label = activity_parent_labels.get((parent_key, record.target_object_id))
        if parent_spec:
            parent_obj_label, _ = _object_labels_for_workspace(workspace, parent_spec)
            subtitle_parts.append(
                f'On {parent_obj_label}: {parent_label}'
                if parent_label
                else f'On {parent_obj_label} #{record.target_object_id}'
            )
        if _is_field_visible(user, workspace, 'activity', 'activity_type') and record.activity_type:
            badge = record.activity_type.capitalize()
            badge_tone = 'info' if record.activity_type == Activity.TYPE_TASK else 'neutral'

    return {
        'object_type': object_key,
        'object_label': obj_label,
        'id': record.id,
        'record_code': record_code,
        'title': title,
        'subtitle': ' · '.join(part for part in subtitle_parts if part),
        'badge': badge,
        'badge_tone': badge_tone,
        'updated_at': record.updated_at.isoformat(),
        'inspect_object_type': inspect_object_type,
        'inspect_pane_id': inspect_pane_id,
        'inspect_record_id': inspect_record_id,
    }


def search_workspace_records(
    workspace,
    user,
    *,
    query: str = '',
    object_type: str = '',
    page: int = 1,
    page_size: int = DEFAULT_SEARCH_PAGE_SIZE,
) -> dict:
    """Search across all enabled, caller-readable CRM objects in workspace with SQL pagination."""
    ensure_workspace_catalog(workspace)
    enabled_keys = set(
        WorkspaceTable.objects.filter(
            workspace=workspace,
            is_enabled=True,
        ).values_list('object_key', flat=True)
    )

    norm_object_type = (object_type or '').strip().lower()
    if norm_object_type:
        canonical = OBJECT_KEY_ALIASES.get(norm_object_type, norm_object_type)
        if canonical not in SEARCH_SPEC_BY_KEY:
            raise CrmDomainError('Unknown search object type.', status_code=400)
        spec = SEARCH_SPEC_BY_KEY[canonical]
        label, _ = _object_labels_for_workspace(workspace, spec)
        if canonical not in enabled_keys:
            raise CrmDomainError(f'{label} is not enabled in this workspace.', status_code=404)
        if not user_has_object_right(user, workspace, canonical, 'read'):
            raise CrmDomainError(
                f'Your role does not have read permission for {label}.',
                status_code=403,
            )
        norm_object_type = canonical

    readable_specs: list[SearchObjectSpec] = []
    readable_keys: set[str] = set()
    for spec in SEARCH_OBJECT_REGISTRY:
        if spec.key in enabled_keys and user_has_object_right(user, workspace, spec.key, 'read'):
            readable_specs.append(spec)
            readable_keys.add(spec.key)

    query_text = (query or '').strip()
    code_match = _parse_record_code_match(query_text) if query_text else None

    querysets_by_key = {}
    counts_by_object: dict[str, int] = {}
    for spec in readable_specs:
        obj_qs = _build_object_search_queryset(
            workspace,
            user,
            spec.key,
            query_text=query_text,
            code_match=code_match,
            enabled_keys=enabled_keys,
            readable_keys=readable_keys,
        )
        querysets_by_key[spec.key] = obj_qs
        counts_by_object[spec.key] = obj_qs.count()

    searchable_objects = []
    for spec in readable_specs:
        singular, plural = _object_labels_for_workspace(workspace, spec)
        searchable_objects.append(
            {
                'key': spec.key,
                'label': singular,
                'plural_label': plural,
                'pane_id': spec.pane_id,
                'count': counts_by_object[spec.key],
            }
        )

    offset = (page - 1) * page_size
    ordered_hits: list[tuple[str, object]] = []

    if norm_object_type:
        total_count = counts_by_object.get(norm_object_type, 0)
        if total_count > 0:
            rows = list(
                querysets_by_key[norm_object_type]
                .order_by('-updated_at', '-id')[offset : offset + page_size]
            )
            ordered_hits = [(norm_object_type, row) for row in rows]
    else:
        total_count = sum(counts_by_object.values())
        non_empty_parts = [
            querysets_by_key[spec.key]
            .order_by()
            .annotate(_search_obj=Value(spec.key, output_field=CharField()))
            .values('_search_obj', 'id', 'updated_at')
            for spec in readable_specs
            if counts_by_object[spec.key] > 0
        ]
        if non_empty_parts:
            combined_qs = (
                non_empty_parts[0]
                if len(non_empty_parts) == 1
                else non_empty_parts[0].union(*non_empty_parts[1:], all=True)
            )
            page_refs = list(combined_qs.order_by('-updated_at', '-id')[offset : offset + page_size])
            ids_by_obj: dict[str, list[int]] = {}
            for ref in page_refs:
                ids_by_obj.setdefault(ref['_search_obj'], []).append(ref['id'])

            hydrated: dict[tuple[str, int], object] = {}
            for obj_key, ids in ids_by_obj.items():
                for row in _build_base_queryset(workspace, obj_key, user=user).filter(id__in=ids):
                    hydrated[(obj_key, row.id)] = row

            for ref in page_refs:
                key_pair = (ref['_search_obj'], ref['id'])
                if key_pair in hydrated:
                    ordered_hits.append((ref['_search_obj'], hydrated[key_pair]))

    activity_rows = [row for obj_key, row in ordered_hits if obj_key == 'activity']
    activity_parent_labels = (
        _batch_activity_parent_labels(workspace, user, activity_rows)
        if activity_rows
        else {}
    )

    results = [
        _serialize_search_hit(
            row,
            obj_key,
            workspace=workspace,
            user=user,
            readable_keys=readable_keys,
            activity_parent_labels=activity_parent_labels,
        )
        for obj_key, row in ordered_hits
    ]

    total_pages = max(1, math.ceil(total_count / page_size)) if total_count else 1
    return {
        'query': query_text,
        'object_type': norm_object_type,
        'results': results,
        'searchable_objects': searchable_objects,
        'counts_by_object': counts_by_object,
        'total_count': total_count,
        'all_objects_total_count': sum(counts_by_object.values()),
        'page': page,
        'page_size': page_size,
        'total_pages': total_pages,
        'has_next': page < total_pages,
        'has_previous': page > 1,
    }
