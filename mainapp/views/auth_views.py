import re

from django.contrib.auth import authenticate, login, logout
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_GET, require_http_methods

from ..helpers.request_helpers import (
    DEACTIVATED_ACCOUNT_MESSAGE,
    EMAIL_SERVICE_UNAVAILABLE_MESSAGE,
    VERIFICATION_REQUIRED_MESSAGE,
    parse_email_field,
    parse_json_object,
    validate_new_password,
    validate_password_basics,
)
from ..integrations.geolocation import (
    LOCATION_REFUSAL_MESSAGE,
    check_request_connection_is_clearly_ghana,
)
from ..models import AuditLog, EmailActionToken, User, Workspace
from ..serializers import serialize_workspace_and_user
from ..service.audit_service import record_audit_event
from ..service.catalog_service import (
    CatalogError,
    initialize_workspace_catalog,
    resolve_business_type,
)
from ..service.pipeline_service import ensure_workspace_pipeline
from ..service.email_service import (
    EmailDeliveryError,
    EmailThrottledError,
    consume_email_action_token,
    ensure_email_provider_configured,
    send_password_reset_email,
    send_verification_email,
)
from ..service.role_service import ensure_workspace_default_roles
from ..service.session_service import invalidate_user_sessions

RESERVED_SLUGS = frozenset(
    {
        "api",
        "admin",
        "static",
        "assets",
        "signup",
        "login",
        "verify-email",
        "forgot-password",
        "reset-password",
        "accept-invite",
        "invite",
    }
)
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@require_http_methods(["POST"])
def signup_view(request):
    # Always run the connection-location check first using the connection address,
    # before inspecting or validating payload fields or touching the database.
    if not check_request_connection_is_clearly_ghana(request):
        return JsonResponse({"error": LOCATION_REFUSAL_MESSAGE}, status=403)

    if request.user.is_authenticated:
        if not getattr(request.user, "is_active", False):
            logout(request)
        elif not getattr(request.user, "email_verified", False):
            pending_workspace = getattr(request.user, "workspace", None)
            return JsonResponse(
                {
                    "error": (
                        "You already have a workspace awaiting email verification. "
                        "Verify your email address or sign out before creating a new workspace."
                    ),
                    "verification_required": True,
                    "email": request.user.email,
                    "workspace": (
                        {"name": pending_workspace.name, "slug": pending_workspace.slug}
                        if pending_workspace is not None
                        else None
                    ),
                },
                status=403,
            )
        else:
            return JsonResponse(
                {
                    "error": (
                        "You are already signed in to a workspace. "
                        "Sign out before creating a new workspace."
                    )
                },
                status=403,
            )

    payload, error_response = parse_json_object(request)
    if error_response is not None:
        return error_response

    workspace_name = payload.get("workspace_name")
    if not isinstance(workspace_name, str) or not workspace_name.strip():
        return JsonResponse({"error": "Workspace name is required."}, status=400)
    workspace_name = workspace_name.strip()
    if len(workspace_name) > 150:
        return JsonResponse(
            {"error": "Workspace name must be 150 characters or fewer."},
            status=400,
        )

    raw_slug = payload.get("slug")
    if raw_slug is not None and not isinstance(raw_slug, str):
        return JsonResponse({"error": "Workspace slug must be a string."}, status=400)

    if isinstance(raw_slug, str) and raw_slug.strip():
        slug = raw_slug.strip().lower()
        if len(slug) > 80 or not SLUG_PATTERN.match(slug):
            return JsonResponse(
                {
                    "error": (
                        "Workspace slug may only contain lowercase letters, "
                        "numbers, and single hyphens."
                    )
                },
                status=400,
            )
    else:
        slug = slugify(workspace_name)[:80].strip("-")
        if not slug or not SLUG_PATTERN.match(slug):
            return JsonResponse(
                {
                    "error": (
                        "Workspace name must include letters or numbers to form "
                        "a valid URL slug."
                    )
                },
                status=400,
            )

    if slug in RESERVED_SLUGS:
        return JsonResponse(
            {"error": f"The workspace slug '{slug}' is reserved."},
            status=400,
        )

    try:
        business_type_def = resolve_business_type(payload.get("business_type"))
    except CatalogError as exc:
        return JsonResponse({"error": exc.message}, status=exc.status_code)

    email, email_error = parse_email_field(payload.get("email"))
    if email_error is not None:
        return email_error

    password = payload.get("password")
    password_error = validate_new_password(
        password,
        user=User(email=email, timezone="Africa/Accra"),
    )
    if password_error is not None:
        return password_error

    if Workspace.objects.filter(slug=slug).exists():
        return JsonResponse(
            {"error": "A workspace with this URL slug already exists."},
            status=400,
        )

    if User.objects.filter(email__iexact=email).exists():
        return JsonResponse(
            {"error": "An account with this email address already exists."},
            status=400,
        )

    try:
        with transaction.atomic():
            workspace = Workspace.objects.create(
                name=workspace_name,
                slug=slug,
                business_type=business_type_def.key,
                business_type_label=business_type_def.label,
                contact_label=business_type_def.default_contact_label,
            )
            owner_role, _staff_role = ensure_workspace_default_roles(workspace)
            user = User.objects.create_user(
                email=email,
                workspace=workspace,
                role=owner_role,
                password=password,
                timezone="Africa/Accra",
                email_verified=False,
            )
            workspace.created_by = user
            workspace.save(update_fields=["created_by"])
            initialize_workspace_catalog(
                workspace,
                business_type_def.key,
                actor_user=user,
            )
            if 'deal' in business_type_def.pack_objects:
                ensure_workspace_pipeline(workspace, require_enabled=False)
            record_audit_event(
                workspace=workspace,
                object_type="workspace",
                object_id=workspace.id,
                record_label=workspace.name,
                action=AuditLog.ACTION_CREATE,
                field_name="workspace",
                previous_value=None,
                new_value=workspace.slug,
                actor_kind=AuditLog.ACTOR_KIND_PERSON,
                actor_user=user,
            )
            record_audit_event(
                workspace=workspace,
                object_type="workspace",
                object_id=workspace.id,
                record_label=workspace.name,
                action=AuditLog.ACTION_CREATE,
                field_name="business_type",
                previous_value=None,
                new_value=business_type_def.label,
                actor_kind=AuditLog.ACTOR_KIND_PERSON,
                actor_user=user,
            )
            send_verification_email(user, enforce_cooldown=False)
    except EmailDeliveryError:
        return JsonResponse(
            {"error": EMAIL_SERVICE_UNAVAILABLE_MESSAGE},
            status=503,
        )
    except IntegrityError:
        if Workspace.objects.filter(slug=slug).exists():
            return JsonResponse(
                {"error": "A workspace with this URL slug already exists."},
                status=400,
            )
        return JsonResponse(
            {"error": "An account with this email address already exists."},
            status=400,
        )

    # Clear any prior session; the workspace stays closed until the email is verified.
    if request.user.is_authenticated:
        logout(request)

    response_data = serialize_workspace_and_user(workspace, user)
    response_data["verification_required"] = True
    response_data["message"] = (
        f"Verification message sent to {user.email}. "
        "Open the link in that message to unlock your workspace."
    )
    return JsonResponse(response_data, status=201)


