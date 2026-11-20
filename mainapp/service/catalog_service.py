from collections.abc import Callable

from django.apps import apps
from django.db import transaction
from django.db.models import Count, IntegerField, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce

from .audit_service import record_audit_event
from .cache_service import (
    DOMAIN_CATALOG,
    DOMAIN_RECORDS,
    compute_etag,
    get_or_set_workspace_cache_bundle,
    serialize_json_bytes,
)
from ..catalog import (
    ALL_OBJECTS,
    BUSINESS_TYPES,
    BUSINESS_TYPES_BY_KEY,
    CATALOG_OBJECTS,
    CONTACT_LABEL_PRESETS,
    CONTACT_PLURAL_OVERRIDES,
    CORE_OBJECT_KEYS,
    CORE_OBJECTS,
    OBJECTS_BY_KEY,
    BusinessTypeDefinition,
    ObjectDefinition,
)
from ..models import AuditLog, WorkspacePack, WorkspaceTable


class CatalogError(Exception):
    """Domain error raised for invalid business type, pack, or table operations."""

    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# Registry of optional record-count resolvers per starter object key.
# S2.1+ models register here (or are looked up via OBJECT_MODEL_NAMES).
OBJECT_RECORD_COUNT_RESOLVERS: dict[str, Callable[[object], int]] = {}
OBJECT_MODEL_NAMES: dict[str, str] = {
    'account': 'Account',
    'contact': 'Contact',
    'activity': 'Activity',
    'lead': 'Lead',
    'deal': 'Deal',
    'job': 'Job',
    'appointment': 'Appointment',
    'visit': 'Visit',
    'ticket': 'Ticket',
}


def register_record_count_resolver(
    object_key: str,
    resolver: Callable[[object], int],
) -> None:
    """Register a callable(workspace) -> int returning stored rows for an object key."""
    OBJECT_RECORD_COUNT_RESOLVERS[object_key] = resolver


def unregister_record_count_resolver(object_key: str) -> None:
    """Remove a registered record-count resolver for an object key if present."""
    OBJECT_RECORD_COUNT_RESOLVERS.pop(object_key, None)


def get_table_record_count(workspace, object_key: str) -> int:
    """Return the count of stored records for one object in the given workspace."""
    if workspace is None or object_key not in OBJECTS_BY_KEY:
        return 0

    resolver = OBJECT_RECORD_COUNT_RESOLVERS.get(object_key)
    if resolver is not None:
        return max(0, int(resolver(workspace)))

    model_name = OBJECT_MODEL_NAMES.get(object_key)
    if model_name:
        try:
            model_cls = apps.get_model('mainapp', model_name)
        except LookupError:
            model_cls = None
        if model_cls is not None:
            return model_cls.objects.filter(workspace=workspace).count()

    return 0


def _compute_db_table_record_counts(workspace) -> dict[str, int]:
    """Compute stored row counts across all DB-backed starter object keys in one batched query."""
    all_keys = tuple(OBJECTS_BY_KEY.keys())
    counts: dict[str, int] = {key: 0 for key in all_keys}
    if workspace is None:
        return counts

    annotations = {}
    for key in all_keys:
        model_name = OBJECT_MODEL_NAMES.get(key)
        if not model_name:
            continue
        try:
            model_cls = apps.get_model('mainapp', model_name)
        except LookupError:
            model_cls = None
        if model_cls is None:
            continue
        subq = (
            model_cls.objects.filter(workspace_id=OuterRef('pk'))
            .order_by()
            .values('workspace_id')
            .annotate(cnt=Count('pk'))
            .values('cnt')[:1]
        )
        annotations[f'cnt_{key}'] = Coalesce(
            Subquery(subq, output_field=IntegerField()),
            Value(0),
        )

    if annotations:
        from ..models import Workspace

        row = Workspace.objects.filter(pk=workspace.pk).values(**annotations).first() or {}
        for ann_key, val in row.items():
            counts[ann_key[4:]] = max(0, int(val or 0))

    return counts


