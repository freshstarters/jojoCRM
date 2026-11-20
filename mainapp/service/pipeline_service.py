from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from ..helpers.crm_errors import CrmDomainError
from ..models import (
    AuditLog,
    Deal,
    Pipeline,
    PipelineStage,
    Workspace,
    WorkspaceTable,
)
from .audit_service import record_audit_event
from .catalog_service import ensure_workspace_catalog
from .role_service import user_can_manage_sales_settings, user_has_object_right

MAX_PIPELINE_STAGES = 30
VALID_STAGE_TONES = tuple(choice[0] for choice in PipelineStage.TONE_CHOICES)


def _collapse_spaces(value: str) -> str:
    return ' '.join(value.strip().split())


def _parse_pipeline_name(raw_name) -> str:
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise CrmDomainError('Pipeline name is required.', status_code=400)
    cleaned = _collapse_spaces(raw_name)
    if len(cleaned) > 120:
        raise CrmDomainError('Pipeline name must be 120 characters or fewer.', status_code=400)
    return cleaned


def _parse_stage_name(raw_name) -> str:
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise CrmDomainError('Stage name is required.', status_code=400)
    cleaned = _collapse_spaces(raw_name)
    if len(cleaned) > 100:
        raise CrmDomainError('Stage name must be 100 characters or fewer.', status_code=400)
    return cleaned


def _parse_stage_tone(raw_tone, *, default: str = PipelineStage.TONE_INFO) -> str:
    if raw_tone is None or raw_tone == '':
        return default
    if not isinstance(raw_tone, str):
        raise CrmDomainError(
            f'Stage tone must be one of: {", ".join(VALID_STAGE_TONES)}.',
            status_code=400,
        )
    cleaned = raw_tone.strip().lower()
    if cleaned not in VALID_STAGE_TONES:
        raise CrmDomainError(
            f'Stage tone must be one of: {", ".join(VALID_STAGE_TONES)}.',
            status_code=400,
        )
    return cleaned


def _parse_positive_id(raw_id, *, field_name: str) -> int | None:
    if raw_id is None or raw_id == '':
        return None
    if isinstance(raw_id, bool):
        raise CrmDomainError(f'{field_name} must be a valid ID.', status_code=400)
    try:
        parsed = int(raw_id)
    except (TypeError, ValueError) as exc:
        raise CrmDomainError(f'{field_name} must be a valid ID.', status_code=400) from exc
    if parsed < 1:
        raise CrmDomainError(f'{field_name} must be a valid ID.', status_code=400)
    return parsed


def _require_sales_settings_permission(user, workspace) -> None:
    if not user_can_manage_sales_settings(user, workspace):
        raise CrmDomainError(
            'Only the workspace Owner or a role with Theme & layouts or Users & roles access can edit pipeline settings.',
            status_code=403,
        )


def is_deal_table_enabled(workspace) -> bool:
    if workspace is None:
        return False
    ensure_workspace_catalog(workspace)
    return WorkspaceTable.objects.filter(
        workspace=workspace,
        object_key='deal',
        is_enabled=True,
    ).exists()


def ensure_workspace_pipeline(workspace, *, require_enabled: bool = True) -> Pipeline | None:
    """Return the workspace's default Pipeline, seeding the 5 starter stages when Deal is enabled."""
    if not is_deal_table_enabled(workspace):
        if require_enabled:
            raise CrmDomainError('Deal table is not enabled in this workspace.', status_code=404)
        return None

    existing = Pipeline.objects.filter(workspace=workspace, is_default=True).first()
    if existing is not None and existing.stages.exists():
        return existing

    with transaction.atomic():
        Workspace.objects.select_for_update().filter(pk=workspace.pk).exists()
        pipeline = Pipeline.objects.select_for_update().filter(
            workspace=workspace,
            is_default=True,
        ).first()
        if pipeline is None:
            try:
                pipeline = Pipeline.objects.create(
                    workspace=workspace,
                    name=Pipeline.DEFAULT_NAME,
                    is_default=True,
                )
            except IntegrityError:
                pipeline = Pipeline.objects.get(workspace=workspace, is_default=True)

        if not PipelineStage.objects.filter(pipeline=pipeline).exists():
            for position, (stage_name, tone) in enumerate(
                PipelineStage.STARTER_STAGE_SPECS,
                start=1,
            ):
                PipelineStage.objects.create(
                    pipeline=pipeline,
                    workspace=workspace,
                    name=stage_name,
                    position=position,
                    tone=tone,
                )
        return pipeline


