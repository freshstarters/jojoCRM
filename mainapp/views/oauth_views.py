from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import login
from django.db import transaction
from django.http import HttpResponseRedirect, JsonResponse
from django.views.decorators.http import require_GET, require_http_methods

from ..helpers.request_helpers import require_verified_user
from ..integrations.auth_providers import (
    OAuthAuthenticationError,
    OAuthNotConfiguredError,
    build_authorization_url,
    complete_oauth_login,
    is_provider_configured,
)
from ..models import AuditLog, ExternalIdentity, PasskeyCredential
from ..service.audit_service import record_audit_event


def _app_url(path: str, params: dict | None = None) -> str:
    base = (getattr(settings, "APP_BASE_URL", "http://localhost:5173") or "").rstrip("/")
    url = f"{base}/{path.lstrip('/')}"
    if params:
        url = f"{url}?{urlencode(params)}"
    return url


@require_GET
def auth_providers_view(request):
    return JsonResponse(
        {
            "google": {
                "configured": is_provider_configured(ExternalIdentity.PROVIDER_GOOGLE),
            },
            "microsoft": {
                "configured": is_provider_configured(ExternalIdentity.PROVIDER_MICROSOFT),
            },
            "passkey": {
                "configured": True,
            },
        },
        status=200,
    )


def _oauth_start(request, provider: str):
    try:
        authorization_url = build_authorization_url(request, provider)
    except OAuthNotConfiguredError as exc:
        return JsonResponse({"error": str(exc)}, status=503)

    if request.GET.get("response") == "json":
        return JsonResponse({"authorization_url": authorization_url}, status=200)
    return HttpResponseRedirect(authorization_url)


def _oauth_callback(request, provider: str):
    try:
        user = complete_oauth_login(request, provider)
    except OAuthNotConfiguredError as exc:
        return JsonResponse({"error": str(exc)}, status=503)
    except OAuthAuthenticationError as exc:
        return HttpResponseRedirect(
            _app_url("/login", {"auth_error": str(exc)})
        )

    login(request, user)
    return HttpResponseRedirect(_app_url(f"/{user.workspace.slug}"))


@require_GET
def google_oauth_start_view(request):
    return _oauth_start(request, ExternalIdentity.PROVIDER_GOOGLE)


@require_GET
def google_oauth_callback_view(request):
    return _oauth_callback(request, ExternalIdentity.PROVIDER_GOOGLE)


@require_GET
def microsoft_oauth_start_view(request):
    return _oauth_start(request, ExternalIdentity.PROVIDER_MICROSOFT)


@require_GET
def microsoft_oauth_callback_view(request):
    return _oauth_callback(request, ExternalIdentity.PROVIDER_MICROSOFT)


@require_http_methods(['DELETE', 'POST'])
def auth_provider_unlink_view(request, provider: str):
    """Unlink a connected Google or Microsoft sign-in identity without altering password login."""
    if provider not in (
        ExternalIdentity.PROVIDER_GOOGLE,
        ExternalIdentity.PROVIDER_MICROSOFT,
    ):
        return JsonResponse({'error': 'Unsupported sign-in provider.'}, status=404)

    user, error_response = require_verified_user(request)
    if error_response is not None:
        return error_response

    provider_label = 'Google' if provider == ExternalIdentity.PROVIDER_GOOGLE else 'Microsoft'

    with transaction.atomic():
        identity = (
            ExternalIdentity.objects.select_for_update()
            .filter(user=user, provider=provider)
            .first()
        )
        if identity is None:
            return JsonResponse(
                {'error': f'No {provider_label} sign-in identity is linked to this account.'},
                status=404,
            )

        has_other_external = (
            ExternalIdentity.objects.filter(user=user)
            .exclude(id=identity.id)
            .exists()
        )
        has_passkeys = PasskeyCredential.objects.filter(user=user).exists()
        if not user.has_usable_password() and not has_other_external and not has_passkeys:
            return JsonResponse(
                {
                    'error': (
                        'Add a password or another sign-in method before unlinking '
                        f'{provider_label}.'
                    )
                },
                status=400,
            )

        identity_id = identity.id
        identity.delete()
        record_audit_event(
            workspace=user.workspace,
            object_type='external_identity',
            object_id=identity_id,
            record_label=user.email,
            action=AuditLog.ACTION_DELETE,
            field_name=f'{provider}_sign_in',
            previous_value='Linked',
            new_value='Unlinked',
            actor_kind=AuditLog.ACTOR_KIND_PERSON,
            actor_user=user,
        )

    return JsonResponse(
        {
            'ok': True,
            'provider': provider,
            'has_password': bool(user.has_usable_password()),
            'message': (
                f'{provider_label} sign-in unlinked. Email and password login '
                'remains active.'
            ),
        },
        status=200,
    )