def get_workspace_table_record_counts(
    workspace,
    object_keys: list[str] | tuple[str, ...] | None = None,
) -> dict[str, int]:
    """Return a map of object_key -> stored record count for a workspace (cached per workspace)."""
    keys = tuple(object_keys) if object_keys is not None else tuple(OBJECTS_BY_KEY.keys())
    if workspace is None:
        return {key: 0 for key in keys}

    bundle = get_or_set_workspace_cache_bundle(
        workspace.id,
        domains=(DOMAIN_RECORDS,),
        key_suffix='table_counts',
        scope='table_counts',
        loader=lambda: _compute_db_table_record_counts(workspace),
    )
    all_counts = dict(bundle['payload'])
    for key, resolver in OBJECT_RECORD_COUNT_RESOLVERS.items():
        if key in OBJECTS_BY_KEY:
            all_counts[key] = max(0, int(resolver(workspace)))

    return {key: all_counts.get(key, 0) for key in keys}


def pluralize_contact_label(label: str) -> str:
    """Return a clean plural screen label for a workspace's Contact label."""
    cleaned = ' '.join((label or '').strip().split())
    if not cleaned:
        return 'Contacts'

    override = CONTACT_PLURAL_OVERRIDES.get(cleaned.lower())
    if override is not None:
        return override

    lower = cleaned.lower()
    if lower.endswith(('s', 'x', 'z', 'ch', 'sh')):
        return f'{cleaned}es'
    if lower.endswith('y') and len(cleaned) > 1 and lower[-2] not in 'aeiou':
        return f'{cleaned[:-1]}ies'
    return f'{cleaned}s'


def resolve_business_type(raw_key: object) -> BusinessTypeDefinition:
    """Validate and return the BusinessTypeDefinition for a requested business_type key."""
    if not isinstance(raw_key, str) or not raw_key.strip():
        raise CatalogError('Select one of the 20 business types.', status_code=400)
    normalized = raw_key.strip().lower()
    definition = BUSINESS_TYPES_BY_KEY.get(normalized)
    if definition is None:
        raise CatalogError('Select a valid business type from the catalog.', status_code=400)
    return definition


def _object_payload(obj: ObjectDefinition, *, contact_label: str = 'Contact') -> dict:
    label = contact_label if obj.key == 'contact' and contact_label else obj.label
    plural_label = (
        pluralize_contact_label(label)
        if obj.key == 'contact'
        else obj.plural_label
    )
    return {
        'key': obj.key,
        'label': label,
        'plural_label': plural_label,
        'description': obj.description,
        'is_core': obj.is_core,
        'nav_group': obj.nav_group,
        'pane_id': obj.pane_id,
    }


def serialize_business_type_definition(bt: BusinessTypeDefinition) -> dict:
    """Serialize one business type, its 12 catalog pack objects, and its starter roles."""
    return {
        'key': bt.key,
        'label': bt.label,
        'description': bt.description,
        'default_contact_label': bt.default_contact_label,
        'default_contact_plural_label': pluralize_contact_label(bt.default_contact_label),
        'pack_object_keys': list(bt.pack_objects),
        'pack_objects': [
            _object_payload(OBJECTS_BY_KEY[obj_key]) for obj_key in bt.pack_objects
        ],
        'pack_table_count': len(bt.pack_objects),
        'total_starter_tables': len(CORE_OBJECTS) + len(bt.pack_objects),
        'starter_roles': [
            {
                'name': role_def.name,
                'description': role_def.description,
            }
            for role_def in (getattr(bt, 'starter_roles', ()) or ())
        ],
    }


def serialize_business_type_catalog() -> dict:
    """Serialize the complete public starter catalog (5 core, 25 catalog, 20 business types)."""
    return {
        'core_objects': [_object_payload(obj) for obj in CORE_OBJECTS],
        'catalog_objects': [_object_payload(obj) for obj in CATALOG_OBJECTS],
        'contact_label_presets': list(CONTACT_LABEL_PRESETS),
        'business_types': [
            serialize_business_type_definition(bt) for bt in BUSINESS_TYPES
        ],
    }


def initialize_workspace_catalog(
    workspace,
    business_type_key: str,
    *,
    actor_user=None,
) -> BusinessTypeDefinition:
    """Create the primary WorkspacePack, 17 starter WorkspaceTable rows, and starter roles for a new workspace."""
    from .role_service import seed_business_type_starter_roles

    bt = resolve_business_type(business_type_key)
    with transaction.atomic():
        WorkspacePack.objects.get_or_create(
            workspace=workspace,
            pack_key=bt.key,
            defaults={
                'is_primary': True,
                'enabled_by': actor_user,
            },
        )
        for core_key in CORE_OBJECT_KEYS:
            WorkspaceTable.objects.update_or_create(
                workspace=workspace,
                object_key=core_key,
                defaults={
                    'is_core': True,
                    'is_enabled': True,
                    'source_pack_key': '',
                },
            )
        for obj_key in bt.pack_objects:
            WorkspaceTable.objects.update_or_create(
                workspace=workspace,
                object_key=obj_key,
                defaults={
                    'is_core': False,
                    'is_enabled': True,
                    'source_pack_key': bt.key,
                },
            )
        seed_business_type_starter_roles(workspace, bt, actor_user=actor_user)
    return bt