def get_ordered_pipeline_stages(workspace, *, require_enabled: bool = True) -> list[PipelineStage]:
    """Return ordered PipelineStage rows for the workspace's default pipeline."""
    pipeline = ensure_workspace_pipeline(workspace, require_enabled=require_enabled)
    if pipeline is None:
        return []
    return list(PipelineStage.objects.filter(pipeline=pipeline).order_by('position', 'id'))


def serialize_pipeline_stage(stage: PipelineStage) -> dict:
    """Serialize compact ordered PipelineStage metadata."""
    return {
        'id': stage.id,
        'name': stage.name,
        'position': stage.position,
        'tone': stage.tone,
    }


def resolve_deal_stage(
    workspace,
    raw_stage=None,
    *,
    stage_id=None,
    allow_default: bool = False,
    prefer_qualified: bool = False,
) -> PipelineStage:
    """Resolve a Deal stage name or ID against the workspace's active pipeline stages."""
    stages = get_ordered_pipeline_stages(workspace, require_enabled=True)
    if not stages:
        raise CrmDomainError('Workspace pipeline has no configured stages.', status_code=400)

    if stage_id is not None and stage_id != '':
        parsed_id = _parse_positive_id(stage_id, field_name='Stage')
        for stage in stages:
            if stage.id == parsed_id:
                return stage
        raise CrmDomainError('Selected stage was not found in this workspace pipeline.', status_code=400)

    if raw_stage is None or (isinstance(raw_stage, str) and not raw_stage.strip()):
        if not allow_default:
            raise CrmDomainError('Stage is required.', status_code=400)
        if prefer_qualified:
            for stage in stages:
                if stage.name.lower() == 'qualified':
                    return stage
        return stages[0]

    if not isinstance(raw_stage, str):
        raise CrmDomainError('Stage must be text.', status_code=400)

    cleaned = _collapse_spaces(raw_stage).lower()
    for stage in stages:
        if stage.name.lower() == cleaned:
            return stage

    available = ', '.join(stage.name for stage in stages)
    raise CrmDomainError(f'Stage must be one of: {available}.', status_code=400)


def _deals_for_stage_filter(workspace, stage: PipelineStage, *, stage_name: str | None = None):
    match_name = stage_name if stage_name is not None else stage.name
    return Deal.objects.filter(workspace=workspace).filter(
        Q(pipeline_stage=stage)
        | Q(pipeline_stage__isnull=True, stage__iexact=match_name)
    )


def _build_deal_stage_audit_rows(
    workspace,
    user,
    deals: list[Deal],
    *,
    old_stage_name: str,
    new_stage_name: str,
    now,
) -> list[AuditLog]:
    if not deals or old_stage_name == new_stage_name:
        return []
    actor_label = (getattr(user, 'email', '') or str(user))[:255]
    return [
        AuditLog(
            workspace=workspace,
            actor_kind=AuditLog.ACTOR_KIND_PERSON,
            actor_user=user,
            actor_label=actor_label,
            object_type='deal',
            object_id=str(deal.id)[:64],
            record_label=(deal.title or f'Deal #{deal.id}')[:255],
            action=AuditLog.ACTION_UPDATE,
            field_name='stage',
            previous_value=old_stage_name,
            new_value=new_stage_name,
            created_at=now,
        )
        for deal in deals
    ]


