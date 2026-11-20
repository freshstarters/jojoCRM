from django.contrib.auth import update_session_auth_hash
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from ..helpers.request_helpers import (
    parse_json_object,
    parse_timezone_field,
    require_verified_user,
    validate_new_password,
)
from ..integrations.auth_providers import is_provider_configured
from ..models import AuditLog, EmailActionToken, ExternalIdentity, User
from ..serializers import serialize_passkey
from ..service.audit_service import record_audit_event
from ..service.session_service import invalidate_user_sessions, sync_session_version


@require_GET
def auth_methods_view(request):
    user, error_response = require_verified_user(request)
    if error_response is not None:
        return error_response

    identities_by_provider = {
        identity.provider: identity
        for identity in user.external_identities.all()
    }
    passkeys = [serialize_passkey(pk) for pk in user.passkeys.all()]

    def _provider_summary(provider_key: str) -> dict:
        identity = identities_by_provider.get(provider_key)
        return {
            "configured": is_provider_configured(provider_key),
            "linked": identity is not None,
            "last_used_at": (
                identity.last_used_at.isoformat()
                if identity is not None and identity.last_used_at
                else None
            ),
        }

    return JsonResponse(
        {
            "email": user.email,
            "email_verified": bool(user.email_verified),
            "has_password": bool(user.has_usable_password()),
            "providers": {
                "google": _provider_summary(ExternalIdentity.PROVIDER_GOOGLE),
                "microsoft": _provider_summary(ExternalIdentity.PROVIDER_MICROSOFT),
            },
            "mailbox_connection": {
                "connected": False,
                "provider": None,
            },
            "passkeys": passkeys,
        },
        status=200,
    )


@require_http_methods(['PATCH', 'POST'])
def account_profile_view(request):
    """Update the signed-in verified user's timezone preference and record an audit entry."""
    user, error_response = require_verified_user(request)
    if error_response is not None:
        return error_response

    payload, parse_error = parse_json_object(request)
    if parse_error is not None:
        return parse_error

    resolved_tz, tz_error = parse_timezone_field(
        payload.get('timezone'),
        required=True,
    )
    if tz_error is not None:
        return tz_error

    previous_tz = user.timezone
    changed = resolved_tz != previous_tz
    if changed:
        with transaction.atomic():
            locked_user = (
                User.objects.select_for_update(of=('self',))
                .select_related('workspace')
                .get(pk=user.pk)
            )
            previous_tz = locked_user.timezone
            if resolved_tz != previous_tz:
                locked_user.timezone = resolved_tz
                locked_user.save(update_fields=['timezone'])
                record_audit_event(
                    workspace=locked_user.workspace,
                    object_type='user',
                    object_id=locked_user.id,
                    record_label=locked_user.email,
                    action=AuditLog.ACTION_UPDATE,
                    field_name='timezone',
                    previous_value=previous_tz,
                    new_value=resolved_tz,
                    actor_kind=AuditLog.ACTOR_KIND_PERSON,
                    actor_user=locked_user,
                )
            user.timezone = locked_user.timezone

    return JsonResponse(
        {
            'user': {
                'id': user.id,
                'email': user.email,
                'timezone': user.timezone,
                'email_verified': bool(user.email_verified),
            },
            'changed': changed,
            'message': (
                f'Timezone updated to {user.timezone}.'
                if changed
                else f'Timezone remains {user.timezone}.'
            ),
        },
        status=200,
    )