@require_http_methods(["POST"])
def verify_email_view(request):
    payload, error_response = parse_json_object(request)
    if error_response is not None:
        return error_response

    raw_token = payload.get("token")
    if not isinstance(raw_token, str) or not raw_token.strip():
        return JsonResponse({"error": "Verification token is required."}, status=400)

    with transaction.atomic():
        user = consume_email_action_token(
            raw_token, EmailActionToken.PURPOSE_VERIFY_EMAIL
        )
        if user is None:
            return JsonResponse(
                {"error": "This verification link is invalid or has expired."},
                status=400,
            )
        if not user.is_active:
            transaction.set_rollback(True)
            return JsonResponse({"error": DEACTIVATED_ACCOUNT_MESSAGE}, status=403)
        now = timezone.now()
        if not user.email_verified:
            user.email_verified = True
            user.email_verified_at = now
            user.save(update_fields=["email_verified", "email_verified_at"])
            record_audit_event(
                workspace=user.workspace,
                object_type="user",
                object_id=user.id,
                record_label=user.email,
                action=AuditLog.ACTION_UPDATE,
                field_name="email_verified",
                previous_value=False,
                new_value=True,
                actor_kind=AuditLog.ACTOR_KIND_PERSON,
                actor_user=user,
            )

    login(request, user)
    return JsonResponse(
        serialize_workspace_and_user(user.workspace, user),
        status=200,
    )