def ensure_workspace_catalog(workspace) -> None:
    """Idempotently ensure a workspace has its primary pack and 5 core + starter pack tables."""
    if workspace is None:
        return

    bt = BUSINESS_TYPES_BY_KEY.get(workspace.business_type) or BUSINESS_TYPES_BY_KEY['general_services']
    updates = []
    if workspace.business_type != bt.key:
        workspace.business_type = bt.key
        updates.append('business_type')
    if not (workspace.business_type_label or '').strip():
        workspace.business_type_label = bt.label
        updates.append('business_type_label')
    if not (workspace.contact_label or '').strip():
        workspace.contact_label = bt.default_contact_label
        updates.append('contact_label')
    if updates:
        workspace.save(update_fields=updates)

    has_packs = WorkspacePack.objects.filter(workspace=workspace).exists()
    existing_table_keys = set(
        WorkspaceTable.objects.filter(workspace=workspace).values_list('object_key', flat=True)
    )

    if not has_packs:
        WorkspacePack.objects.create(
            workspace=workspace,
            pack_key=bt.key,
            is_primary=True,
            enabled_by=workspace.created_by,
        )

    for core_key in CORE_OBJECT_KEYS:
        if core_key not in existing_table_keys:
            WorkspaceTable.objects.get_or_create(
                workspace=workspace,
                object_key=core_key,
                defaults={
                    'is_core': True,
                    'is_enabled': True,
                    'source_pack_key': '',
                },
            )

    if not has_packs:
        for obj_key in bt.pack_objects:
            if obj_key not in existing_table_keys:
                WorkspaceTable.objects.get_or_create(
                    workspace=workspace,
                    object_key=obj_key,
                    defaults={
                        'is_core': False,
                        'is_enabled': True,
                        'source_pack_key': bt.key,
                    },
                )


def enable_pack_for_workspace(workspace, pack_key: str, *, actor_user) -> dict:
    """Turn on an additional pack on a workspace, keeping all existing tables and records."""
    bt = resolve_business_type(pack_key)

    with transaction.atomic():
        ensure_workspace_catalog(workspace)
        pack, pack_created = WorkspacePack.objects.get_or_create(
            workspace=workspace,
            pack_key=bt.key,
            defaults={
                'is_primary': False,
                'enabled_by': actor_user,
            },
        )

        existing_tables = {
            row.object_key: row
            for row in WorkspaceTable.objects.select_for_update().filter(workspace=workspace)
        }
        newly_enabled_keys: list[str] = []

        for obj_key in bt.pack_objects:
            row = existing_tables.get(obj_key)
            if row is None:
                WorkspaceTable.objects.create(
                    workspace=workspace,
                    object_key=obj_key,
                    is_core=False,
                    is_enabled=True,
                    source_pack_key=bt.key,
                )
                newly_enabled_keys.append(obj_key)
            elif not row.is_enabled:
                row.is_enabled = True
                if not row.source_pack_key:
                    row.source_pack_key = bt.key
                row.save(update_fields=['is_enabled', 'source_pack_key', 'updated_at'])
                newly_enabled_keys.append(obj_key)

        if pack_created or newly_enabled_keys:
            if 'deal' in newly_enabled_keys:
                from .pipeline_service import ensure_workspace_pipeline

                ensure_workspace_pipeline(workspace, require_enabled=False)
            record_audit_event(
                workspace=workspace,
                object_type='workspace_pack',
                object_id=pack.id,
                record_label=bt.label,
                action=AuditLog.ACTION_CREATE if pack_created else AuditLog.ACTION_UPDATE,
                field_name='pack_enabled',
                previous_value='Disabled' if pack_created else 'Partial',
                new_value=(
                    f"{bt.label} (+{len(newly_enabled_keys)} table"
                    f"{'' if len(newly_enabled_keys) == 1 else 's'})"
                ),
                actor_kind=AuditLog.ACTOR_KIND_PERSON,
                actor_user=actor_user,
            )

    return {
        'pack': pack,
        'business_type': bt,
        'pack_created': pack_created,
        'newly_enabled_keys': newly_enabled_keys,
    }


