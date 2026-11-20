from django.contrib.auth import login
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from ..helpers.request_helpers import (
    VERIFICATION_REQUIRED_MESSAGE,
    parse_json_object,
    require_verified_user,
)
from ..models import AuditLog, PasskeyCredential
from ..serializers import serialize_passkey, serialize_workspace_and_user
from ..service.audit_service import record_audit_event
from ..service.passkey_service import (
    PasskeyError,
    PasskeyVerificationRequiredError,
    generate_passkey_login_options,
    generate_passkey_registration_options,
    verify_passkey_login,
    verify_passkey_registration,
)


@require_http_methods(["POST"])
def passkey_register_options_view(request):
    user, error_response = require_verified_user(request)
    if error_response is not None:
        return error_response

    payload, error_response = parse_json_object(request)
    if error_response is not None:
        return error_response

    name = payload.get("name", "")
    if name is not None and not isinstance(name, str):
        return JsonResponse({"error": "Passkey label must be a string."}, status=400)

    options = generate_passkey_registration_options(request, user, name=name or "")
    return JsonResponse({"options": options}, status=200)


@require_http_methods(["POST"])
def passkey_register_verify_view(request):
    user, error_response = require_verified_user(request)
    if error_response is not None:
        return error_response

    payload, error_response = parse_json_object(request)
    if error_response is not None:
        return error_response

    credential_payload = payload.get("credential")
    name = payload.get("name")
    if name is not None and not isinstance(name, str):
        return JsonResponse({"error": "Passkey label must be a string."}, status=400)

    try:
        with transaction.atomic():
            passkey = verify_passkey_registration(
                request, user, credential_payload, name=name
            )
            record_audit_event(
                workspace=user.workspace,
                object_type="passkey",
                object_id=passkey.id,
                record_label=passkey.name,
                action=AuditLog.ACTION_CREATE,
                field_name="passkey",
                previous_value=None,
                new_value=passkey.name,
                actor_kind=AuditLog.ACTOR_KIND_PERSON,
                actor_user=user,
            )
    except PasskeyError as exc:
        return JsonResponse({"error": exc.message}, status=exc.status_code)

    return JsonResponse({"passkey": serialize_passkey(passkey)}, status=201)


@require_http_methods(["PATCH", "DELETE"])
def passkey_detail_view(request, passkey_id: int):
    """Rename (PATCH) or remove (DELETE) a passkey belonging to the signed-in user."""
    user, error_response = require_verified_user(request)
    if error_response is not None:
        return error_response

    if request.method == "PATCH":
        payload, parse_error = parse_json_object(request)
        if parse_error is not None:
            return parse_error

        raw_name = payload.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            return JsonResponse({"error": "Passkey label is required."}, status=400)
        cleaned_name = " ".join(raw_name.strip().split())
        if len(cleaned_name) > 100:
            return JsonResponse(
                {"error": "Passkey label must be 100 characters or fewer."},
                status=400,
            )

        with transaction.atomic():
            passkey = (
                PasskeyCredential.objects.select_for_update()
                .filter(id=passkey_id, user=user)
                .first()
            )
            if passkey is None:
                return JsonResponse({"error": "Passkey not found."}, status=404)
            previous_name = passkey.name
            changed = cleaned_name != previous_name
            if changed:
                passkey.name = cleaned_name
                passkey.save(update_fields=["name"])
                record_audit_event(
                    workspace=user.workspace,
                    object_type="passkey",
                    object_id=passkey.id,
                    record_label=passkey.name,
                    action=AuditLog.ACTION_UPDATE,
                    field_name="name",
                    previous_value=previous_name,
                    new_value=passkey.name,
                    actor_kind=AuditLog.ACTOR_KIND_PERSON,
                    actor_user=user,
                )

        return JsonResponse(
            {
                "passkey": serialize_passkey(passkey),
                "changed": changed,
                "message": (
                    f'Passkey renamed to "{passkey.name}".'
                    if changed
                    else f'Passkey label remains "{passkey.name}".'
                ),
            },
            status=200,
        )

    with transaction.atomic():
        passkey = (
            PasskeyCredential.objects.select_for_update()
            .filter(id=passkey_id, user=user)
            .first()
        )
        if passkey is None:
            return JsonResponse({"error": "Passkey not found."}, status=404)
        pk_id = passkey.id
        pk_name = passkey.name
        passkey.delete()
        record_audit_event(
            workspace=user.workspace,
            object_type="passkey",
            object_id=pk_id,
            record_label=pk_name,
            action=AuditLog.ACTION_DELETE,
            field_name="passkey",
            previous_value=pk_name,
            new_value=None,
            actor_kind=AuditLog.ACTOR_KIND_PERSON,
            actor_user=user,
        )

    return JsonResponse({"ok": True}, status=200)


# Backward-compatible alias for URL/test imports.
passkey_delete_view = passkey_detail_view


@require_http_methods(["POST"])
def passkey_login_options_view(request):
    payload, error_response = parse_json_object(request)
    if error_response is not None:
        return error_response

    email = payload.get("email")
    if email is not None and not isinstance(email, str):
        return JsonResponse({"error": "Email address must be a string."}, status=400)

    try:
        options = generate_passkey_login_options(request, email=email)
    except PasskeyError as exc:
        return JsonResponse({"error": exc.message}, status=exc.status_code)

    return JsonResponse({"options": options}, status=200)


@require_http_methods(["POST"])
def passkey_login_verify_view(request):
    payload, error_response = parse_json_object(request)
    if error_response is not None:
        return error_response

    credential_payload = payload.get("credential")
    try:
        user = verify_passkey_login(request, credential_payload)
    except PasskeyVerificationRequiredError as exc:
        u = exc.user
        return JsonResponse(
            {
                "error": VERIFICATION_REQUIRED_MESSAGE,
                "verification_required": True,
                "email": u.email,
                "workspace": {
                    "name": u.workspace.name,
                    "slug": u.workspace.slug,
                },
            },
            status=403,
        )
    except PasskeyError as exc:
        return JsonResponse({"error": exc.message}, status=exc.status_code)

    login(request, user)
    return JsonResponse(
        serialize_workspace_and_user(user.workspace, user),
        status=200,
    )
