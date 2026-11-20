from decimal import Decimal
import re

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .audit_service import record_audit_event
from .crm_service import (
    RECORD_CODE_PREFIXES,
    _build_base_queryset,
    _first_present,
    _get_contact_screen_label,
    _get_visible_record_label,
    _has_any_key,
    _is_enabled_and_readable,
    _is_field_visible,
    _parse_decimal,
    _parse_fk_id,
    _parse_int_range,
    _parse_optional_date,
    _parse_optional_str,
    _parse_required_str,
    ensure_object_permission,
    ensure_object_table_enabled,
    get_lead_conversion_capability,
    get_record_label,
    serialize_crm_record,
)
from .pipeline_service import (
    get_ordered_pipeline_stages,
    resolve_deal_stage,
    serialize_pipeline_stage,
)
from .role_service import (
    FIELD_RIGHT_EDITABLE,
    FIELD_RIGHT_HIDDEN,
    OBJECT_FIELD_SCHEMAS,
    build_record_visibility_q,
    get_user_field_rights,
    is_workspace_owner,
    user_can_see_record,
)
from ..helpers.crm_errors import CrmDomainError
from ..catalog import OBJECTS_BY_KEY
from ..models import (
    Account,
    Activity,
    AuditLog,
    Contact,
    Deal,
    Lead,
    WorkspaceTable,
)

MAX_MATCH_RESULTS = 15
_PHONE_PUNCTUATION_RE = re.compile(r'[\s\-\(\)\.]+')


def _normalize_text(value: str | None) -> str:
    return ' '.join((value or '').strip().lower().split())


def _normalize_phone(value: str | None) -> str:
    return _PHONE_PUNCTUATION_RE.sub('', (value or '').strip()).lower()


def _ensure_conversion_tables_enabled(workspace) -> None:
    """Require Lead, Deal, Account, and Contact tables to be enabled for conversion."""
    ensure_object_table_enabled(workspace, 'lead')
    if not WorkspaceTable.objects.filter(
        workspace=workspace,
        object_key='deal',
        is_enabled=True,
    ).exists():
        raise CrmDomainError(
            'Lead-to-deal conversion is not available in this workspace because the Deal table is not enabled.',
            status_code=404,
        )
    ensure_object_table_enabled(workspace, 'account')
    ensure_object_table_enabled(workspace, 'contact')


def _ensure_conversion_writable_overrides(
    user,
    workspace,
    object_key: str,
    payload: dict,
    field_key_map: dict[str, tuple[str, ...]],
) -> None:
    """Reject explicit lead-conversion overrides for fields marked hidden or read-only."""
    if is_workspace_owner(user, workspace):
        return
    field_rights = get_user_field_rights(user, workspace, object_key)
    schema_by_key = {s.key: s for s in OBJECT_FIELD_SCHEMAS.get(object_key, ())}
    contact_label = _get_contact_screen_label(workspace)
    for field_key, payload_keys in field_key_map.items():
        level = field_rights.get(field_key, FIELD_RIGHT_EDITABLE)
        if level == FIELD_RIGHT_EDITABLE:
            continue
        if any(k in payload for k in payload_keys):
            schema = schema_by_key.get(field_key)
            if schema is not None:
                label = contact_label if schema.uses_contact_label else schema.label
            else:
                label = field_key.replace('_', ' ').title()
            level_label = 'hidden' if level == FIELD_RIGHT_HIDDEN else 'read-only'
            raise CrmDomainError(
                f'Your role cannot modify {label} because this field is {level_label}.',
                status_code=403,
            )