def _resequence_stages(stages: list[PipelineStage], now) -> None:
    to_update = []
    for index, stage in enumerate(stages, start=1):
        if stage.position != index:
            stage.position = index
            stage.updated_at = now
            to_update.append(stage)
    if to_update:
        PipelineStage.objects.bulk_update(to_update, ['position', 'updated_at'])


def get_workspace_pipeline_detail(workspace, user) -> dict:
    """Return the workspace default pipeline, ordered stages, counts, and caller permissions."""
    pipeline = ensure_workspace_pipeline(workspace, require_enabled=True)
    stages = list(PipelineStage.objects.filter(pipeline=pipeline).order_by('position', 'id'))

    can_manage_sales = user_can_manage_sales_settings(user, workspace)
    can_read_deals = user_has_object_right(user, workspace, 'deal', 'read')
    can_update_deals = user_has_object_right(user, workspace, 'deal', 'update')

    counts_by_stage_id: dict[int, int] = {stage.id: 0 for stage in stages}
    amounts_by_stage_id: dict[int, Decimal] = {stage.id: Decimal('0.00') for stage in stages}

    if can_read_deals or can_manage_sales:
        stages_by_lower = {stage.name.lower(): stage for stage in stages}
        rows = (
            Deal.objects.filter(workspace=workspace)
            .values('pipeline_stage_id', 'stage')
            .annotate(cnt=Count('id'), val=Sum('amount'))
        )
        for row in rows:
            stage_id = row['pipeline_stage_id']
            if stage_id not in counts_by_stage_id:
                matched = stages_by_lower.get((row['stage'] or '').strip().lower())
                stage_id = matched.id if matched else None
            if stage_id is not None:
                counts_by_stage_id[stage_id] += int(row['cnt'] or 0)
                amounts_by_stage_id[stage_id] += row['val'] or Decimal('0.00')

    serialized_stages = []
    for stage in stages:
        item = serialize_pipeline_stage(stage)
        item['deal_count'] = counts_by_stage_id[stage.id]
        item['total_amount'] = (
            f'{amounts_by_stage_id[stage.id]:.2f}' if can_read_deals else None
        )
        serialized_stages.append(item)

    return {
        'pipeline': {
            'id': pipeline.id,
            'name': pipeline.name,
            'is_default': bool(pipeline.is_default),
        },
        'stages': serialized_stages,
        'total_deals': sum(counts_by_stage_id.values()),
        'tone_choices': [
            {'value': value, 'label': label}
            for value, label in PipelineStage.TONE_CHOICES
        ],
        'permissions': {
            'can_manage_sales_settings': can_manage_sales,
            'can_read_deals': can_read_deals,
            'can_update_deals': can_update_deals,
        },
    }


def rename_workspace_pipeline(workspace, user, payload: dict) -> dict:
    """Rename the workspace's default sales pipeline and record an AuditLog entry."""
    pipeline = ensure_workspace_pipeline(workspace, require_enabled=True)
    _require_sales_settings_permission(user, workspace)

    next_name = _parse_pipeline_name(payload.get('name'))

    with transaction.atomic():
        locked = Pipeline.objects.select_for_update().get(pk=pipeline.pk)
        if (
            Pipeline.objects.filter(workspace=workspace, name__iexact=next_name)
            .exclude(pk=locked.pk)
            .exists()
        ):
            raise CrmDomainError(
                f'A pipeline named "{next_name}" already exists.',
                status_code=400,
            )

        old_name = locked.name
        changed = old_name != next_name
        if changed:
            locked.name = next_name
            locked.save(update_fields=['name', 'updated_at'])
            record_audit_event(
                workspace=workspace,
                object_type='pipeline',
                object_id=locked.id,
                record_label=locked.name,
                action=AuditLog.ACTION_UPDATE,
                field_name='name',
                previous_value=old_name,
                new_value=next_name,
                actor_kind=AuditLog.ACTOR_KIND_PERSON,
                actor_user=user,
            )

    result = get_workspace_pipeline_detail(workspace, user)
    result['changed'] = changed
    result['message'] = (
        f'Pipeline renamed to "{next_name}".'
        if changed
        else 'Pipeline name unchanged.'
    )
    return result


