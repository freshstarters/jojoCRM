import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone
import jwt
from jwt.exceptions import PyJWKSetError, PyJWTError

from ..models import AuditLog, ExternalIdentity, User
from ..service.audit_service import record_audit_event

OAUTH_SESSION_KEY = "oauth_pending"
OAUTH_STATE_TTL_SECONDS = 600
OIDC_SCOPES = "openid email profile"
ALLOWED_OIDC_ALGS = ("RS256", "RS384", "RS512", "ES256")
MSA_TENANT_ID = "9188040d-6c67-4c5b-b112-36a304b66dad"


class OAuthNotConfiguredError(Exception):
    """Raised when OAuth client configuration for a provider is missing."""


class OAuthAuthenticationError(Exception):
    """Raised when an OAuth sign-in callback fails verification or account matching."""


def _clean_setting(name: str, default: str = "") -> str:
    return (getattr(settings, name, default) or "").strip()


def _get_provider_config(provider: str) -> dict:
    if provider == ExternalIdentity.PROVIDER_GOOGLE:
        client_id = _clean_setting("GOOGLE_OAUTH_CLIENT_ID")
        client_secret = _clean_setting("GOOGLE_OAUTH_CLIENT_SECRET")
        redirect_uri = _clean_setting("GOOGLE_OAUTH_REDIRECT_URI")
        if not client_id or not client_secret or not redirect_uri:
            raise OAuthNotConfiguredError("Google sign-in is not configured.")
        return {
            "provider": provider,
            "label": "Google",
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "jwks_url": "https://www.googleapis.com/oauth2/v3/certs",
        }

    if provider == ExternalIdentity.PROVIDER_MICROSOFT:
        client_id = _clean_setting("MICROSOFT_OAUTH_CLIENT_ID")
        client_secret = _clean_setting("MICROSOFT_OAUTH_CLIENT_SECRET")
        tenant_id = _clean_setting("MICROSOFT_OAUTH_TENANT_ID", "common") or "common"
        redirect_uri = _clean_setting("MICROSOFT_OAUTH_REDIRECT_URI")
        if not client_id or not client_secret or not tenant_id or not redirect_uri:
            raise OAuthNotConfiguredError("Microsoft sign-in is not configured.")
        return {
            "provider": provider,
            "label": "Microsoft",
            "client_id": client_id,
            "client_secret": client_secret,
            "tenant_id": tenant_id,
            "redirect_uri": redirect_uri,
            "auth_url": f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize",
            "token_url": f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            "jwks_url": f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys",
        }

    raise ValueError(f"Unsupported OAuth provider: {provider}")


def is_provider_configured(provider: str) -> bool:
    try:
        _get_provider_config(provider)
        return True
    except OAuthNotConfiguredError:
        return False