def _find_account_matches(workspace, user, lead: Lead) -> list[dict]:
    if not _is_enabled_and_readable(user, workspace, 'account'):
        return []

    can_match_company = (
        _is_field_visible(user, workspace, 'lead', 'company')
        and _is_field_visible(user, workspace, 'account', 'name')
    )
    can_match_email = (
        _is_field_visible(user, workspace, 'lead', 'email')
        and _is_field_visible(user, workspace, 'account', 'email')
    )
    can_match_phone = (
        _is_field_visible(user, workspace, 'lead', 'phone')
        and _is_field_visible(user, workspace, 'account', 'phone')
    )

    company_norm = _normalize_text(lead.company) if can_match_company else ''
    email_norm = _normalize_text(lead.email) if can_match_email else ''
    phone_norm = _normalize_phone(lead.phone) if can_match_phone else ''
    if not company_norm and not email_norm and not phone_norm:
        return []

    query = Q()
    if company_norm:
        query |= Q(name__iexact=lead.company.strip())
    if email_norm:
        query |= Q(email__iexact=email_norm)

    vis_q = build_record_visibility_q(user, workspace)
    phone_ids: list[int] = []
    if phone_norm:
        for acc_id, acc_phone in (
            Account.objects.filter(workspace=workspace)
            .filter(vis_q)
            .exclude(phone='')
            .values_list('id', 'phone')
        ):
            if _normalize_phone(acc_phone) == phone_norm:
                phone_ids.append(acc_id)
        if phone_ids:
            query |= Q(id__in=phone_ids)

    if not query:
        return []

    city_visible = _is_field_visible(user, workspace, 'account', 'city')
    email_visible = _is_field_visible(user, workspace, 'account', 'email')
    phone_visible = _is_field_visible(user, workspace, 'account', 'phone')

    matches: list[dict] = []
    for account in (
        Account.objects.filter(workspace=workspace)
        .filter(vis_q)
        .filter(query)
        .order_by('-updated_at', 'id')
    ):
        reasons: list[str] = []
        if company_norm and _normalize_text(account.name) == company_norm:
            reasons.append('Company name')
        if email_norm and _normalize_text(account.email) == email_norm:
            reasons.append('Email')
        if phone_norm and _normalize_phone(account.phone) == phone_norm:
            reasons.append('Phone')
        if not reasons:
            continue
        item = {
            'id': account.id,
            'record_code': f'{RECORD_CODE_PREFIXES["account"]}-{account.id}',
            'name': _get_visible_record_label(account, 'account', user=user, workspace=workspace),
            'match_reasons': reasons,
        }
        if city_visible:
            item['city'] = account.city
        if email_visible:
            item['email'] = account.email
        if phone_visible:
            item['phone'] = account.phone
        matches.append(item)

    matches.sort(key=lambda item: (-len(item['match_reasons']), item['name'].lower(), item['id']))
    return matches[:MAX_MATCH_RESULTS]


def _find_contact_matches(workspace, user, lead: Lead) -> list[dict]:
    if not _is_enabled_and_readable(user, workspace, 'contact'):
        return []

    can_match_name = (
        _is_field_visible(user, workspace, 'lead', 'name')
        and _is_field_visible(user, workspace, 'contact', 'name')
    )
    can_match_email = (
        _is_field_visible(user, workspace, 'lead', 'email')
        and _is_field_visible(user, workspace, 'contact', 'email')
    )
    can_match_phone = (
        _is_field_visible(user, workspace, 'lead', 'phone')
        and _is_field_visible(user, workspace, 'contact', 'phone')
    )

    name_norm = _normalize_text(lead.name) if can_match_name else ''
    email_norm = _normalize_text(lead.email) if can_match_email else ''
    phone_norm = _normalize_phone(lead.phone) if can_match_phone else ''
    if not name_norm and not email_norm and not phone_norm:
        return []

    query = Q()
    if email_norm:
        query |= Q(email__iexact=email_norm)
    if name_norm:
        query |= Q(name__iexact=lead.name.strip())

    vis_q = build_record_visibility_q(user, workspace)
    phone_ids: list[int] = []
    if phone_norm:
        for con_id, con_phone in (
            Contact.objects.filter(workspace=workspace)
            .filter(vis_q)
            .exclude(phone='')
            .values_list('id', 'phone')
        ):
            if _normalize_phone(con_phone) == phone_norm:
                phone_ids.append(con_id)
        if phone_ids:
            query |= Q(id__in=phone_ids)

    if not query:
        return []

    title_visible = _is_field_visible(user, workspace, 'contact', 'title')
    email_visible = _is_field_visible(user, workspace, 'contact', 'email')
    phone_visible = _is_field_visible(user, workspace, 'contact', 'phone')
    contact_account_visible = _is_field_visible(user, workspace, 'contact', 'account')
    can_read_account = _is_enabled_and_readable(user, workspace, 'account')

    matches: list[dict] = []
    contacts = (
        Contact.objects.select_related('account')
        .filter(workspace=workspace)
        .filter(vis_q)
        .filter(query)
        .order_by('-updated_at', 'id')
    )
    for contact in contacts:
        reasons: list[str] = []
        if email_norm and _normalize_text(contact.email) == email_norm:
            reasons.append('Email')
        if phone_norm and _normalize_phone(contact.phone) == phone_norm:
            reasons.append('Phone')
        if name_norm and _normalize_text(contact.name) == name_norm:
            reasons.append('Name')
        if not reasons:
            continue
        item = {
            'id': contact.id,
            'record_code': f'{RECORD_CODE_PREFIXES["contact"]}-{contact.id}',
            'name': _get_visible_record_label(contact, 'contact', user=user, workspace=workspace),
            'match_reasons': reasons,
        }
        if title_visible:
            item['title'] = contact.title
        if email_visible:
            item['email'] = contact.email
        if phone_visible:
            item['phone'] = contact.phone
        if contact_account_visible:
            if (
                contact.account_id
                and contact.account is not None
                and can_read_account
                and user_can_see_record(user, workspace, contact.account)
            ):
                item['account_id'] = contact.account_id
                item['account_name'] = _get_visible_record_label(
                    contact.account, 'account', user=user, workspace=workspace
                )
            else:
                item['account_id'] = None
                item['account_name'] = ''
        matches.append(item)

    matches.sort(
        key=lambda item: (
            -len(item['match_reasons']),
            0 if ('Email' in item['match_reasons'] or 'Phone' in item['match_reasons']) else 1,
            item['name'].lower(),
            item['id'],
        )
    )
    return matches[:MAX_MATCH_RESULTS]