def add_pipeline_stage(workspace, user, payload: dict) -> dict:
    """Add a new stage to the workspace pipeline."""
    pipeline = ensure_workspace_pipeline(workspace, require_enabled=True)
    _require_sales_settings_permission(user, workspace)

    name = _parse_stage_name(payload.get('name'))
    tone = _parse_stage_tone(payload.get('tone'), default=PipelineStage.TONE_INFO)

    raw_position = payload.get('position')
    requested_pos = None
    if raw_position is not None and raw_position != '':
        requested_pos = _parse_positive_id(raw_position, field_name='Position')

    with transaction.atomic():
        Pipeline.objects.select_for_update().get(pk=pipeline.pk)
        stages = list(
            PipelineStage.objects.select_for_update()
            .filter(pipeline=pipeline)
            .order_by('position', 'id')
        )
        if len(stages) >= MAX_PIPELINE_STAGES:
            raise CrmDomainError(
                f'A pipeline cannot have more than {MAX_PIPELINE_STAGES} stages.',
                status_code=400,
            )
        if any(stage.name.lower() == name.lower() for stage in stages):
            raise CrmDomainError(f'A stage named "{name}" already exists.', status_code=400)

        insert_index = (
            min(max(requested_pos - 1, 0), len(stages))
            if requested_pos is not None
            else len(stages)
        )
        now = timezone.now()
        new_stage = PipelineStage.objects.create(
            pipeline=pipeline,
            workspace=workspace,
            name=name,
            position=insert_index + 1,
            tone=tone,
        )
        stages.insert(insert_index, new_stage)
        _resequence_stages(stages, now)

        record_audit_event(
            workspace=workspace,
            object_type='pipeline_stage',
            object_id=new_stage.id,
            record_label=new_stage.name,
            action=AuditLog.ACTION_CREATE,
            field_name='stage',
            previous_value=None,
            new_value=new_stage.name,
            actor_kind=AuditLog.ACTOR_KIND_PERSON,
            actor_user=user,
        )

    result = get_workspace_pipeline_detail(workspace, user)
    result['stage'] = serialize_pipeline_stage(new_stage)
    result['message'] = f'Added stage "{new_stage.name}".'
    return result


