import hmac
import json
import time

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
import webauthn
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from ..models import PasskeyCredential

PASSKEY_REGISTER_SESSION_KEY = "passkey_register"
PASSKEY_LOGIN_SESSION_KEY = "passkey_login"
PASSKEY_CHALLENGE_TTL_SECONDS = 300
VALID_TRANSPORTS = frozenset(item.value for item in AuthenticatorTransport)


class PasskeyError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class PasskeyVerificationRequiredError(Exception):
    def __init__(self, user):
        super().__init__("Email verification required.")
        self.user = user


def _rp_id() -> str:
    return (getattr(settings, "WEBAUTHN_RP_ID", "localhost") or "localhost").strip()


def _rp_name() -> str:
    return (getattr(settings, "WEBAUTHN_RP_NAME", "jojoCRM") or "jojoCRM").strip()


def _expected_origins() -> list[str]:
    raw = (getattr(settings, "WEBAUTHN_ORIGIN", "http://localhost:5173") or "").strip()
    origins = [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]
    return origins or ["http://localhost:5173"]


def _user_handle_bytes(user) -> bytes:
    return f"jojocrm-user-{user.id}".encode("utf-8")


def _clean_transports(raw_transports) -> list[str]:
    if not isinstance(raw_transports, list):
        return []
    cleaned = []
    for item in raw_transports:
        if isinstance(item, str) and item in VALID_TRANSPORTS and item not in cleaned:
            cleaned.append(item)
    return cleaned


def _descriptor_transports(raw_transports) -> list[AuthenticatorTransport] | None:
    cleaned = [AuthenticatorTransport(t) for t in _clean_transports(raw_transports)]
    return cleaned or None


def _clean_label(raw_name) -> str:
    if not isinstance(raw_name, str):
        return "Passkey"
    trimmed = raw_name.strip()
    if not trimmed:
        return "Passkey"
    return trimmed[:100]


def generate_passkey_registration_options(request, user, name: str = "") -> dict:
    exclude_credentials = [
        PublicKeyCredentialDescriptor(
            id=base64url_to_bytes(pk.credential_id),
            transports=_descriptor_transports(pk.transports),
        )
        for pk in user.passkeys.all()
    ]

    options = webauthn.generate_registration_options(
        rp_id=_rp_id(),
        rp_name=_rp_name(),
        user_id=_user_handle_bytes(user),
        user_name=user.email,
        user_display_name=user.email,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=exclude_credentials or None,
    )

    request.session[PASSKEY_REGISTER_SESSION_KEY] = {
        "challenge": bytes_to_base64url(options.challenge),
        "user_id": user.id,
        "name": _clean_label(name),
        "created_at": int(time.time()),
    }
    return json.loads(webauthn.options_to_json(options))


def verify_passkey_registration(
    request, user, credential_payload, name=None
) -> PasskeyCredential:
    pending = request.session.pop(PASSKEY_REGISTER_SESSION_KEY, None)
    if not isinstance(pending, dict) or pending.get("user_id") != user.id:
        raise PasskeyError(
            "Passkey registration session expired or is invalid. Please try again."
        )

    created_at = pending.get("created_at")
    if (
        not isinstance(created_at, int)
        or int(time.time()) - created_at > PASSKEY_CHALLENGE_TTL_SECONDS
    ):
        raise PasskeyError("Passkey registration timed out. Please try again.")

    if not isinstance(credential_payload, dict):
        raise PasskeyError("Passkey credential payload is required.")

    challenge_b64 = pending.get("challenge")
    if not isinstance(challenge_b64, str) or not challenge_b64:
        raise PasskeyError("Passkey challenge is missing.")

    try:
        verified = webauthn.verify_registration_response(
            credential=credential_payload,
            expected_challenge=base64url_to_bytes(challenge_b64),
            expected_rp_id=_rp_id(),
            expected_origin=_expected_origins(),
            require_user_presence=True,
            require_user_verification=False,
        )
    except (WebAuthnException, ValueError, TypeError) as exc:
        raise PasskeyError("Passkey registration could not be verified.") from exc

    cred_id_b64 = bytes_to_base64url(verified.credential_id)
    pub_key_b64 = bytes_to_base64url(verified.credential_public_key)
    transports = _clean_transports(
        (credential_payload.get("response") or {}).get("transports")
    )
    resolved_name = _clean_label(name if isinstance(name, str) and name.strip() else pending.get("name"))

    try:
        return PasskeyCredential.objects.create(
            user=user,
            credential_id=cred_id_b64,
            public_key=pub_key_b64,
            sign_count=verified.sign_count,
            transports=transports,
            name=resolved_name,
            aaguid=str(verified.aaguid or "")[:64],
            backed_up=bool(verified.credential_backed_up),
        )
    except IntegrityError as exc:
        raise PasskeyError("This passkey is already registered.") from exc