def get_lead_conversion_preview(workspace, user, lead_id: int) -> dict:
    """Return matching existing Accounts/Contacts, defaults, field rights, and permissions for converting a Lead."""
    _ensure_conversion_tables_enabled(workspace)
    ensure_object_permission(user, workspace, 'lead', 'read')

    lead = _build_base_queryset(workspace, 'lead', user=user).filter(id=lead_id).first()
    if lead is None:
        raise CrmDomainError(f'{OBJECTS_BY_KEY["lead"].label} not found.', status_code=404)

    visible_lead_company = (
        (lead.company or '').strip()
        if _is_field_visible(user, workspace, 'lead', 'company')
        else ''
    )
    visible_lead_name = _get_visible_record_label(lead, 'lead', user=user, workspace=workspace)
    default_account_name = visible_lead_company or visible_lead_name
    capability = get_lead_conversion_capability(user, workspace)
    account_field_rights = get_user_field_rights(user, workspace, 'account')
    contact_field_rights = get_user_field_rights(user, workspace, 'contact')
    deal_field_rights = get_user_field_rights(user, workspace, 'deal')
    capability = {
        **capability,
        'can_link_standalone_contact': bool(capability.get('can_update_contact'))
        and (
            is_workspace_owner(user, workspace)
            or contact_field_rights.get('account', FIELD_RIGHT_EDITABLE) == FIELD_RIGHT_EDITABLE
        ),
    }

    stages = get_ordered_pipeline_stages(workspace, require_enabled=True)
    default_stage = resolve_deal_stage(
        workspace,
        None,
        allow_default=True,
        prefer_qualified=True,
    )

    defaults: dict = {}
    if account_field_rights.get('name', FIELD_RIGHT_EDITABLE) != FIELD_RIGHT_HIDDEN:
        defaults['account_name'] = default_account_name
    if account_field_rights.get('city', FIELD_RIGHT_EDITABLE) != FIELD_RIGHT_HIDDEN:
        defaults['account_city'] = ''
    if contact_field_rights.get('name', FIELD_RIGHT_EDITABLE) != FIELD_RIGHT_HIDDEN:
        defaults['contact_name'] = visible_lead_name
    if contact_field_rights.get('title', FIELD_RIGHT_EDITABLE) != FIELD_RIGHT_HIDDEN:
        defaults['contact_title'] = ''
    if deal_field_rights.get('title', FIELD_RIGHT_EDITABLE) != FIELD_RIGHT_HIDDEN:
        defaults['deal_title'] = f'{default_account_name} Deal'
    if deal_field_rights.get('stage', FIELD_RIGHT_EDITABLE) != FIELD_RIGHT_HIDDEN:
        defaults['deal_stage'] = default_stage.name
    if deal_field_rights.get('amount', FIELD_RIGHT_EDITABLE) != FIELD_RIGHT_HIDDEN:
        defaults['deal_amount'] = '0.00'
    if deal_field_rights.get('target_close_date', FIELD_RIGHT_EDITABLE) != FIELD_RIGHT_HIDDEN:
        defaults['target_close_date'] = None
    if deal_field_rights.get('win_probability', FIELD_RIGHT_EDITABLE) != FIELD_RIGHT_HIDDEN:
        defaults['win_probability'] = None

    return {
        'lead': serialize_crm_record(lead, 'lead', user=user, workspace=workspace),
        'already_converted': lead.status == Lead.STATUS_CONVERTED or lead.converted_at is not None,
        'account_matches': _find_account_matches(workspace, user, lead),
        'contact_matches': _find_contact_matches(workspace, user, lead),
        'deal_stages': (
            [serialize_pipeline_stage(stage) for stage in stages]
            if deal_field_rights.get('stage', FIELD_RIGHT_EDITABLE) != FIELD_RIGHT_HIDDEN
            else []
        ),
        'defaults': defaults,
        'capabilities': capability,
        'field_rights': {
            'account': account_field_rights,
            'contact': contact_field_rights,
            'deal': deal_field_rights,
        },
    }


