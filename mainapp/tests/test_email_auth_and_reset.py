from datetime import timedelta
import json
from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from ..models import EmailActionToken, User, Workspace
from .helpers import (
    CLEAR_GH_PAYLOAD,
    extract_token_from_email_body,
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
)
class EmailVerificationLoginAndPasswordResetTests(TestCase):
    def _signup_user(
        self,
        email="founder@kora.example",
        password="StrongPassword123!",
        workspace_name="Kora Industrial",
    ):
        with patch("mainapp.integrations.geolocation.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = make_mock_urlopen(
                json.dumps(CLEAR_GH_PAYLOAD).encode("utf-8")
            )
            return self.client.post(
                "/api/v1/signup/",
                data=json.dumps(
                    {
                        "workspace_name": workspace_name,
                        "business_type": "general_services",
                        "email": email,
                        "password": password,
                    }
                ),
                content_type="application/json",
                REMOTE_ADDR="154.160.1.1",
            )

    def test_workspace_closed_until_verification_token_used_once(self):
        signup_res = self._signup_user()
        self.assertEqual(signup_res.status_code, 201)
        self.assertEqual(len(mail.outbox), 1)
        verify_token = extract_token_from_email_body(mail.outbox[0].body)

        # Login with right password before verification is refused with 403
        login_before = self.client.post(
            "/api/v1/login/",
            data=json.dumps(
                {
                    "email": "founder@kora.example",
                    "password": "StrongPassword123!",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(login_before.status_code, 403)
        self.assertTrue(login_before.json().get("verification_required"))

        # Even if a session exists for an unverified user, workspace endpoint returns 403
        user = User.objects.get(email="founder@kora.example")
        self.client.force_login(user)
        ws_before = self.client.get("/api/v1/workspaces/kora-industrial/")
        self.assertEqual(ws_before.status_code, 403)
        self.assertTrue(ws_before.json().get("verification_required"))
        self.client.logout()

        # Verify email with the token
        verify_res = self.client.post(
            "/api/v1/verify-email/",
            data=json.dumps({"token": verify_token}),
            content_type="application/json",
        )
        self.assertEqual(verify_res.status_code, 200)
        self.assertEqual(
            verify_res.json()["workspace"]["slug"], "kora-industrial"
        )
        self.assertTrue(verify_res.json()["user"]["email_verified"])

        # Workspace now opens
        ws_after = self.client.get("/api/v1/workspaces/kora-industrial/")
        self.assertEqual(ws_after.status_code, 200)

        # Single-use: using the same verification token a second time is rejected
        replay_res = self.client.post(
            "/api/v1/verify-email/",
            data=json.dumps({"token": verify_token}),
            content_type="application/json",
        )
        self.assertEqual(replay_res.status_code, 400)

    def test_expired_verification_token_is_rejected(self):
        self._signup_user()
        verify_token = extract_token_from_email_body(mail.outbox[0].body)

        # Expire the token (1 hour TTL exceeded)
        EmailActionToken.objects.all().update(
            expires_at=timezone.now() - timedelta(seconds=5)
        )

        res = self.client.post(
            "/api/v1/verify-email/",
            data=json.dumps({"token": verify_token}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertFalse(User.objects.get().email_verified)

    def test_wrong_password_is_refused(self):
        self._signup_user()
        verify_token = extract_token_from_email_body(mail.outbox[0].body)
        self.client.post(
            "/api/v1/verify-email/",
            data=json.dumps({"token": verify_token}),
            content_type="application/json",
        )
        self.client.post("/api/v1/logout/")

        # Wrong password -> 401
        wrong_pw = self.client.post(
            "/api/v1/login/",
            data=json.dumps(
                {
                    "email": "founder@kora.example",
                    "password": "WrongPassword999!",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(wrong_pw.status_code, 401)
        self.assertIn("invalid email or password", wrong_pw.json()["error"].lower())

        # Unknown email -> 401 with same message
        unknown_email = self.client.post(
            "/api/v1/login/",
            data=json.dumps(
                {
                    "email": "nobody@kora.example",
                    "password": "StrongPassword123!",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(unknown_email.status_code, 401)
        self.assertEqual(unknown_email.json(), wrong_pw.json())

        # Correct password -> 200
        right_pw = self.client.post(
            "/api/v1/login/",
            data=json.dumps(
                {
                    "email": "founder@kora.example",
                    "password": "StrongPassword123!",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(right_pw.status_code, 200)
        self.assertEqual(right_pw.json()["workspace"]["slug"], "kora-industrial")

    def test_password_reset_sends_message_and_accepts_new_password(self):
        self._signup_user()
        verify_token = extract_token_from_email_body(mail.outbox[0].body)
        self.client.post(
            "/api/v1/verify-email/",
            data=json.dumps({"token": verify_token}),
            content_type="application/json",
        )
        self.client.post("/api/v1/logout/")
        mail.outbox.clear()

        # Request password reset
        req_res = self.client.post(
            "/api/v1/password-reset/request/",
            data=json.dumps({"email": "founder@kora.example"}),
            content_type="application/json",
        )
        self.assertEqual(req_res.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        reset_token = extract_token_from_email_body(mail.outbox[0].body)

        # Confirm new password
        confirm_res = self.client.post(
            "/api/v1/password-reset/confirm/",
            data=json.dumps(
                {
                    "token": reset_token,
                    "password": "BrandNewPassword456!",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(confirm_res.status_code, 200)
        self.assertEqual(
            confirm_res.json()["workspace"]["slug"], "kora-industrial"
        )

        # Reusing the reset token is rejected
        replay_res = self.client.post(
            "/api/v1/password-reset/confirm/",
            data=json.dumps(
                {
                    "token": reset_token,
                    "password": "AnotherPassword789!",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(replay_res.status_code, 400)

        # Old password no longer works
        self.client.post("/api/v1/logout/")
        old_login = self.client.post(
            "/api/v1/login/",
            data=json.dumps(
                {
                    "email": "founder@kora.example",
                    "password": "StrongPassword123!",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(old_login.status_code, 401)

        # New password works
        new_login = self.client.post(
            "/api/v1/login/",
            data=json.dumps(
                {
                    "email": "founder@kora.example",
                    "password": "BrandNewPassword456!",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(new_login.status_code, 200)

    def test_cooldown_enforced_on_resend_and_password_reset(self):
        self._signup_user()
        self.assertEqual(len(mail.outbox), 1)

        # Immediate resend within 60s -> 429
        resend_fast = self.client.post(
            "/api/v1/resend-verification/",
            data=json.dumps({"email": "founder@kora.example"}),
            content_type="application/json",
        )
        self.assertEqual(resend_fast.status_code, 429)

        # Backdate token creation past 60s cooldown -> succeeds
        EmailActionToken.objects.all().update(
            created_at=timezone.now() - timedelta(seconds=65)
        )
        resend_ok = self.client.post(
            "/api/v1/resend-verification/",
            data=json.dumps({"email": "founder@kora.example"}),
            content_type="application/json",
        )
        self.assertEqual(resend_ok.status_code, 200)
        self.assertEqual(len(mail.outbox), 2)

    def test_missing_smtp_credentials_fails_closed_and_rolls_back_signup(self):
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="smtp.gmail.com",
            EMAIL_PORT=465,
            EMAIL_HOST_USER="",
            EMAIL_HOST_PASSWORD="",
            DEFAULT_FROM_EMAIL="",
        ):
            res = self._signup_user(
                email="failclosed@kora.example",
                workspace_name="Fail Closed Co",
            )
            self.assertEqual(res.status_code, 503)
            self.assertEqual(Workspace.objects.count(), 0)
            self.assertEqual(User.objects.count(), 0)