def _pkce_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_authorization_url(request, provider: str) -> str:
    config = _get_provider_config(provider)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(48)

    request.session[OAUTH_SESSION_KEY] = {
        "provider": provider,
        "state": state,
        "nonce": nonce,
        "code_verifier": code_verifier,
        "created_at": int(time.time()),
    }

    params = {
        "client_id": config["client_id"],
        "redirect_uri": config["redirect_uri"],
        "response_type": "code",
        "scope": OIDC_SCOPES,
        "state": state,
        "nonce": nonce,
        "code_challenge": _pkce_challenge(code_verifier),
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    if provider == ExternalIdentity.PROVIDER_MICROSOFT:
        params["response_mode"] = "query"

    return f"{config['auth_url']}?{urllib.parse.urlencode(params)}"


def _http_post_form_json(url: str, form_data: dict, timeout: float) -> dict:
    encoded = urllib.parse.urlencode(form_data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=encoded,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise OAuthAuthenticationError(
            "Sign-in could not be completed with the identity provider."
        ) from exc
    except Exception as exc:
        raise OAuthAuthenticationError(
            "Unable to reach the identity provider to complete sign-in."
        ) from exc

    if status != 200:
        raise OAuthAuthenticationError(
            "Sign-in could not be completed with the identity provider."
        )

    try:
        payload = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise OAuthAuthenticationError(
            "Received an invalid token response from the identity provider."
        ) from exc

    if not isinstance(payload, dict):
        raise OAuthAuthenticationError(
            "Received an invalid token response from the identity provider."
        )
    return payload


def _http_get_json(url: str, timeout: float) -> dict:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            body = resp.read().decode("utf-8")
    except Exception as exc:
        raise OAuthAuthenticationError(
            "Unable to verify the identity provider signing keys."
        ) from exc

    if status != 200:
        raise OAuthAuthenticationError(
            "Unable to verify the identity provider signing keys."
        )

    try:
        payload = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise OAuthAuthenticationError(
            "Received invalid signing keys from the identity provider."
        ) from exc

    if not isinstance(payload, dict):
        raise OAuthAuthenticationError(
            "Received invalid signing keys from the identity provider."
        )
    return payload


def _parse_bool_flag(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("1", "true"):
            return True
        if normalized in ("0", "false"):
            return False
    return None


def _verify_oidc_id_token(id_token: str, config: dict, expected_nonce: str) -> dict:
    if not isinstance(id_token, str) or not id_token.strip():
        raise OAuthAuthenticationError(
            "The identity provider did not return an ID token."
        )

    try:
        header = jwt.get_unverified_header(id_token)
    except PyJWTError as exc:
        raise OAuthAuthenticationError("The returned ID token header is invalid.") from exc

    alg = header.get("alg")
    kid = header.get("kid")
    if alg not in ALLOWED_OIDC_ALGS or not isinstance(kid, str) or not kid.strip():
        raise OAuthAuthenticationError("The returned ID token signature is not supported.")

    timeout = float(getattr(settings, "OAUTH_HTTP_TIMEOUT", 10.0))
    jwks_data = _http_get_json(config["jwks_url"], timeout=timeout)
    try:
        jwk_set = jwt.PyJWKSet.from_dict(jwks_data)
        signing_key = jwk_set[kid]
    except (PyJWKSetError, KeyError, PyJWTError) as exc:
        raise OAuthAuthenticationError(
            "Could not match the identity provider signing key."
        ) from exc

    try:
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=list(ALLOWED_OIDC_ALGS),
            audience=config["client_id"],
            leeway=60,
            options={"require": ["exp", "sub"]},
        )
    except PyJWTError as exc:
        raise OAuthAuthenticationError(
            "The identity provider token could not be verified."
        ) from exc

    token_nonce = claims.get("nonce")
    if not isinstance(token_nonce, str) or not hmac.compare_digest(
        token_nonce, expected_nonce
    ):
        raise OAuthAuthenticationError("Invalid sign-in nonce. Please try again.")

    _validate_issuer(claims, config)
    return claims


def _validate_issuer(claims: dict, config: dict) -> None:
    issuer = claims.get("iss")
    if not isinstance(issuer, str) or not issuer.strip():
        raise OAuthAuthenticationError("Token issuer is missing.")

    provider = config["provider"]
    if provider == ExternalIdentity.PROVIDER_GOOGLE:
        if issuer not in ("https://accounts.google.com", "accounts.google.com"):
            raise OAuthAuthenticationError("Invalid Google token issuer.")
        return

    if provider == ExternalIdentity.PROVIDER_MICROSOFT:
        tid = claims.get("tid")
        if not isinstance(tid, str) or not tid.strip():
            raise OAuthAuthenticationError("Microsoft tenant claim is missing.")
        tid = tid.strip()
        configured_tenant = config["tenant_id"]
        if configured_tenant == "consumers" and tid != MSA_TENANT_ID:
            raise OAuthAuthenticationError("Microsoft account type is not permitted.")
        if configured_tenant == "organizations" and tid == MSA_TENANT_ID:
            raise OAuthAuthenticationError("Personal Microsoft accounts are not permitted.")
        if (
            configured_tenant not in ("common", "organizations", "consumers")
            and tid != configured_tenant
        ):
            raise OAuthAuthenticationError("Microsoft tenant does not match configuration.")

        expected_issuer = f"https://login.microsoftonline.com/{tid}/v2.0"
        if issuer != expected_issuer:
            raise OAuthAuthenticationError("Invalid Microsoft token issuer.")
        return

    raise OAuthAuthenticationError("Unsupported sign-in provider.")


def _extract_verified_email_and_subject(claims: dict, provider: str) -> tuple[str, str]:
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise OAuthAuthenticationError("Provider user identifier is missing.")
    subject = subject.strip()

    if provider == ExternalIdentity.PROVIDER_GOOGLE:
        if claims.get("email_verified") is not True:
            raise OAuthAuthenticationError(
                "Your Google email address is not verified."
            )
        raw_email = claims.get("email")
        if not isinstance(raw_email, str) or not raw_email.strip():
            raise OAuthAuthenticationError(
                "Google did not provide an email address for this account."
            )
        email = raw_email.strip().lower()
    elif provider == ExternalIdentity.PROVIDER_MICROSOFT:
        has_edov = "xms_edov" in claims
        edov_flag = _parse_bool_flag(claims.get("xms_edov")) if has_edov else None
        if has_edov and edov_flag is False:
            raise OAuthAuthenticationError(
                "Your Microsoft email address is not verified."
            )

        raw_email = claims.get("email")
        if isinstance(raw_email, str) and raw_email.strip():
            email = raw_email.strip().lower()
        else:
            # Only fall back to preferred_username when email is absent AND xms_edov is verified.
            preferred = claims.get("preferred_username")
            if (
                edov_flag is True
                and isinstance(preferred, str)
                and preferred.strip()
            ):
                email = preferred.strip().lower()
            else:
                raise OAuthAuthenticationError(
                    "Microsoft did not provide a verified email address for this account."
                )
    else:
        raise OAuthAuthenticationError("Unsupported sign-in provider.")

    try:
        validate_email(email)
    except ValidationError as exc:
        raise OAuthAuthenticationError(
            "The identity provider returned an invalid email address."
        ) from exc

    return email, subject


def complete_oauth_login(request, provider: str) -> User:
    config = _get_provider_config(provider)
    pending = request.session.pop(OAUTH_SESSION_KEY, None)

    provider_error = request.GET.get("error")
    if provider_error:
        raise OAuthAuthenticationError(
            f"{config['label']} sign-in was cancelled or denied."
        )

    if not isinstance(pending, dict) or pending.get("provider") != provider:
        raise OAuthAuthenticationError(
            "Sign-in session expired or is invalid. Please try again."
        )

    created_at = pending.get("created_at")
    if (
        not isinstance(created_at, int)
        or int(time.time()) - created_at > OAUTH_STATE_TTL_SECONDS
    ):
        raise OAuthAuthenticationError("Sign-in session expired. Please try again.")

    expected_state = pending.get("state")
    returned_state = request.GET.get("state")
    if (
        not isinstance(expected_state, str)
        or not isinstance(returned_state, str)
        or not expected_state
        or not hmac.compare_digest(expected_state, returned_state)
    ):
        raise OAuthAuthenticationError("Invalid sign-in state. Please try again.")

    code = request.GET.get("code")
    if not isinstance(code, str) or not code.strip():
        raise OAuthAuthenticationError("Authorization code is missing.")

    code_verifier = pending.get("code_verifier")
    nonce = pending.get("nonce")
    if not isinstance(code_verifier, str) or not isinstance(nonce, str):
        raise OAuthAuthenticationError(
            "Sign-in session is incomplete. Please try again."
        )

    timeout = float(getattr(settings, "OAUTH_HTTP_TIMEOUT", 10.0))
    token_payload = _http_post_form_json(
        config["token_url"],
        {
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "code": code.strip(),
            "redirect_uri": config["redirect_uri"],
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        },
        timeout=timeout,
    )

    claims = _verify_oidc_id_token(
        token_payload.get("id_token"),
        config=config,
        expected_nonce=nonce,
    )
    verified_email, subject = _extract_verified_email_and_subject(claims, provider)

    with transaction.atomic():
        user = (
            User.objects.select_related("workspace", "role")
            .filter(email__iexact=verified_email)
            .first()
        )
        if user is None:
            raise OAuthAuthenticationError(
                "No jojoCRM workspace account exists for that email address. "
                "Create a workspace first."
            )
        if not user.is_active:
            raise OAuthAuthenticationError("This account is inactive.")

        by_sub = (
            ExternalIdentity.objects.select_for_update()
            .filter(provider=provider, subject=subject)
            .first()
        )
        if by_sub is not None and by_sub.user_id != user.id:
            raise OAuthAuthenticationError(
                f"This {config['label']} identity is already linked to another account."
            )

        by_user = (
            ExternalIdentity.objects.select_for_update()
            .filter(user=user, provider=provider)
            .first()
        )
        if by_user is not None and by_user.subject != subject:
            raise OAuthAuthenticationError(
                f"This account is already linked to a different {config['label']} identity."
            )

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
                created_at=now,
            )

        if by_user is not None:
            by_user.email_at_link = verified_email
            by_user.last_used_at = now
            by_user.save(update_fields=["email_at_link", "last_used_at"])
        else:
            identity = ExternalIdentity.objects.create(
                user=user,
                provider=provider,
                subject=subject,
                email_at_link=verified_email,
                last_used_at=now,
            )
            record_audit_event(
                workspace=user.workspace,
                object_type="external_identity",
                object_id=identity.id,
                record_label=user.email,
                action=AuditLog.ACTION_CREATE,
                field_name=f"{provider}_sign_in",
                previous_value="Unlinked",
                new_value="Linked",
                actor_kind=AuditLog.ACTOR_KIND_PERSON,
                actor_user=user,
                created_at=now,
            )

    return user