def generate_passkey_login_options(request, email=None) -> dict:
    normalized_email = None
    allow_credentials = None

    if isinstance(email, str) and email.strip():
        normalized_email = email.strip().lower()
        passkeys = list(
            PasskeyCredential.objects.filter(
                user__email__iexact=normalized_email,
                user__is_active=True,
                user__email_verified=True,
            )
        )
        if not passkeys:
            raise PasskeyError(
                "No passkey is registered for that email address.",
                status_code=400,
            )
        allow_credentials = [
            PublicKeyCredentialDescriptor(
                id=base64url_to_bytes(pk.credential_id),
                transports=_descriptor_transports(pk.transports),
            )
            for pk in passkeys
        ]

    options = webauthn.generate_authentication_options(
        rp_id=_rp_id(),
        allow_credentials=allow_credentials,
        user_verification=UserVerificationRequirement.PREFERRED,
    )

    request.session[PASSKEY_LOGIN_SESSION_KEY] = {
        "challenge": bytes_to_base64url(options.challenge),
        "expected_email": normalized_email,
        "created_at": int(time.time()),
    }
    return json.loads(webauthn.options_to_json(options))


def verify_passkey_login(request, credential_payload):
    pending = request.session.pop(PASSKEY_LOGIN_SESSION_KEY, None)
    if not isinstance(pending, dict):
        raise PasskeyError(
            "Passkey sign-in session expired or is invalid. Please try again.",
            status_code=401,
        )

    created_at = pending.get("created_at")
    if (
        not isinstance(created_at, int)
        or int(time.time()) - created_at > PASSKEY_CHALLENGE_TTL_SECONDS
    ):
        raise PasskeyError(
            "Passkey sign-in timed out. Please try again.", status_code=401
        )

    if not isinstance(credential_payload, dict):
        raise PasskeyError("Passkey credential payload is required.", status_code=400)

    raw_cred_id = credential_payload.get("id") or credential_payload.get("rawId")
    if not isinstance(raw_cred_id, str) or not raw_cred_id.strip():
        raise PasskeyError("Passkey sign-in could not be verified.", status_code=401)

    challenge_b64 = pending.get("challenge")
    if not isinstance(challenge_b64, str) or not challenge_b64:
        raise PasskeyError("Passkey sign-in could not be verified.", status_code=401)

    with transaction.atomic():
        passkey = (
            PasskeyCredential.objects.select_related(
                "user", "user__workspace", "user__role"
            )
            .select_for_update(of=("self",))
            .filter(credential_id=raw_cred_id.strip())
            .first()
        )
        if passkey is None:
            raise PasskeyError(
                "Passkey sign-in could not be verified.", status_code=401
            )

        expected_email = pending.get("expected_email")
        if (
            isinstance(expected_email, str)
            and expected_email
            and passkey.user.email.lower() != expected_email
        ):
            raise PasskeyError(
                "Passkey sign-in could not be verified.", status_code=401
            )

        if not passkey.user.is_active:
            raise PasskeyError("This account is inactive.", status_code=401)

        if not passkey.user.email_verified:
            raise PasskeyVerificationRequiredError(passkey.user)

        user_handle_b64 = (credential_payload.get("response") or {}).get("userHandle")
        if isinstance(user_handle_b64, str) and user_handle_b64.strip():
            try:
                user_handle = base64url_to_bytes(user_handle_b64.strip())
            except Exception as exc:
                raise PasskeyError(
                    "Passkey sign-in could not be verified.", status_code=401
                ) from exc
            if not hmac.compare_digest(user_handle, _user_handle_bytes(passkey.user)):
                raise PasskeyError(
                    "Passkey sign-in could not be verified.", status_code=401
                )

        try:
            verified = webauthn.verify_authentication_response(
                credential=credential_payload,
                expected_challenge=base64url_to_bytes(challenge_b64),
                expected_rp_id=_rp_id(),
                expected_origin=_expected_origins(),
                credential_public_key=base64url_to_bytes(passkey.public_key),
                credential_current_sign_count=passkey.sign_count,
                require_user_verification=False,
            )
        except (WebAuthnException, ValueError, TypeError) as exc:
            raise PasskeyError(
                "Passkey sign-in could not be verified.", status_code=401
            ) from exc

        passkey.sign_count = verified.new_sign_count
        passkey.backed_up = bool(verified.credential_backed_up)
        passkey.last_used_at = timezone.now()
        passkey.save(update_fields=["sign_count", "backed_up", "last_used_at"])

        return passkey.user