@require_http_methods(["POST"])
def resend_verification_view(request):
    payload, error_response = parse_json_object(request)
    if error_response is not None:
        return error_response

    email, email_error = parse_email_field(payload.get("email"))
    if email_error is not None:
        return email_error

    try:
        ensure_email_provider_configured()
    except EmailDeliveryError:
        return JsonResponse(
            {"error": EMAIL_SERVICE_UNAVAILABLE_MESSAGE},
            status=503,
        )

    user = (
        User.objects.select_related("workspace")
        .filter(email__iexact=email, is_active=True)
        .first()
    )
    if user is not None and not user.email_verified:
        try:
            with transaction.atomic():
                send_verification_email(user, enforce_cooldown=True)
        except EmailThrottledError as exc:
            return JsonResponse({"error": str(exc)}, status=429)
        except EmailDeliveryError:
            return JsonResponse(
                {"error": EMAIL_SERVICE_UNAVAILABLE_MESSAGE},
                status=503,
            )

    return JsonResponse(
        {
            "message": (
                "If an unverified account exists for that email address, "
                "a new verification message has been sent."
            )
        },
        status=200,
    )


@require_http_methods(["POST"])
def login_view(request):
    payload, error_response = parse_json_object(request)
    if error_response is not None:
        return error_response

    email = payload.get("email")
    if not isinstance(email, str) or not email.strip():
        return JsonResponse({"error": "Email address is required."}, status=400)
    email = email.strip().lower()

    password = payload.get("password")
    if not isinstance(password, str) or not password:
        return JsonResponse({"error": "Password is required."}, status=400)

    candidate = (
        User.objects.select_related("workspace", "role")
        .filter(email__iexact=email)
        .first()
    )
    if candidate is not None and not candidate.is_active:
        if candidate.check_password(password):
            return JsonResponse({"error": DEACTIVATED_ACCOUNT_MESSAGE}, status=403)
        return JsonResponse({"error": "Invalid email or password."}, status=401)

    user = authenticate(request, username=email, password=password)
    if user is None:
        return JsonResponse({"error": "Invalid email or password."}, status=401)

    if not user.email_verified:
        return JsonResponse(
            {
                "error": VERIFICATION_REQUIRED_MESSAGE,
                "verification_required": True,
                "email": user.email,
                "workspace": {
                    "name": user.workspace.name,
                    "slug": user.workspace.slug,
                },
            },
            status=403,
        )

    login(request, user)
    return JsonResponse(
        serialize_workspace_and_user(user.workspace, user),
        status=200,
    )


@require_http_methods(["POST"])
def logout_view(request):
    logout(request)
    return JsonResponse({"ok": True}, status=200)