@require_http_methods(['POST'])
def account_password_change_view(request):
    """Verify current_password and set a new password while keeping the active session open."""
    user, error_response = require_verified_user(request)
    if error_response is not None:
        return error_response

    payload, parse_error = parse_json_object(request)
    if parse_error is not None:
        return parse_error

    if not user.has_usable_password():
        return JsonResponse(
            {
                'error': (
                    'Password sign-in is not set on this account. Use password reset '
                    'to establish a password.'
                )
            },
            status=400,
        )

    current_password = payload.get('current_password')
    if not isinstance(current_password, str) or not current_password:
        return JsonResponse({'error': 'Current password is required.'}, status=400)

    if not user.check_password(current_password):
        return JsonResponse({'error': 'Current password is incorrect.'}, status=400)

    new_password = payload.get('new_password')
    password_error = validate_new_password(
        new_password,
        user=user,
        required_error='New password is required.',
    )
    if password_error is not None:
        return password_error

    if user.check_password(new_password):
        return JsonResponse(
            {'error': 'New password must be different from your current password.'},
            status=400,
        )

    with transaction.atomic():
        locked_user = (
            User.objects.select_for_update(of=('self',))
            .select_related('workspace')
            .get(pk=user.pk)
        )
        locked_user.set_password(new_password)
        locked_user.save(update_fields=['password'])
        # A changed password retires outstanding reset links and every other session.
        EmailActionToken.objects.filter(
            user=locked_user,
            purpose=EmailActionToken.PURPOSE_RESET_PASSWORD,
            used_at__isnull=True,
        ).update(used_at=timezone.now())
        invalidate_user_sessions(
            locked_user,
            exclude_session_key=request.session.session_key,
        )
        record_audit_event(
            workspace=locked_user.workspace,
            object_type='user',
            object_id=locked_user.id,
            record_label=locked_user.email,
            action=AuditLog.ACTION_UPDATE,
            field_name='password',
            previous_value='Configured',
            new_value='Updated',
            actor_kind=AuditLog.ACTOR_KIND_PERSON,
            actor_user=locked_user,
        )

    user.password = locked_user.password
    user.session_version = locked_user.session_version
    update_session_auth_hash(request, user)
    sync_session_version(request, user)

    return JsonResponse(
        {
            'ok': True,
            'message': 'Your password has been updated.',
        },
        status=200,
    )


@require_http_methods(['POST'])
def account_password_set_view(request):
    """Allow a signed-in verified user with no password yet to set an initial password."""
    user, error_response = require_verified_user(request)
    if error_response is not None:
        return error_response

    payload, parse_error = parse_json_object(request)
    if parse_error is not None:
        return parse_error

    if user.has_usable_password():
        return JsonResponse(
            {
                'error': (
                    'A password is already configured on this account. '
                    'Use change password instead.'
                )
            },
            status=400,
        )

    new_password = payload.get('new_password')
    password_error = validate_new_password(
        new_password,
        user=user,
        required_error='New password is required.',
    )
    if password_error is not None:
        return password_error

    with transaction.atomic():
        locked_user = (
            User.objects.select_for_update(of=('self',))
            .select_related('workspace')
            .get(pk=user.pk)
        )
        if locked_user.has_usable_password():
            return JsonResponse(
                {
                    'error': (
                        'A password is already configured on this account. '
                        'Use change password instead.'
                    )
                },
                status=400,
            )
        locked_user.set_password(new_password)
        locked_user.save(update_fields=['password'])
        EmailActionToken.objects.filter(
            user=locked_user,
            purpose=EmailActionToken.PURPOSE_RESET_PASSWORD,
            used_at__isnull=True,
        ).update(used_at=timezone.now())
        invalidate_user_sessions(
            locked_user,
            exclude_session_key=request.session.session_key,
        )
        record_audit_event(
            workspace=locked_user.workspace,
            object_type='user',
            object_id=locked_user.id,
            record_label=locked_user.email,
            action=AuditLog.ACTION_UPDATE,
            field_name='password',
            previous_value='Not set',
            new_value='Configured',
            actor_kind=AuditLog.ACTOR_KIND_PERSON,
            actor_user=locked_user,
        )

    user.password = locked_user.password
    user.session_version = locked_user.session_version
    update_session_auth_hash(request, user)
    sync_session_version(request, user)

    return JsonResponse(
        {
            'ok': True,
            'has_password': True,
            'message': 'Your password has been set.',
        },
        status=200,
    )
