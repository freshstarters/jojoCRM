from datetime import timedelta
import hashlib
import secrets
from urllib.parse import urlencode

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from ..models import EmailActionToken, WorkspaceInvite

VERIFY_EMAIL_TTL = timedelta(hours=1)
RESET_PASSWORD_TTL = timedelta(minutes=15)
EMAIL_COOLDOWN = timedelta(seconds=60)


class EmailDeliveryError(Exception):
    """Raised when transactional email configuration is missing or delivery fails."""


class EmailThrottledError(Exception):
    """Raised when a user requests another email before the cooldown has elapsed."""


def get_invite_ttl() -> timedelta:
    """Return the configured invite expiration lifetime as a positive timedelta."""
    raw_hours = getattr(settings, "INVITE_TTL_HOURS", 24)
    try:
        hours = int(raw_hours)
    except (TypeError, ValueError):
        hours = 24
    return timedelta(hours=max(1, hours))


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def ensure_email_provider_configured() -> None:
    backend = getattr(settings, "EMAIL_BACKEND", "")
    # For standard SMTP, fail closed when credentials or host/port are missing.
    if backend == "django.core.mail.backends.smtp.EmailBackend":
        host = (getattr(settings, "EMAIL_HOST", "") or "").strip()
        port = getattr(settings, "EMAIL_PORT", None)
        user = (getattr(settings, "EMAIL_HOST_USER", "") or "").strip()
        password = (getattr(settings, "EMAIL_HOST_PASSWORD", "") or "").strip()
        from_email = (getattr(settings, "DEFAULT_FROM_EMAIL", "") or user).strip()
        if not host or not port or not user or not password or not from_email:
            raise EmailDeliveryError(
                "Transactional email provider credentials are not configured."
            )
    else:
        from_email = (
            getattr(settings, "DEFAULT_FROM_EMAIL", "")
            or getattr(settings, "EMAIL_HOST_USER", "")
            or ""
        ).strip()
        if not from_email:
            raise EmailDeliveryError(
                "Transactional email sender address is not configured."
            )


def check_cooldown(user, purpose: str) -> None:
    now = timezone.now()
    cutoff = now - EMAIL_COOLDOWN
    recent_exists = EmailActionToken.objects.filter(
        user=user,
        purpose=purpose,
        created_at__gt=cutoff,
    ).exists()
    if recent_exists:
        raise EmailThrottledError(
            "Please wait at least 60 seconds before requesting another email."
        )


def issue_email_action_token(
    user,
    purpose: str,
    *,
    enforce_cooldown: bool = False,
) -> str:
    if enforce_cooldown:
        check_cooldown(user, purpose)

    now = timezone.now()
    ttl = (
        VERIFY_EMAIL_TTL
        if purpose == EmailActionToken.PURPOSE_VERIFY_EMAIL
        else RESET_PASSWORD_TTL
    )

    # Invalidate any previously issued unused tokens for the same purpose.
    EmailActionToken.objects.filter(
        user=user,
        purpose=purpose,
        used_at__isnull=True,
    ).update(used_at=now)

    raw_token = secrets.token_urlsafe(32)
    EmailActionToken.objects.create(
        user=user,
        purpose=purpose,
        token_hash=hash_token(raw_token),
        expires_at=now + ttl,
    )
    return raw_token


def consume_email_action_token(raw_token: str, purpose: str):
    if not isinstance(raw_token, str) or not raw_token.strip():
        return None
    token_digest = hash_token(raw_token.strip())
    now = timezone.now()
    token_obj = (
        EmailActionToken.objects.select_related("user", "user__workspace")
        .filter(
            token_hash=token_digest,
            purpose=purpose,
        )
        .first()
    )
    if token_obj is None:
        return None
    if token_obj.used_at is not None or token_obj.expires_at <= now:
        return None
    token_obj.used_at = now
    token_obj.save(update_fields=["used_at"])
    return token_obj.user


def _build_app_url(path: str, token: str) -> str:
    base_url = (getattr(settings, "APP_BASE_URL", "http://localhost:5173") or "").rstrip("/")
    query = urlencode({"token": token})
    return f"{base_url}/{path.lstrip('/')}?{query}"


def _send_transactional_mail(
    *,
    subject: str,
    body: str,
    recipient: str,
    failure_message: str,
) -> None:
    from_email = (
        getattr(settings, "DEFAULT_FROM_EMAIL", "")
        or getattr(settings, "EMAIL_HOST_USER", "")
    ).strip()

    try:
        sent_count = send_mail(
            subject=subject,
            message=body,
            from_email=from_email,
            recipient_list=[recipient],
            fail_silently=False,
        )
    except Exception as exc:
        raise EmailDeliveryError(failure_message) from exc

    if not sent_count:
        raise EmailDeliveryError(failure_message)


