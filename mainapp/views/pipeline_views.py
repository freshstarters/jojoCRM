from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from ..helpers.crm_errors import CrmDomainError
from ..helpers.request_helpers import parse_json_object, require_workspace_member
from ..service.pipeline_service import (
    add_pipeline_stage,
    get_workspace_pipeline_detail,
    remove_pipeline_stage,
    rename_workspace_pipeline,
    reorder_pipeline_stages,
    update_pipeline_stage,
)


@require_http_methods(['GET', 'PATCH'])
def workspace_pipelines_view(request, slug: str):
    """Fetch (GET) or rename (PATCH) the workspace default sales pipeline."""
    user, workspace, error_response = require_workspace_member(request, slug)
    if error_response is not None:
        return error_response

    try:
        if request.method == 'GET':
            return JsonResponse(get_workspace_pipeline_detail(workspace, user), status=200)

        payload, parse_error = parse_json_object(request)
        if parse_error is not None:
            return parse_error
        return JsonResponse(rename_workspace_pipeline(workspace, user, payload), status=200)
    except CrmDomainError as exc:
        return JsonResponse({'error': exc.message}, status=exc.status_code)


@require_http_methods(['POST'])
def workspace_pipeline_stages_view(request, slug: str):
    """Add a new stage to the workspace sales pipeline."""
    user, workspace, error_response = require_workspace_member(request, slug)
    if error_response is not None:
        return error_response

    payload, parse_error = parse_json_object(request)
    if parse_error is not None:
        return parse_error

    try:
        return JsonResponse(add_pipeline_stage(workspace, user, payload), status=201)
    except CrmDomainError as exc:
        return JsonResponse({'error': exc.message}, status=exc.status_code)


@require_http_methods(['PATCH', 'PUT', 'DELETE'])
def workspace_pipeline_stage_detail_view(request, slug: str, stage_id: int):
    """Edit (PATCH/PUT) or remove (DELETE) a stage in the workspace sales pipeline."""
    user, workspace, error_response = require_workspace_member(request, slug)
    if error_response is not None:
        return error_response

    try:
        if request.method == 'DELETE':
            payload, parse_error = parse_json_object(request)
            if parse_error is not None:
                return parse_error
            move_to_stage_id = (
                payload.get('move_to_stage_id')
                if 'move_to_stage_id' in payload
                else payload.get('target_stage_id')
            )
            if move_to_stage_id is None:
                move_to_stage_id = (
                    request.GET.get('move_to_stage_id')
                    or request.GET.get('target_stage_id')
                )
            result = remove_pipeline_stage(
                workspace,
                user,
                stage_id,
                move_to_stage_id=move_to_stage_id,
            )
            return JsonResponse(result, status=200)

        payload, parse_error = parse_json_object(request)
        if parse_error is not None:
            return parse_error
        return JsonResponse(
            update_pipeline_stage(workspace, user, stage_id, payload),
            status=200,
        )
    except CrmDomainError as exc:
        return JsonResponse({'error': exc.message}, status=exc.status_code)


@require_http_methods(['PUT', 'POST'])
def workspace_pipeline_stages_reorder_view(request, slug: str):
    """Reorder all stages in the workspace sales pipeline."""
    user, workspace, error_response = require_workspace_member(request, slug)
    if error_response is not None:
        return error_response

    payload, parse_error = parse_json_object(request)
    if parse_error is not None:
        return parse_error

    try:
        return JsonResponse(reorder_pipeline_stages(workspace, user, payload), status=200)
    except CrmDomainError as exc:
        return JsonResponse({'error': exc.message}, status=exc.status_code)