def _parse_optional_override_name(payload: dict, *keys: str, fallback: str, label: str) -> str:
    if _has_any_key(payload, *keys):
        raw = _first_present(payload, *keys)
        if raw is not None:
            return _parse_required_str(payload, *keys, label=label, max_length=255)
    return fallback


def _resolve_conversion_account(
    workspace,
    user,
    lead: Lead,
    payload: dict,
) -> tuple[Account, bool]:
    raw_account_id = _first_present(payload, 'account_id', 'account')
    account_id = _parse_fk_id(raw_account_id, field_name='Account')
    if account_id is not None:
        ensure_object_permission(user, workspace, 'account', 'read')
        account = (
            Account.objects.filter(workspace=workspace, id=account_id)
            .filter(build_record_visibility_q(user, workspace))
            .first()
        )
        if account is None:
            raise CrmDomainError('Selected Account was not found in this workspace.', status_code=400)
        return account, False

    ensure_object_permission(user, workspace, 'account', 'create')
    _ensure_conversion_writable_overrides(
        user,
        workspace,
        'account',
        payload,
        {
            'name': ('account_name', 'company'),
            'city': ('account_city', 'city'),
        },
    )
    fallback_name = (lead.company or '').strip() or lead.name
    account_name = _parse_optional_override_name(
        payload,
        'account_name',
        'company',
        fallback=fallback_name,
        label='Account name',
    )
    account_city = _parse_optional_str(
        payload,
        'account_city',
        'city',
        label='City',
        max_length=120,
    )
    account = Account.objects.create(
        workspace=workspace,
        owner=lead.owner or user,
        created_by=user,
        visibility=lead.visibility,
        shared_role=lead.shared_role,
        name=account_name,
        city=account_city,
        phone=lead.phone,
        email=lead.email,
        notes=lead.notes,
        source_lead=lead,
    )
    return account, True