def send_verification_email(user, *, enforce_cooldown: bool = False) -> str:
    ensure_email_provider_configured()
    raw_token = issue_email_action_token(
        user,
        EmailActionToken.PURPOSE_VERIFY_EMAIL,
        enforce_cooldown=enforce_cooldown,
    )
    verify_url = _build_app_url("/verify-email", raw_token)
    workspace_name = getattr(getattr(user, "workspace", None), "name", "jojoCRM")
    subject = f"Verify your email for {workspace_name} on jojoCRM"
    body = (
        f"Welcome to {workspace_name} on jojoCRM.\n\n"
        f"Your workspace stays closed until you verify your email address.\n"
        f"Open the single-use verification link below within 1 hour:\n\n"
        f"{verify_url}\n\n"
        f"If you did not create this account, you can ignore this message."
    )
    _send_transactional_mail(
        subject=subject,
        body=body,
        recipient=user.email,
        failure_message="Could not send verification email.",
    )
    return raw_token


def send_password_reset_email(user, *, enforce_cooldown: bool = False) -> str:
    ensure_email_provider_configured()
    raw_token = issue_email_action_token(
        user,
        EmailActionToken.PURPOSE_RESET_PASSWORD,
        enforce_cooldown=enforce_cooldown,
    )
    reset_url = _build_app_url("/reset-password", raw_token)
    workspace_name = getattr(getattr(user, "workspace", None), "name", "jojoCRM")
    subject = f"Reset your jojoCRM password ({workspace_name})"
    body = (
        f"A password reset was requested for your jojoCRM account ({user.email}).\n\n"
        f"Open the single-use link below within 15 minutes to set a new password:\n\n"
        f"{reset_url}\n\n"
        f"If you did not request a password reset, you can ignore this message."
    )
    _send_transactional_mail(
        subject=subject,
        body=body,
        recipient=user.email,
        failure_message="Could not send password reset email.",
    )
    return raw_token


def send_workspace_invite_email(
    workspace,
    invited_by,
    email: str,
    *,
    enforce_cooldown: bool = True,
) -> tuple[WorkspaceInvite, str]:
    """Create or rotate an active workspace invite and deliver the invite email."""
    ensure_email_provider_configured()

    normalized_email = (email or "").strip().lower()
    now = timezone.now()

    if enforce_cooldown:
        cutoff = now - EMAIL_COOLDOWN
        recently_sent = WorkspaceInvite.objects.filter(
            workspace=workspace,
            email__iexact=normalized_email,
            last_sent_at__gt=cutoff,
        ).exists()
        if recently_sent:
            raise EmailThrottledError(
                "Please wait at least 60 seconds before resending an invite to this email."
            )

    ttl = get_invite_ttl()
    ttl_hours = max(1, int(ttl.total_seconds() // 3600))
    raw_token = secrets.token_urlsafe(32)
    token_digest = hash_token(raw_token)
    expires_at = now + ttl

    invite = (
        WorkspaceInvite.objects.select_for_update()
        .filter(
            workspace=workspace,
            email__iexact=normalized_email,
            accepted_at__isnull=True,
            revoked_at__isnull=True,
        )
        .first()
    )
    if invite is not None:
        prev_status = invite.current_status(now=now).capitalize()
        invite.email = normalized_email
        invite.invited_by = invited_by
        invite.token_hash = token_digest
        invite.last_sent_at = now
        invite.expires_at = expires_at
        invite.save(
            update_fields=[
                "email",
                "invited_by",
                "token_hash",
                "last_sent_at",
                "expires_at",
                "updated_at",
            ]
        )
        invite._audit_prev_status = prev_status
    else:
        invite = WorkspaceInvite.objects.create(
            workspace=workspace,
            email=normalized_email,
            invited_by=invited_by,
            token_hash=token_digest,
            last_sent_at=now,
            expires_at=expires_at,
        )
        invite._audit_prev_status = None

    invite_url = _build_app_url("/accept-invite", raw_token)
    inviter_email = getattr(invited_by, "email", "a workspace administrator")
    expiry_label = f"{ttl_hours} hour" if ttl_hours == 1 else f"{ttl_hours} hours"
    subject = f"You are invited to join {workspace.name} on jojoCRM"
    body = (
        f"{inviter_email} invited you to join {workspace.name} on jojoCRM.\n\n"
        f"You can accept this invite from any country without a connection-location check.\n"
        f"Open the single-use invite link below within {expiry_label} to set your password and join this workspace:\n\n"
        f"{invite_url}\n\n"
        f"If you were not expecting this invitation, you can ignore this message."
    )
    _send_transactional_mail(
        subject=subject,
        body=body,
        recipient=normalized_email,
        failure_message="Could not send workspace invite email.",
    )
    return invite, raw_token
