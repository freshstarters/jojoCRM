import json
from zoneinfo import available_timezones

from django.contrib.auth import logout
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import JsonResponse

VALID_TIMEZONES = frozenset(available_timezones())

VERIFICATION_REQUIRED_MESSAGE = (
    'Verify your email address from the verification message before opening this workspace.'
)
DEACTIVATED_ACCOUNT_MESSAGE = (
    'This account has been deactivated. Contact a workspace administrator to restore access.'
)
EMAIL_SERVICE_UNAVAILABLE_MESSAGE = (
    'Email delivery is unavailable right now. Please try again later.'
)


def parse_json_object(request):
    """Parse a request body into a JSON dict or return a 400 JsonResponse."""
    try:
        raw_body = request.body.decode('utf-8') if request.body else '{}'
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, JsonResponse(
            {'error': 'Request body must be valid JSON.'}, status=400
        )

    if not isinstance(payload, dict):
        return None, JsonResponse(
            {'error': 'Request body must be a JSON object.'}, status=400
        )
    return payload, None


def parse_email_field(raw_email):
    """Normalize and validate an email field or return a 400 JsonResponse."""
    if not isinstance(raw_email, str) or not raw_email.strip():
        return None, JsonResponse({'error': 'Email address is required.'}, status=400)
    normalized = raw_email.strip().lower()
    try:
        validate_email(normalized)
    except ValidationError:
        return None, JsonResponse({'error': 'Enter a valid email address.'}, status=400)
    return normalized, None


def validate_password_basics(password, *, required_error='Password is required.'):
    """Validate non-empty password and minimum 8-character length."""
    if not isinstance(password, str) or not password:
        return JsonResponse({'error': required_error}, status=400)
    if len(password) < 8:
        return JsonResponse(
            {'error': 'Password must be at least 8 characters long.'},
            status=400,
        )
    return None


def validate_new_password(password, *, user=None, required_error='Password is required.'):
    """Validate password length and Django's configured AUTH_PASSWORD_VALIDATORS."""
    basic_error = validate_password_basics(password, required_error=required_error)
    if basic_error is not None:
        return basic_error
    try:
        validate_password(password, user=user)
    except ValidationError as exc:
        return JsonResponse({'error': ' '.join(exc.messages)}, status=400)
    return None


def parse_timezone_field(raw_timezone, *, required=False, default='Africa/Accra'):
    """Validate an IANA timezone string or return a 400 JsonResponse."""
    if raw_timezone is None:
        if required:
            return None, JsonResponse({'error': 'Timezone is required.'}, status=400)
        return default, None
    if not isinstance(raw_timezone, str):
        return None, JsonResponse({'error': 'Timezone must be a string.'}, status=400)
    trimmed = raw_timezone.strip()
    if not trimmed:
        if required:
            return None, JsonResponse({'error': 'Timezone is required.'}, status=400)
        return default, None
    if trimmed not in VALID_TIMEZONES:
        return None, JsonResponse({'error': 'Select a valid IANA timezone.'}, status=400)
    return trimmed, None


def require_verified_user(request):
    """Ensure the request has an authenticated, active, email-verified user."""
    if not request.user.is_authenticated:
        return None, JsonResponse({'error': 'Authentication required.'}, status=401)
    if not getattr(request.user, 'is_active', False):
        logout(request)
        return None, JsonResponse({'error': DEACTIVATED_ACCOUNT_MESSAGE}, status=401)
    if not getattr(request.user, 'email_verified', False):
        workspace = getattr(request.user, 'workspace', None)
        return None, JsonResponse(
            {
                'error': VERIFICATION_REQUIRED_MESSAGE,
                'verification_required': True,
                'email': request.user.email,
                'workspace': (
                    {'name': workspace.name, 'slug': workspace.slug}
                    if workspace is not None
                    else None
                ),
            },
            status=403,
        )
    return request.user, None


def require_workspace_member(request, slug: str):
    """Ensure the request user is an active, verified member of the named workspace."""
    if not request.user.is_authenticated:
        return None, None, JsonResponse({'error': 'Authentication required.'}, status=401)
    if not getattr(request.user, 'is_active', False):
        logout(request)
        return None, None, JsonResponse({'error': DEACTIVATED_ACCOUNT_MESSAGE}, status=401)

    workspace = getattr(request.user, 'workspace', None)
    if workspace is None or workspace.slug != slug:
        return None, None, JsonResponse({'error': 'Workspace not found.'}, status=404)

    if not getattr(request.user, 'email_verified', False):
        return (
            None,
            None,
            JsonResponse(
                {
                    'error': VERIFICATION_REQUIRED_MESSAGE,
                    'verification_required': True,
                    'email': request.user.email,
                    'workspace': {
                        'name': workspace.name,
                        'slug': workspace.slug,
                    },
                },
                status=403,
            ),
        )

    return request.user, workspace, None