def _resolve_conversion_contact(
    workspace,
    user,
    lead: Lead,
    account: Account,
    payload: dict,
) -> tuple[Contact, bool, bool]:
    contact_label = _get_contact_screen_label(workspace)
    raw_contact_id = _first_present(payload, 'contact_id', 'contact')
    contact_id = _parse_fk_id(raw_contact_id, field_name=contact_label)

    if contact_id is not None:
        ensure_object_permission(user, workspace, 'contact', 'read')
        contact = (
            Contact.objects.select_related('account')
            .filter(workspace=workspace, id=contact_id)
            .filter(build_record_visibility_q(user, workspace))
            .first()
        )
        if contact is None:
            raise CrmDomainError(
                f'Selected {contact_label} was not found in this workspace.',
                status_code=400,
            )
        if contact.account_id is not None and contact.account_id != account.id:
            existing_acc_label = _get_visible_record_label(
                contact.account, 'account', user=user, workspace=workspace
            )
            target_acc_label = _get_visible_record_label(
                account, 'account', user=user, workspace=workspace
            )
            raise CrmDomainError(
                f'Selected {contact_label} belongs to {existing_acc_label}, not {target_acc_label}.',
                status_code=400,
            )
        linked_existing_contact = False
        if contact.account_id is None:
            ensure_object_permission(user, workspace, 'contact', 'update')
            if not is_workspace_owner(user, workspace):
                acc_field_level = get_user_field_rights(user, workspace, 'contact').get(
                    'account', FIELD_RIGHT_EDITABLE
                )
                if acc_field_level != FIELD_RIGHT_EDITABLE:
                    level_label = 'hidden' if acc_field_level == FIELD_RIGHT_HIDDEN else 'read-only'
                    raise CrmDomainError(
                        f'Your role cannot modify Account because this field is {level_label}.',
                        status_code=403,
                    )
            contact.account = account
            contact.save(update_fields=['account', 'updated_at'])
            linked_existing_contact = True
        return contact, False, linked_existing_contact

    ensure_object_permission(user, workspace, 'contact', 'create')
    _ensure_conversion_writable_overrides(
        user,
        workspace,
        'contact',
        payload,
        {
            'name': ('contact_name',),
            'title': ('contact_title', 'title'),
        },
    )
    contact_name = _parse_optional_override_name(
        payload,
        'contact_name',
        fallback=lead.name,
        label=f'{contact_label} name',
    )
    contact_title = _parse_optional_str(
        payload,
        'contact_title',
        'title',
        label='Title',
        max_length=150,
    )
    contact = Contact.objects.create(
        workspace=workspace,
        owner=lead.owner or user,
        created_by=user,
        visibility=lead.visibility,
        shared_role=lead.shared_role,
        account=account,
        name=contact_name,
        title=contact_title,
        email=lead.email,
        phone=lead.phone,
        sms_consent=bool(lead.sms_consent),
        notes=lead.notes,
        source_lead=lead,
    )
    return contact, True, False


def _record_conversion_notes(
    workspace,
    user,
    *,
    lead: Lead,
    account: Account,
    account_created: bool,
    contact: Contact,
    contact_created: bool,
    deal: Deal,
) -> None:
    if not WorkspaceTable.objects.filter(
        workspace=workspace,
        object_key='activity',
        is_enabled=True,
    ).exists():
        return

    lead_code = f'{RECORD_CODE_PREFIXES["lead"]}-{lead.id}'
    acc_code = f'{RECORD_CODE_PREFIXES["account"]}-{account.id}'
    con_code = f'{RECORD_CODE_PREFIXES["contact"]}-{contact.id}'
    deal_code = f'{RECORD_CODE_PREFIXES["deal"]}-{deal.id}'

    notes = [
        (
            'lead',
            lead.id,
            f'Converted into {acc_code} ({account.name}), {con_code} ({contact.name}), and {deal_code} ({deal.title}).',
        ),
        (
            'account',
            account.id,
            (
                f'Created from {lead_code} ({lead.name}) alongside {con_code} and {deal_code}.'
                if account_created
                else f'Linked from converted lead {lead_code} ({lead.name}) alongside {con_code} and {deal_code}.'
            ),
        ),
        (
            'contact',
            contact.id,
            (
                f'Converted from {lead_code} ({lead.name}) with {acc_code} and {deal_code}.'
                if contact_created
                else f'Linked from converted lead {lead_code} ({lead.name}) with {acc_code} and {deal_code}.'
            ),
        ),
        (
            'deal',
            deal.id,
            f'Converted from {lead_code} ({lead.name}) for {acc_code} and {con_code}.',
        ),
    ]
    for target_type, target_id, summary in notes:
        Activity.objects.create(
            workspace=workspace,
            owner=lead.owner or user,
            created_by=user,
            visibility=lead.visibility,
            shared_role=lead.shared_role,
            activity_type=Activity.TYPE_NOTE,
            target_object_type=target_type,
            target_object_id=target_id,
            summary=summary,
        )