def set_workspace_table_enabled(
    workspace,
    object_key: str,
    *,
    enabled: bool,
    actor_user,
) -> dict:
    """Enable or disable a non-core catalog table; refuse disabling core or non-empty tables."""
    normalized_key = (object_key or '').strip().lower()
    definition = OBJECTS_BY_KEY.get(normalized_key)
    if definition is None:
        raise CatalogError('Catalog table not found.', status_code=404)

    if definition.is_core:
        if not enabled:
            raise CatalogError(
                f'{definition.label} is a core table and cannot be turned off.',
                status_code=400,
            )
        return {'changed': False, 'object_key': normalized_key, 'record_count': get_table_record_count(workspace, normalized_key)}

    with transaction.atomic():
        ensure_workspace_catalog(workspace)
        row = (
            WorkspaceTable.objects.select_for_update()
            .filter(workspace=workspace, object_key=normalized_key)
            .first()
        )
        record_count = get_table_record_count(workspace, normalized_key)

        if not enabled:
            if row is None or not row.is_enabled:
                return {
                    'changed': False,
                    'object_key': normalized_key,
                    'record_count': record_count,
                }
            if record_count > 0:
                plural_suffix = '' if record_count == 1 else 's'
                raise CatalogError(
                    f'Cannot turn off {definition.label} because it already holds '
                    f'{record_count} record{plural_suffix}.',
                    status_code=409,
                )
            row.is_enabled = False
            row.save(update_fields=['is_enabled', 'updated_at'])
            record_audit_event(
                workspace=workspace,
                object_type='workspace_table',
                object_id=row.id,
                record_label=definition.label,
                action=AuditLog.ACTION_UPDATE,
                field_name='is_enabled',
                previous_value=True,
                new_value=False,
                actor_kind=AuditLog.ACTOR_KIND_PERSON,
                actor_user=actor_user,
            )
            return {
                'changed': True,
                'object_key': normalized_key,
                'record_count': record_count,
            }

        # Turning a catalog table on
        if row is None:
            row = WorkspaceTable.objects.create(
                workspace=workspace,
                object_key=normalized_key,
                is_core=False,
                is_enabled=True,
                source_pack_key=workspace.business_type or '',
            )
            changed = True
        elif not row.is_enabled:
            row.is_enabled = True
            row.save(update_fields=['is_enabled', 'updated_at'])
            changed = True
        else:
            changed = False

        if changed:
            if normalized_key == 'deal':
                from .pipeline_service import ensure_workspace_pipeline

                ensure_workspace_pipeline(workspace, require_enabled=False)
            record_audit_event(
                workspace=workspace,
                object_type='workspace_table',
                object_id=row.id,
                record_label=definition.label,
                action=AuditLog.ACTION_UPDATE,
                field_name='is_enabled',
                previous_value=False,
                new_value=True,
                actor_kind=AuditLog.ACTOR_KIND_PERSON,
                actor_user=actor_user,
            )

    return {
        'changed': changed,
        'object_key': normalized_key,
        'record_count': record_count,
    }


def _compute_workspace_catalog_summary(workspace) -> dict:
    """Compute compact workspace business-type, label, enabled packs, and enabled table keys."""
    ensure_workspace_catalog(workspace)
    packs = list(WorkspacePack.objects.filter(workspace=workspace).order_by('-is_primary', 'created_at', 'id'))
    enabled_table_rows = list(
        WorkspaceTable.objects.filter(workspace=workspace, is_enabled=True).order_by('-is_core', 'id')
    )
    enabled_set = {row.object_key for row in enabled_table_rows}
    ordered_enabled_keys = [obj.key for obj in ALL_OBJECTS if obj.key in enabled_set]
    core_count = sum(1 for key in ordered_enabled_keys if OBJECTS_BY_KEY[key].is_core)
    pack_count = len(ordered_enabled_keys) - core_count
    contact_label = (workspace.contact_label or 'Contact').strip() or 'Contact'

    return {
        'business_type': workspace.business_type,
        'business_type_label': workspace.business_type_label,
        'contact_label': contact_label,
        'contact_plural_label': pluralize_contact_label(contact_label),
        'enabled_packs': [pack.pack_key for pack in packs],
        'enabled_object_keys': ordered_enabled_keys,
        'active_table_count': len(ordered_enabled_keys),
        'core_table_count': core_count,
        'pack_table_count': pack_count,
    }