@require_http_methods(["POST"])
def password_reset_request_view(request):
    payload, error_response = parse_json_object(request)
    if error_response is not None:
        return error_response

    email, email_error = parse_email_field(payload.get("email"))
    if email_error is not None:
        return email_error

    try:
        ensure_email_provider_configured()
    except EmailDeliveryError:
        return JsonResponse(
            {"error": EMAIL_SERVICE_UNAVAILABLE_MESSAGE},
            status=503,
        )

    user = (
        User.objects.select_related("workspace")
        .filter(email__iexact=email, is_active=True)
        .first()
    )
    if user is not None:
        try:
            with transaction.atomic():
                send_password_reset_email(user, enforce_cooldown=True)
        except EmailThrottledError as exc:
            return JsonResponse({"error": str(exc)}, status=429)
        except EmailDeliveryError:
            return JsonResponse(
                {"error": EMAIL_SERVICE_UNAVAILABLE_MESSAGE},
                status=503,
            )

    return JsonResponse(
        {
            "message": (
                "If an account exists for that email address, "
                "a password reset message has been sent."
            )
        },
        status=200,
    )


@require_http_methods(["POST"])
def password_reset_confirm_view(request):
    payload, error_response = parse_json_object(request)
    if error_response is not None:
        return error_response

    raw_token = payload.get("token")
    if not isinstance(raw_token, str) or not raw_token.strip():
        return JsonResponse({"error": "Password reset token is required."}, status=400)

    password = payload.get("password")
    password_error = validate_password_basics(
        password, required_error="New password is required."
    )
    if password_error is not None:
        return password_error

    with transaction.atomic():
        user = consume_email_action_token(
            raw_token, EmailActionToken.PURPOSE_RESET_PASSWORD
        )
        if user is None:
            return JsonResponse(
                {"error": "This password reset link is invalid or has expired."},
                status=400,
            )
        if not user.is_active:
            transaction.set_rollback(True)
            return JsonResponse({"error": DEACTIVATED_ACCOUNT_MESSAGE}, status=403)

        password_error = validate_new_password(
            password, user=user, required_error="New password is required."
        )
        if password_error is not None:
            # Transaction will roll back when returning outside or if we set rollback
            transaction.set_rollback(True)
            return password_error

        now = timezone.now()
        user.set_password(password)
        if not user.email_verified:
            user.email_verified = True
            user.email_verified_at = now
        user.save(
            update_fields=["password", "email_verified", "email_verified_at"]
        )
        record_audit_event(
            workspace=user.workspace,
            object_type="user",
            object_id=user.id,
            record_label=user.email,
            action=AuditLog.ACTION_UPDATE,
            field_name="password",
            previous_value="Configured",
            new_value="Reset",
            actor_kind=AuditLog.ACTOR_KIND_PERSON,
            actor_user=user,
        )

        # Invalidate any remaining unused verification or reset tokens for this user.
        EmailActionToken.objects.filter(
            user=user,
            used_at__isnull=True,
        ).update(used_at=now)
        # Revoke every pre-existing session; login() below binds the new session_version.
        invalidate_user_sessions(user)

    login(request, user)
    return JsonResponse(
        serialize_workspace_and_user(user.workspace, user),
        status=200,
    )


@require_GET
def auth_session_view(request):
    """Return current session status without mutating state."""
    def _no_store_json(payload: dict) -> JsonResponse:
        resp = JsonResponse(payload, status=200)
        resp["Cache-Control"] = "private, no-store"
        resp["Vary"] = "Cookie"
        return resp

    if not request.user.is_authenticated:
        return _no_store_json({"authenticated": False})

    if not getattr(request.user, "is_active", False):
        logout(request)
        return _no_store_json({"authenticated": False})

    workspace = getattr(request.user, "workspace", None)
    if not getattr(request.user, "email_verified", False) or workspace is None:
        return _no_store_json(
            {
                "authenticated": False,
                "verification_required": True,
                "email": request.user.email,
                "workspace": (
                    {"name": workspace.name, "slug": workspace.slug}
                    if workspace is not None
                    else None
                ),
            }
        )

    payload = serialize_workspace_and_user(workspace, request.user)
    payload["authenticated"] = True
    return _no_store_json(payload)