def _record_conversion_audits(
    workspace,
    user,
    *,
    lead: Lead,
    previous_status: str,
    account: Account,
    account_created: bool,
    contact: Contact,
    contact_created: bool,
    linked_existing_contact: bool,
    deal: Deal,
) -> None:
    if account_created:
        record_audit_event(
            workspace=workspace,
            object_type='account',
            object_id=account.id,
            record_label=get_record_label(account, 'account'),
            action=AuditLog.ACTION_CREATE,
            field_name='record',
            previous_value=None,
            new_value=account.name,
            actor_kind=AuditLog.ACTOR_KIND_PERSON,
            actor_user=user,
            record=account,
        )
    if contact_created:
        record_audit_event(
            workspace=workspace,
            object_type='contact',
            object_id=contact.id,
            record_label=get_record_label(contact, 'contact'),
            action=AuditLog.ACTION_CREATE,
            field_name='record',
            previous_value=None,
            new_value=contact.name,
            actor_kind=AuditLog.ACTOR_KIND_PERSON,
            actor_user=user,
            record=contact,
        )
    elif linked_existing_contact:
        record_audit_event(
            workspace=workspace,
            object_type='contact',
            object_id=contact.id,
            record_label=get_record_label(contact, 'contact'),
            action=AuditLog.ACTION_UPDATE,
            field_name='account',
            previous_value=None,
            new_value=account.name,
            actor_kind=AuditLog.ACTOR_KIND_PERSON,
            actor_user=user,
            record=contact,
        )

    record_audit_event(
        workspace=workspace,
        object_type='deal',
        object_id=deal.id,
        record_label=get_record_label(deal, 'deal'),
        action=AuditLog.ACTION_CREATE,
        field_name='record',
        previous_value=None,
        new_value=deal.title,
        actor_kind=AuditLog.ACTOR_KIND_PERSON,
        actor_user=user,
        record=deal,
    )

    lead_label = get_record_label(lead, 'lead')
    record_audit_event(
        workspace=workspace,
        object_type='lead',
        object_id=lead.id,
        record_label=lead_label,
        action=AuditLog.ACTION_UPDATE,
        field_name='status',
        previous_value=previous_status,
        new_value=Lead.STATUS_CONVERTED,
        actor_kind=AuditLog.ACTOR_KIND_PERSON,
        actor_user=user,
        record=lead,
    )
    record_audit_event(
        workspace=workspace,
        object_type='lead',
        object_id=lead.id,
        record_label=lead_label,
        action=AuditLog.ACTION_UPDATE,
        field_name='converted_deal',
        previous_value=None,
        new_value=f'{RECORD_CODE_PREFIXES["deal"]}-{deal.id} ({deal.title})',
        actor_kind=AuditLog.ACTOR_KIND_PERSON,
        actor_user=user,
        record=lead,
    )