def serialize_workspace_catalog_summary(workspace) -> dict:
    """Return cached workspace business-type, label, enabled packs, and enabled table keys."""
    if workspace is None or not getattr(workspace, 'id', None):
        return _compute_workspace_catalog_summary(workspace)
    bundle = get_or_set_workspace_cache_bundle(
        workspace.id,
        domains=(DOMAIN_CATALOG,),
        key_suffix='catalog_summary',
        scope='catalog_summary',
        loader=lambda: _compute_workspace_catalog_summary(workspace),
    )
    return dict(bundle['payload'])


def _compute_workspace_packs_detail(workspace) -> dict:
    """Compute full workspace packs, all 30 catalog tables with record counts, and toggles."""
    ensure_workspace_catalog(workspace)
    contact_label = (workspace.contact_label or 'Contact').strip() or 'Contact'
    packs_by_key = {
        pack.pack_key: pack
        for pack in WorkspacePack.objects.filter(workspace=workspace).select_related('enabled_by')
    }
    tables_by_key = {
        row.object_key: row
        for row in WorkspaceTable.objects.filter(workspace=workspace)
    }
    record_counts = get_workspace_table_record_counts(workspace)

    serialized_tables = []
    for obj in ALL_OBJECTS:
        row = tables_by_key.get(obj.key)
        is_enabled = bool(obj.is_core or (row is not None and row.is_enabled))
        record_count = record_counts.get(obj.key, 0)
        source_pack_key = row.source_pack_key if row is not None else ''
        source_bt = BUSINESS_TYPES_BY_KEY.get(source_pack_key)

        base_payload = _object_payload(obj, contact_label=contact_label)
        base_payload.update(
            {
                'is_enabled': is_enabled,
                'in_workspace': row is not None,
                'record_count': record_count,
                'can_disable': bool((not obj.is_core) and is_enabled and record_count == 0),
                'source_pack_key': source_pack_key,
                'source_pack_label': source_bt.label if source_bt else '',
            }
        )
        serialized_tables.append(base_payload)

    enabled_key_set = {item['key'] for item in serialized_tables if item['is_enabled']}
    serialized_packs = []
    for bt in BUSINESS_TYPES:
        pack_row = packs_by_key.get(bt.key)
        enabled_in_pack = sum(1 for key in bt.pack_objects if key in enabled_key_set)
        bt_payload = serialize_business_type_definition(bt)
        bt_payload.update(
            {
                'is_primary': bool(pack_row is not None and pack_row.is_primary),
                'is_pack_enabled': pack_row is not None,
                'enabled_table_count': enabled_in_pack,
                'all_tables_enabled': enabled_in_pack == len(bt.pack_objects),
                'enabled_at': pack_row.created_at.isoformat() if pack_row else None,
            }
        )
        serialized_packs.append(bt_payload)

    core_table_count = sum(1 for item in serialized_tables if item['is_core'] and item['is_enabled'])
    pack_table_count = sum(
        1 for item in serialized_tables if (not item['is_core']) and item['is_enabled']
    )

    return {
        'workspace': {
            'name': workspace.name,
            'slug': workspace.slug,
            **serialize_workspace_catalog_summary(workspace),
        },
        'active_table_count': core_table_count + pack_table_count,
        'core_table_count': core_table_count,
        'pack_table_count': pack_table_count,
        'tables': serialized_tables,
        'packs': serialized_packs,
    }


def get_cached_workspace_packs_detail_bundle(workspace) -> dict:
    """Return workspace packs detail with serialized JSON and ETag, cached per workspace."""
    if OBJECT_RECORD_COUNT_RESOLVERS:
        payload = _compute_workspace_packs_detail(workspace)
        body_bytes = serialize_json_bytes(payload)
        return {
            'payload': payload,
            'body_text': body_bytes.decode('utf-8'),
            'etag': compute_etag(body_bytes, scope=f'ws:{workspace.id}:packs_detail'),
            'cache_hit': False,
        }
    return get_or_set_workspace_cache_bundle(
        workspace.id,
        domains=(DOMAIN_CATALOG, DOMAIN_RECORDS),
        key_suffix='packs_detail',
        scope='packs_detail',
        loader=lambda: _compute_workspace_packs_detail(workspace),
    )


def serialize_workspace_packs_detail(workspace) -> dict:
    """Serialize full workspace packs, all 30 catalog tables with record counts, and toggles."""
    return dict(get_cached_workspace_packs_detail_bundle(workspace)['payload'])
