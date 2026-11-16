import json
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.test import TestCase, override_settings
from django.utils import timezone
import jwt

from ..models import ExternalIdentity, PasskeyCredential, User, Workspace
from .helpers import (
    _build_webauthn_assertion_payload,
    _build_webauthn_registration_payload,
    _make_rsa_jwk_and_private_key,
    make_mock_urlopen,
)


@override_settings(
    IPINFO_TOKEN="test-token-do-not-log",
    GEO_LOOKUP_IP="",
    TRUSTED_PROXY_IPS=[],
    BYPASS_GEO_CHECK=False,
    DEBUG=False,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="noreply@jojocrm.example",
    EMAIL_HOST_USER="noreply@jojocrm.example",
    APP_BASE_URL="http://localhost:5173",
    GOOGLE_OAUTH_CLIENT_ID="google-client-id-test",
    GOOGLE_OAUTH_CLIENT_SECRET="google-client-secret-test",
    GOOGLE_OAUTH_REDIRECT_URI="http://localhost:5173/api/v1/auth/google/callback/",
    MICROSOFT_OAUTH_CLIENT_ID="ms-client-id-test",
    MICROSOFT_OAUTH_CLIENT_SECRET="ms-client-secret-test",
    MICROSOFT_OAUTH_TENANT_ID="common",
    MICROSOFT_OAUTH_REDIRECT_URI="http://localhost:5173/api/v1/auth/microsoft/callback/",
    WEBAUTHN_RP_ID="localhost",
    WEBAUTHN_RP_NAME="jojoCRM",
    WEBAUTHN_ORIGIN="http://localhost:5173",
)
class AdditionalSignInTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(
            name="Kora Industrial",
            slug="kora-industrial",
        )
        self.user = User.objects.create_user(
            email="founder@kora.example",
            workspace=self.workspace,
            password="StrongPassword123!",
            email_verified=True,
            email_verified_at=timezone.now(),
        )
        self.workspace.created_by = self.user
        self.workspace.save(update_fields=["created_by"])

    def _start_and_callback(
        self,
        provider: str,
        *,
        claims_builder,
        rsa_key=None,
        jwks_data=None,
        kid="test-kid-1",
    ):
        if rsa_key is None or jwks_data is None:
            rsa_key, jwks_data = _make_rsa_jwk_and_private_key(kid)

        start_res = self.client.get(f"/api/v1/auth/{provider}/start/")
        self.assertEqual(start_res.status_code, 302)
        auth_url = urlparse(start_res["Location"])
        auth_params = parse_qs(auth_url.query)

        # Ensure strictly identity scopes and PKCE S256 (never mailbox scopes)
        self.assertEqual(auth_params["scope"][0], "openid email profile")
        self.assertEqual(auth_params["code_challenge_method"][0], "S256")
        state = auth_params["state"][0]
        nonce = auth_params["nonce"][0]

        claims = claims_builder(nonce)
        id_token = jwt.encode(
            claims,
            rsa_key,
            algorithm="RS256",
            headers={"kid": kid},
        )

        def fake_urlopen(req, timeout=None):
            if req.full_url.endswith("/token"):
                return make_mock_urlopen(
                    json.dumps(
                        {
                            "access_token": "unused-access-token",
                            "token_type": "Bearer",
                            "id_token": id_token,
                        }
                    ).encode("utf-8")
                )
            return make_mock_urlopen(json.dumps(jwks_data).encode("utf-8"))

        with patch("mainapp.integrations.auth_providers.urllib.request.urlopen", side_effect=fake_urlopen):
            return self.client.get(
                f"/api/v1/auth/{provider}/callback/",
                {"code": "auth-code-123", "state": state},
            )

    def test_missing_oauth_credentials_fail_closed_with_503(self):
        with override_settings(
            GOOGLE_OAUTH_CLIENT_ID="",
            GOOGLE_OAUTH_CLIENT_SECRET="",
            MICROSOFT_OAUTH_CLIENT_ID="",
            MICROSOFT_OAUTH_CLIENT_SECRET="",
        ):
            providers_res = self.client.get("/api/v1/auth/providers/")
            self.assertEqual(providers_res.status_code, 200)
            self.assertFalse(providers_res.json()["google"]["configured"])
            self.assertFalse(providers_res.json()["microsoft"]["configured"])
            self.assertTrue(providers_res.json()["passkey"]["configured"])

            for provider in ("google", "microsoft"):
                start_res = self.client.get(f"/api/v1/auth/{provider}/start/")
                self.assertEqual(start_res.status_code, 503)
                cb_res = self.client.get(
                    f"/api/v1/auth/{provider}/callback/",
                    {"code": "x", "state": "y"},
                )
                self.assertEqual(cb_res.status_code, 503)

    def test_google_and_microsoft_open_same_account_without_connecting_mailbox(self):
        now_ts = int(time.time())

        # Start unverified to prove Q1: verified OAuth login marks the matching account verified.
        self.user.email_verified = False
        self.user.email_verified_at = None
        self.user.save(update_fields=["email_verified", "email_verified_at"])

        google_res = self._start_and_callback(
            "google",
            claims_builder=lambda nonce: {
                "iss": "https://accounts.google.com",
                "aud": "google-client-id-test",
                "sub": "google-sub-001",
                "email": "FOUNDER@kora.example",
                "email_verified": True,
                "nonce": nonce,
                "iat": now_ts,
                "exp": now_ts + 300,
            },
        )
        self.assertEqual(google_res.status_code, 302)
        self.assertTrue(google_res["Location"].endswith("/kora-industrial"))

        self.user.refresh_from_db()
        self.assertTrue(self.user.email_verified)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(ExternalIdentity.objects.count(), 1)

        ws_res = self.client.get("/api/v1/workspaces/kora-industrial/")
        self.assertEqual(ws_res.status_code, 200)
        self.assertEqual(
            ws_res.json()["mailbox_connection"],
            {"connected": False, "provider": None},
        )

        methods_res = self.client.get("/api/v1/auth/methods/")
        self.assertEqual(methods_res.status_code, 200)
        methods = methods_res.json()
        self.assertTrue(methods["providers"]["google"]["linked"])
        self.assertFalse(methods["providers"]["microsoft"]["linked"])
        self.assertEqual(
            methods["mailbox_connection"],
            {"connected": False, "provider": None},
        )

        # Logout and sign in with Microsoft for the same email
        self.client.post("/api/v1/logout/")

        ms_tenant = "9188040d-6c67-4c5b-b112-36a304b66dad"
        ms_res = self._start_and_callback(
            "microsoft",
            claims_builder=lambda nonce: {
                "iss": f"https://login.microsoftonline.com/{ms_tenant}/v2.0",
                "tid": ms_tenant,
                "aud": "ms-client-id-test",
                "sub": "ms-sub-001",
                "email": "founder@kora.example",
                "nonce": nonce,
                "iat": now_ts,
                "exp": now_ts + 300,
            },
        )
        self.assertEqual(ms_res.status_code, 302)
        self.assertTrue(ms_res["Location"].endswith("/kora-industrial"))
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(ExternalIdentity.objects.count(), 2)

        methods_after = self.client.get("/api/v1/auth/methods/").json()
        self.assertTrue(methods_after["providers"]["google"]["linked"])
        self.assertTrue(methods_after["providers"]["microsoft"]["linked"])
        self.assertEqual(
            methods_after["mailbox_connection"],
            {"connected": False, "provider": None},
        )

    def test_microsoft_email_verification_and_fallback_rules(self):
        now_ts = int(time.time())
        tid = "11111111-2222-3333-4444-555555555555"

        # 1. email present + xms_edov=0 -> refused
        res_unverified = self._start_and_callback(
            "microsoft",
            claims_builder=lambda nonce: {
                "iss": f"https://login.microsoftonline.com/{tid}/v2.0",
                "tid": tid,
                "aud": "ms-client-id-test",
                "sub": "ms-sub-bad-1",
                "email": "founder@kora.example",
                "xms_edov": 0,
                "nonce": nonce,
                "iat": now_ts,
                "exp": now_ts + 300,
            },
        )
        self.assertEqual(res_unverified.status_code, 302)
        self.assertIn("/login?auth_error=", res_unverified["Location"])
        self.assertEqual(
            self.client.get("/api/v1/workspaces/kora-industrial/").status_code, 401
        )

        # 2. email absent + preferred_username only (no xms_edov) -> refused
        res_no_edov = self._start_and_callback(
            "microsoft",
            claims_builder=lambda nonce: {
                "iss": f"https://login.microsoftonline.com/{tid}/v2.0",
                "tid": tid,
                "aud": "ms-client-id-test",
                "sub": "ms-sub-bad-2",
                "preferred_username": "founder@kora.example",
                "nonce": nonce,
                "iat": now_ts,
                "exp": now_ts + 300,
            },
        )
        self.assertEqual(res_no_edov.status_code, 302)
        self.assertIn("/login?auth_error=", res_no_edov["Location"])
        self.assertEqual(
            self.client.get("/api/v1/workspaces/kora-industrial/").status_code, 401
        )

        # 3. email absent + preferred_username + xms_edov="1" -> accepted
        res_fallback_ok = self._start_and_callback(
            "microsoft",
            claims_builder=lambda nonce: {
                "iss": f"https://login.microsoftonline.com/{tid}/v2.0",
                "tid": tid,
                "aud": "ms-client-id-test",
                "sub": "ms-sub-good-fallback",
                "preferred_username": "founder@kora.example",
                "xms_edov": "1",
                "nonce": nonce,
                "iat": now_ts,
                "exp": now_ts + 300,
            },
        )
        self.assertEqual(res_fallback_ok.status_code, 302)
        self.assertTrue(res_fallback_ok["Location"].endswith("/kora-industrial"))
        self.assertEqual(
            self.client.get("/api/v1/workspaces/kora-industrial/").status_code, 200
        )

    def test_oauth_refuses_unknown_email_unverified_google_and_subject_mismatch(self):
        now_ts = int(time.time())

        # Unknown email does NOT create a second user or workspace
        unknown_res = self._start_and_callback(
            "google",
            claims_builder=lambda nonce: {
                "iss": "https://accounts.google.com",
                "aud": "google-client-id-test",
                "sub": "google-unknown-sub",
                "email": "stranger@other.example",
                "email_verified": True,
                "nonce": nonce,
                "iat": now_ts,
                "exp": now_ts + 300,
            },
        )
        self.assertEqual(unknown_res.status_code, 302)
        self.assertIn("/login?auth_error=", unknown_res["Location"])
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(Workspace.objects.count(), 1)

        # Unverified Google email is refused
        unverified_google = self._start_and_callback(
            "google",
            claims_builder=lambda nonce: {
                "iss": "https://accounts.google.com",
                "aud": "google-client-id-test",
                "sub": "google-sub-unverified",
                "email": "founder@kora.example",
                "email_verified": False,
                "nonce": nonce,
                "iat": now_ts,
                "exp": now_ts + 300,
            },
        )
        self.assertEqual(unverified_google.status_code, 302)
        self.assertIn("/login?auth_error=", unverified_google["Location"])

        # Bind user to google-sub-1, then attempt same email with google-sub-2 -> refused (subject pinning)
        ok_res = self._start_and_callback(
            "google",
            claims_builder=lambda nonce: {
                "iss": "https://accounts.google.com",
                "aud": "google-client-id-test",
                "sub": "google-sub-1",
                "email": "founder@kora.example",
                "email_verified": True,
                "nonce": nonce,
                "iat": now_ts,
                "exp": now_ts + 300,
            },
        )
        self.assertEqual(ok_res.status_code, 302)
        self.assertTrue(ok_res["Location"].endswith("/kora-industrial"))
        self.client.post("/api/v1/logout/")

        mismatch_res = self._start_and_callback(
            "google",
            claims_builder=lambda nonce: {
                "iss": "https://accounts.google.com",
                "aud": "google-client-id-test",
                "sub": "google-sub-2",
                "email": "founder@kora.example",
                "email_verified": True,
                "nonce": nonce,
                "iat": now_ts,
                "exp": now_ts + 300,
            },
        )
        self.assertEqual(mismatch_res.status_code, 302)
        self.assertIn("/login?auth_error=", mismatch_res["Location"])
        self.assertEqual(
            self.client.get("/api/v1/workspaces/kora-industrial/").status_code, 401
        )

    def test_passkey_register_login_and_password_remains_usable(self):
        # Unauthenticated registration endpoints fail closed with 401
        self.assertEqual(self.client.get("/api/v1/auth/methods/").status_code, 401)
        self.assertEqual(
            self.client.post(
                "/api/v1/auth/passkey/register/options/",
                data="{}",
                content_type="application/json",
            ).status_code,
            401,
        )

        # Sign in with email and password first
        pw_login = self.client.post(
            "/api/v1/login/",
            data=json.dumps(
                {
                    "email": "founder@kora.example",
                    "password": "StrongPassword123!",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(pw_login.status_code, 200)

        # Register a passkey
        reg_opt_res = self.client.post(
            "/api/v1/auth/passkey/register/options/",
            data=json.dumps({"name": "Work Laptop"}),
            content_type="application/json",
        )
        self.assertEqual(reg_opt_res.status_code, 200)
        reg_options = reg_opt_res.json()["options"]

        priv_key, cred_id, reg_cred = _build_webauthn_registration_payload(reg_options)
        reg_verify_res = self.client.post(
            "/api/v1/auth/passkey/register/verify/",
            data=json.dumps({"credential": reg_cred, "name": "Work Laptop"}),
            content_type="application/json",
        )
        self.assertEqual(reg_verify_res.status_code, 201)
        passkey_id = reg_verify_res.json()["passkey"]["id"]
        self.assertEqual(PasskeyCredential.objects.count(), 1)
        self.assertEqual(User.objects.count(), 1)

        # Logout and verify email+password STILL works (passkey is optional, not a second factor)
        self.client.post("/api/v1/logout/")
        pw_again = self.client.post(
            "/api/v1/login/",
            data=json.dumps(
                {
                    "email": "founder@kora.example",
                    "password": "StrongPassword123!",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(pw_again.status_code, 200)
        self.assertEqual(pw_again.json()["workspace"]["slug"], "kora-industrial")

        # Logout and sign in with email-scoped passkey
        self.client.post("/api/v1/logout/")
        login_opt_res = self.client.post(
            "/api/v1/auth/passkey/login/options/",
            data=json.dumps({"email": "founder@kora.example"}),
            content_type="application/json",
        )
        self.assertEqual(login_opt_res.status_code, 200)
        login_options = login_opt_res.json()["options"]
        self.assertEqual(len(login_options["allowCredentials"]), 1)

        assertion_1 = _build_webauthn_assertion_payload(
            priv_key,
            cred_id,
            login_options,
            sign_count=2,
            user_handle=f"jojocrm-user-{self.user.id}".encode("utf-8"),
        )
        login_verify_1 = self.client.post(
            "/api/v1/auth/passkey/login/verify/",
            data=json.dumps({"credential": assertion_1}),
            content_type="application/json",
        )
        self.assertEqual(login_verify_1.status_code, 200)
        self.assertEqual(login_verify_1.json()["workspace"]["slug"], "kora-industrial")
        self.assertEqual(PasskeyCredential.objects.get(id=passkey_id).sign_count, 2)

        # Logout and sign in with discoverable passkey (no email entered)
        self.client.post("/api/v1/logout/")
        disc_opt_res = self.client.post(
            "/api/v1/auth/passkey/login/options/",
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(disc_opt_res.status_code, 200)
        disc_options = disc_opt_res.json()["options"]
        self.assertEqual(disc_options["allowCredentials"], [])

        assertion_2 = _build_webauthn_assertion_payload(
            priv_key,
            cred_id,
            disc_options,
            sign_count=3,
            user_handle=f"jojocrm-user-{self.user.id}".encode("utf-8"),
        )
        login_verify_2 = self.client.post(
            "/api/v1/auth/passkey/login/verify/",
            data=json.dumps({"credential": assertion_2}),
            content_type="application/json",
        )
        self.assertEqual(login_verify_2.status_code, 200)
        self.assertEqual(login_verify_2.json()["user"]["email"], "founder@kora.example")
        self.assertEqual(PasskeyCredential.objects.get(id=passkey_id).sign_count, 3)

        # Delete the passkey and verify email+password still works
        del_res = self.client.delete(f"/api/v1/auth/passkey/{passkey_id}/")
        self.assertEqual(del_res.status_code, 200)
        self.assertEqual(PasskeyCredential.objects.count(), 0)

        self.client.post("/api/v1/logout/")
        pw_final = self.client.post(
            "/api/v1/login/",
            data=json.dumps(
                {
                    "email": "founder@kora.example",
                    "password": "StrongPassword123!",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(pw_final.status_code, 200)

        # Unknown email & account without passkeys give identical 400 error (no enumeration)
        unknown_opt = self.client.post(
            "/api/v1/auth/passkey/login/options/",
            data=json.dumps({"email": "nobody@kora.example"}),
            content_type="application/json",
        )
        no_pk_opt = self.client.post(
            "/api/v1/auth/passkey/login/options/",
            data=json.dumps({"email": "founder@kora.example"}),
            content_type="application/json",
        )
        self.assertEqual(unknown_opt.status_code, 400)
        self.assertEqual(no_pk_opt.status_code, 400)
        self.assertEqual(unknown_opt.json(), no_pk_opt.json())