def convert_lead_record(
    workspace,
    user,
    lead_id: int,
    payload: dict | None = None,
) -> dict:
    """Convert an open Lead into an Account, Contact, and Deal inside one atomic transaction."""
    _ensure_conversion_tables_enabled(workspace)
    ensure_object_permission(user, workspace, 'lead', 'update')
    ensure_object_permission(user, workspace, 'deal', 'create')

    data = payload if isinstance(payload, dict) else {}

    with transaction.atomic():
        lead = (
            Lead.objects.select_for_update(of=('self',))
            .filter(workspace=workspace, id=lead_id)
            .first()
        )
        if lead is None or not user_can_see_record(user, workspace, lead):
            raise CrmDomainError(f'{OBJECTS_BY_KEY["lead"].label} not found.', status_code=404)

        if lead.status == Lead.STATUS_CONVERTED or lead.converted_at is not None:
            visible_lead_label = _get_visible_record_label(
                lead, 'lead', user=user, workspace=workspace
            )
            raise CrmDomainError(
                f'Lead {visible_lead_label} has already been converted and cannot be converted again.',
                status_code=409,
            )

        account, account_created = _resolve_conversion_account(workspace, user, lead, data)
        contact, contact_created, linked_existing_contact = _resolve_conversion_contact(
            workspace,
            user,
            lead,
            account,
            data,
        )

        _ensure_conversion_writable_overrides(
            user,
            workspace,
            'deal',
            data,
            {
                'title': ('deal_title', 'title'),
                'stage': ('deal_stage', 'stage', 'deal_stage_id', 'stage_id', 'pipeline_stage_id'),
                'amount': ('deal_amount', 'amount'),
                'target_close_date': ('target_close_date',),
                'win_probability': ('win_probability',),
            },
        )

        deal_title = _parse_optional_override_name(
            data,
            'deal_title',
            'title',
            fallback=f'{account.name} Deal',
            label='Deal title',
        )
        stage_obj = resolve_deal_stage(
            workspace,
            _first_present(data, 'deal_stage', 'stage'),
            stage_id=_first_present(data, 'deal_stage_id', 'stage_id', 'pipeline_stage_id'),
            allow_default=True,
            prefer_qualified=True,
        )
        deal_amount = _parse_decimal(
            _first_present(data, 'deal_amount', 'amount'),
            field_name='Amount',
            allow_null=False,
            default=Decimal('0.00'),
        )
        target_close_date = _parse_optional_date(
            data.get('target_close_date'),
            field_name='Target close date',
        )
        win_probability = _parse_int_range(
            data.get('win_probability'),
            field_name='Win probability',
            min_val=0,
            max_val=100,
            allow_null=True,
        )

        deal = Deal.objects.create(
            workspace=workspace,
            owner=lead.owner or user,
            created_by=user,
            visibility=lead.visibility,
            shared_role=lead.shared_role,
            pipeline=stage_obj.pipeline,
            pipeline_stage=stage_obj,
            title=deal_title,
            account=account,
            contact=contact,
            amount=deal_amount,
            stage=stage_obj.name,
            target_close_date=target_close_date,
            win_probability=win_probability,
            notes=lead.notes,
            source_lead=lead,
        )

        previous_status = lead.status
        lead.status = Lead.STATUS_CONVERTED
        lead.converted_at = timezone.now()
        lead.converted_by = user
        lead.converted_account = account
        lead.converted_contact = contact
        lead.converted_deal = deal
        lead.save(
            update_fields=[
                'status',
                'converted_at',
                'converted_by',
                'converted_account',
                'converted_contact',
                'converted_deal',
                'updated_at',
            ]
        )

        _record_conversion_notes(
            workspace,
            user,
            lead=lead,
            account=account,
            account_created=account_created,
            contact=contact,
            contact_created=contact_created,
            deal=deal,
        )
        _record_conversion_audits(
            workspace,
            user,
            lead=lead,
            previous_status=previous_status,
            account=account,
            account_created=account_created,
            contact=contact,
            contact_created=contact_created,
            linked_existing_contact=linked_existing_contact,
            deal=deal,
        )

    fresh_lead = _build_base_queryset(workspace, 'lead').get(pk=lead.pk)
    fresh_account = _build_base_queryset(workspace, 'account').get(pk=account.pk)
    fresh_contact = _build_base_queryset(workspace, 'contact').get(pk=contact.pk)
    fresh_deal = _build_base_queryset(workspace, 'deal').get(pk=deal.pk)

    return {
        'lead': serialize_crm_record(
            fresh_lead,
            'lead',
            include_detail=True,
            user=user,
            workspace=workspace,
        ),
        'account': serialize_crm_record(
            fresh_account,
            'account',
            include_detail=True,
            user=user,
            workspace=workspace,
        ),
        'contact': serialize_crm_record(
            fresh_contact,
            'contact',
            include_detail=True,
            user=user,
            workspace=workspace,
        ),
        'deal': serialize_crm_record(
            fresh_deal,
            'deal',
            include_detail=True,
            user=user,
            workspace=workspace,
        ),
        'created': {
            'account': account_created,
            'contact': contact_created,
            'deal': True,
        },
        'message': (
            f'Converted {_get_visible_record_label(lead, "lead", user=user, workspace=workspace)} into '
            f'{_get_visible_record_label(account, "account", user=user, workspace=workspace)}, '
            f'{_get_visible_record_label(contact, "contact", user=user, workspace=workspace)}, and '
            f'{_get_visible_record_label(deal, "deal", user=user, workspace=workspace)}.'
        ),
    }