def update_pipeline_stage(workspace, user, stage_id: int, payload: dict) -> dict:
    """Rename a stage and/or change its tone, syncing Deals and recording per-deal AuditLogs."""
    pipeline = ensure_workspace_pipeline(workspace, require_enabled=True)
    _require_sales_settings_permission(user, workspace)

    has_name = 'name' in payload
    has_tone = 'tone' in payload
    if not has_name and not has_tone:
        raise CrmDomainError('Provide name or tone to update a stage.', status_code=400)

    next_name = _parse_stage_name(payload.get('name')) if has_name else None
    next_tone = _parse_stage_tone(payload.get('tone')) if has_tone else None

    updated_deals_count = 0
    changed = False

    with transaction.atomic():
        Pipeline.objects.select_for_update().get(pk=pipeline.pk)
        stage = (
            PipelineStage.objects.select_for_update()
            .filter(workspace=workspace, pipeline=pipeline, id=stage_id)
            .first()
        )
        if stage is None:
            raise CrmDomainError('Pipeline stage not found.', status_code=404)

        old_name = stage.name
        old_tone = stage.tone
        now = timezone.now()
        update_fields: list[str] = []

        if has_name and next_name != old_name:
            if (
                PipelineStage.objects.filter(pipeline=pipeline, name__iexact=next_name)
                .exclude(pk=stage.pk)
                .exists()
            ):
                raise CrmDomainError(
                    f'A stage named "{next_name}" already exists.',
                    status_code=400,
                )
            stage.name = next_name
            update_fields.append('name')
            changed = True

            affected_deals = list(
                _deals_for_stage_filter(workspace, stage, stage_name=old_name)
                .select_for_update(of=('self',))
                .order_by('id')
            )
            if affected_deals:
                deal_ids = [deal.id for deal in affected_deals]
                Deal.objects.filter(workspace=workspace, id__in=deal_ids).update(
                    pipeline=pipeline,
                    pipeline_stage=stage,
                    stage=next_name,
                    updated_at=now,
                )
                updated_deals_count = len(affected_deals)
                audit_rows = _build_deal_stage_audit_rows(
                    workspace,
                    user,
                    affected_deals,
                    old_stage_name=old_name,
                    new_stage_name=next_name,
                    now=now,
                )
                if audit_rows:
                    AuditLog.objects.bulk_create(audit_rows)

            record_audit_event(
                workspace=workspace,
                object_type='pipeline_stage',
                object_id=stage.id,
                record_label=next_name,
                action=AuditLog.ACTION_UPDATE,
                field_name='name',
                previous_value=old_name,
                new_value=next_name,
                actor_kind=AuditLog.ACTOR_KIND_PERSON,
                actor_user=user,
                created_at=now,
            )

        if has_tone and next_tone != old_tone:
            stage.tone = next_tone
            update_fields.append('tone')
            changed = True
            record_audit_event(
                workspace=workspace,
                object_type='pipeline_stage',
                object_id=stage.id,
                record_label=stage.name,
                action=AuditLog.ACTION_UPDATE,
                field_name='tone',
                previous_value=old_tone,
                new_value=next_tone,
                actor_kind=AuditLog.ACTOR_KIND_PERSON,
                actor_user=user,
                created_at=now,
            )

        if update_fields:
            update_fields.append('updated_at')
            stage.save(update_fields=update_fields)

    result = get_workspace_pipeline_detail(workspace, user)
    result['stage'] = serialize_pipeline_stage(stage)
    result['changed'] = changed
    result['updated_deals_count'] = updated_deals_count
    result['message'] = (
        f'Updated stage "{stage.name}".'
        if changed
        else f'No changes made to stage "{stage.name}".'
    )
    return result


def reorder_pipeline_stages(workspace, user, payload: dict) -> dict:
    """Reorder all stages in the workspace pipeline using an explicit stage_ids sequence."""
    pipeline = ensure_workspace_pipeline(workspace, require_enabled=True)
    _require_sales_settings_permission(user, workspace)

    raw_ids = payload.get('stage_ids')
    if not isinstance(raw_ids, list) or not raw_ids:
        raise CrmDomainError('stage_ids must be a non-empty list of stage IDs.', status_code=400)

    parsed_ids: list[int] = []
    seen: set[int] = set()
    for item in raw_ids:
        stage_id = _parse_positive_id(item, field_name='stage_id')
        if stage_id in seen:
            raise CrmDomainError('stage_ids cannot contain duplicate stage IDs.', status_code=400)
        seen.add(stage_id)
        parsed_ids.append(stage_id)

    with transaction.atomic():
        Pipeline.objects.select_for_update().get(pk=pipeline.pk)
        current_stages = list(
            PipelineStage.objects.select_for_update()
            .filter(pipeline=pipeline)
            .order_by('position', 'id')
        )
        stages_by_id = {stage.id: stage for stage in current_stages}
        if set(parsed_ids) != set(stages_by_id.keys()):
            raise CrmDomainError(
                'stage_ids must include every active stage in this pipeline.',
                status_code=400,
            )

        old_order_ids = [stage.id for stage in current_stages]
        changed = parsed_ids != old_order_ids
        if changed:
            old_names = [stage.name for stage in current_stages]
            reordered = [stages_by_id[sid] for sid in parsed_ids]
            new_names = [stage.name for stage in reordered]
            now = timezone.now()
            _resequence_stages(reordered, now)
            record_audit_event(
                workspace=workspace,
                object_type='pipeline',
                object_id=pipeline.id,
                record_label=pipeline.name,
                action=AuditLog.ACTION_UPDATE,
                field_name='stage_order',
                previous_value=old_names,
                new_value=new_names,
                actor_kind=AuditLog.ACTOR_KIND_PERSON,
                actor_user=user,
                created_at=now,
            )

    result = get_workspace_pipeline_detail(workspace, user)
    result['changed'] = changed
    result['message'] = 'Pipeline stage order saved.' if changed else 'Stage order unchanged.'
    return result


def remove_pipeline_stage(
    workspace,
    user,
    stage_id: int,
    *,
    move_to_stage_id=None,
) -> dict:
    """Remove a stage, moving any deals on it to a chosen remaining stage with per-deal audit logs."""
    pipeline = ensure_workspace_pipeline(workspace, require_enabled=True)
    _require_sales_settings_permission(user, workspace)

    moved_deals_count = 0
    dest_stage_name = None

    with transaction.atomic():
        Pipeline.objects.select_for_update().get(pk=pipeline.pk)
        stages = list(
            PipelineStage.objects.select_for_update()
            .filter(pipeline=pipeline)
            .order_by('position', 'id')
        )
        target = next((stage for stage in stages if stage.id == stage_id), None)
        if target is None:
            raise CrmDomainError('Pipeline stage not found.', status_code=404)

        if len(stages) <= 1:
            raise CrmDomainError(
                'A pipeline must keep at least one stage.',
                status_code=400,
            )

        deals_on_stage = list(
            _deals_for_stage_filter(workspace, target)
            .select_for_update(of=('self',))
            .order_by('id')
        )

        dest_stage = None
        if move_to_stage_id is not None and move_to_stage_id != '':
            parsed_dest_id = _parse_positive_id(
                move_to_stage_id,
                field_name='Destination stage',
            )
            dest_stage = next(
                (stage for stage in stages if stage.id == parsed_dest_id and stage.id != target.id),
                None,
            )
            if dest_stage is None:
                raise CrmDomainError(
                    'Selected destination stage was not found in this pipeline.',
                    status_code=400,
                )

        if deals_on_stage and dest_stage is None:
            plural = '' if len(deals_on_stage) == 1 else 's'
            raise CrmDomainError(
                f'Choose another stage to move the {len(deals_on_stage)} deal{plural} on "{target.name}" before removing it.',
                status_code=400,
            )

        now = timezone.now()
        removed_name = target.name
        removed_id = target.id

        if deals_on_stage and dest_stage is not None:
            deal_ids = [deal.id for deal in deals_on_stage]
            Deal.objects.filter(workspace=workspace, id__in=deal_ids).update(
                pipeline=pipeline,
                pipeline_stage=dest_stage,
                stage=dest_stage.name,
                updated_at=now,
            )
            moved_deals_count = len(deals_on_stage)
            dest_stage_name = dest_stage.name
            audit_rows = _build_deal_stage_audit_rows(
                workspace,
                user,
                deals_on_stage,
                old_stage_name=removed_name,
                new_stage_name=dest_stage.name,
                now=now,
            )
            if audit_rows:
                AuditLog.objects.bulk_create(audit_rows)

        remaining = [stage for stage in stages if stage.id != removed_id]
        target.delete()
        _resequence_stages(remaining, now)

        record_audit_event(
            workspace=workspace,
            object_type='pipeline_stage',
            object_id=removed_id,
            record_label=removed_name,
            action=AuditLog.ACTION_DELETE,
            field_name='stage',
            previous_value=removed_name,
            new_value=dest_stage_name,
            actor_kind=AuditLog.ACTOR_KIND_PERSON,
            actor_user=user,
            created_at=now,
        )

    result = get_workspace_pipeline_detail(workspace, user)
    result['moved_deals_count'] = moved_deals_count
    result['removed_stage_name'] = removed_name
    result['moved_to_stage'] = dest_stage_name
    if moved_deals_count and dest_stage_name:
        plural = '' if moved_deals_count == 1 else 's'
        result['message'] = (
            f'Removed stage "{removed_name}" and moved {moved_deals_count} deal{plural} to "{dest_stage_name}".'
        )
    else:
        result['message'] = f'Removed stage "{removed_name}".'
    return result
