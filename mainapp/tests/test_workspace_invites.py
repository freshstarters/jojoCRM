from datetime import timedelta
import json
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
import jwt

from ..models import User, Workspace, WorkspaceInvite
from .helpers import (
    CLEAR_GH_PAYLOAD,
    _build_webauthn_assertion_payload,
    _build_webauthn_registration_payload,
    _make_rsa_jwk_and_private_key,
    extract_token_from_email_body,
    make_mock_urlopen,
)


@override_settings(
    IPINFO_TOKEN="",
    GEO_LOOKUP_IP="",
    TRUSTED_PROXY_IPS=[],
    BYPASS_GEO_CHECK=False,
    DEBUG=False,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="noreply@jojocrm.example",
    EMAIL_HOST_USER="noreply@jojocrm.example",
    INVITE_TTL_HOURS=24,
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
class WorkspaceInvitesTests(TestCase):
    def _create_verified_workspace(
        self,
        *,
        workspace_name="Accra North Supply",
        email="founder@accranorth.example",
        password="StrongPassword123!",
    ):
        with override_settings(IPINFO_TOKEN="test-token"):
            with patch("mainapp.integrations.geolocation.urllib.request.urlopen") as mock_urlopen:
                mock_urlopen.return_value = make_mock_urlopen(
                    json.dumps(CLEAR_GH_PAYLOAD).encode("utf-8")
                )
                signup_res = self.client.post(
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
                self.assertEqual(signup_res.status_code, 201)

        verify_token = extract_token_from_email_body(mail.outbox[-1].body)
        verify_res = self.client.post(
            "/api/v1/verify-email/",
            data=json.dumps({"token": verify_token}),
            content_type="application/json",
        )
        self.assertEqual(verify_res.status_code, 200)
        mail.outbox.clear()
        slug = signup_res.json()["workspace"]["slug"]
        return Workspace.objects.get(slug=slug), User.objects.get(email=email)

    def test_invite_from_outside_ghana_accepted_without_geo_check_and_single_use(self):
        workspace, owner = self._create_verified_workspace()

        with patch("mainapp.integrations.geolocation.urllib.request.urlopen") as mock_geo:
            invite_res = self.client.post(
                f"/api/v1/workspaces/{workspace.slug}/invites/",
                data=json.dumps({"email": "  Teammate@London.Example  "}),
                content_type="application/json",
                REMOTE_ADDR="8.8.8.8",
            )
            mock_geo.assert_not_called()

        self.assertEqual(invite_res.status_code, 201)
        self.assertEqual(invite_res.json()["invite"]["email"], "teammate@london.example")
        self.assertEqual(invite_res.json()["invite"]["status"], "pending")
        self.assertNotIn("token", invite_res.json()["invite"])
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["teammate@london.example"])
        invite_token = extract_token_from_email_body(mail.outbox[0].body)

        # Log out owner (or keep another browser session) and preview/accept from non-GH IP
        self.client.post("/api/v1/logout/")

        with patch("mainapp.integrations.geolocation.urllib.request.urlopen") as mock_geo:
            preview_res = self.client.get(
                "/api/v1/invites/preview/",
                {"token": invite_token},
                REMOTE_ADDR="8.8.8.8",
            )
            self.assertEqual(preview_res.status_code, 200)
            self.assertEqual(preview_res.json()["email"], "teammate@london.example")
            self.assertEqual(preview_res.json()["workspace"]["slug"], workspace.slug)
            self.assertEqual(preview_res.json()["invited_by_email"], owner.email)

            # Weak password and invalid timezone fail without consuming token
            bad_tz = self.client.post(
                "/api/v1/invites/accept/",
                data=json.dumps(
                    {
                        "token": invite_token,
                        "password": "StrongInvitePassword99!",
                        "timezone": "Mars/Olympus",
                    }
                ),
                content_type="application/json",
                REMOTE_ADDR="8.8.8.8",
            )
            self.assertEqual(bad_tz.status_code, 400)

            bad_pw = self.client.post(
                "/api/v1/invites/accept/",
                data=json.dumps(
                    {
                        "token": invite_token,
                        "password": "password",
                        "timezone": "Europe/London",
                    }
                ),
                content_type="application/json",
                REMOTE_ADDR="8.8.8.8",
            )
            self.assertEqual(bad_pw.status_code, 400)

            accept_res = self.client.post(
                "/api/v1/invites/accept/",
                data=json.dumps(
                    {
                        "token": invite_token,
                        "password": "StrongInvitePassword99!",
                        "timezone": "Europe/London",
                    }
                ),
                content_type="application/json",
                REMOTE_ADDR="8.8.8.8",
            )
            mock_geo.assert_not_called()

        self.assertEqual(accept_res.status_code, 200)
        self.assertEqual(accept_res.json()["workspace"]["slug"], workspace.slug)
        self.assertEqual(accept_res.json()["user"]["email"], "teammate@london.example")
        self.assertEqual(accept_res.json()["user"]["timezone"], "Europe/London")
        self.assertTrue(accept_res.json()["user"]["email_verified"])
        self.assertFalse(accept_res.json()["permissions"]["can_manage_invites"])

        ws_res = self.client.get(f"/api/v1/workspaces/{workspace.slug}/")
        self.assertEqual(ws_res.status_code, 200)

        # Replay of accepted invite token is refused
        replay_res = self.client.post(
            "/api/v1/invites/accept/",
            data=json.dumps(
                {
                    "token": invite_token,
                    "password": "AnotherPassword99!",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(replay_res.status_code, 400)

    def test_invited_user_cannot_create_or_join_second_workspace(self):
        ws1, _owner1 = self._create_verified_workspace(
            workspace_name="Workspace One",
            email="owner1@ws1.example",
        )
        self.client.post(
            f"/api/v1/workspaces/{ws1.slug}/invites/",
            data=json.dumps({"email": "shared@person.example"}),
            content_type="application/json",
        )
        ws1_token = extract_token_from_email_body(mail.outbox[-1].body)
        self.client.post("/api/v1/logout/")

        # Create a second workspace and send a competing pending invite to the same email
        ws2, _owner2 = self._create_verified_workspace(
            workspace_name="Workspace Two",
            email="owner2@ws2.example",
        )
        self.client.post(
            f"/api/v1/workspaces/{ws2.slug}/invites/",
            data=json.dumps({"email": "shared@person.example"}),
            content_type="application/json",
        )
        ws2_token = extract_token_from_email_body(mail.outbox[-1].body)
        self.client.post("/api/v1/logout/")

        # Person accepts ws1 invite first
        accept_ws1 = self.client.post(
            "/api/v1/invites/accept/",
            data=json.dumps(
                {
                    "token": ws1_token,
                    "password": "StrongInvitePassword99!",
                }
            ),
            content_type="application/json",
            REMOTE_ADDR="8.8.8.8",
        )
        self.assertEqual(accept_ws1.status_code, 200)

        # 1. While authenticated as invited user, cannot create a second workspace even with GH IP and new email
        with override_settings(IPINFO_TOKEN="test-token"):
            with patch("mainapp.integrations.geolocation.urllib.request.urlopen") as mock_urlopen:
                mock_urlopen.return_value = make_mock_urlopen(
                    json.dumps(CLEAR_GH_PAYLOAD).encode("utf-8")
                )
                signed_in_signup = self.client.post(
                    "/api/v1/signup/",
                    data=json.dumps(
                        {
                            "workspace_name": "Rogue Second",
                            "business_type": "general_services",
                            "email": "brandnew@person.example",
                            "password": "StrongPassword123!",
                        }
                    ),
                    content_type="application/json",
                    REMOTE_ADDR="154.160.1.1",
                )
                self.assertEqual(signed_in_signup.status_code, 403)
                self.assertEqual(Workspace.objects.count(), 2)

        # 2. Competing invite to ws2 cannot be previewed or accepted
        preview_ws2 = self.client.get("/api/v1/invites/preview/", {"token": ws2_token})
        self.assertEqual(preview_ws2.status_code, 409)
        accept_ws2 = self.client.post(
            "/api/v1/invites/accept/",
            data=json.dumps(
                {
                    "token": ws2_token,
                    "password": "StrongInvitePassword99!",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(accept_ws2.status_code, 409)
        self.assertEqual(
            User.objects.get(email="shared@person.example").workspace_id,
            ws1.id,
        )

        # 3. After logging out, that email still cannot sign up for a second workspace
        self.client.post("/api/v1/logout/")
        with override_settings(IPINFO_TOKEN="test-token"):
            with patch("mainapp.integrations.geolocation.urllib.request.urlopen") as mock_urlopen:
                mock_urlopen.return_value = make_mock_urlopen(
                    json.dumps(CLEAR_GH_PAYLOAD).encode("utf-8")
                )
                dup_signup = self.client.post(
                    "/api/v1/signup/",
                    data=json.dumps(
                        {
                            "workspace_name": "Another Co",
                            "business_type": "general_services",
                            "email": "shared@person.example",
                            "password": "StrongPassword123!",
                        }
                    ),
                    content_type="application/json",
                    REMOTE_ADDR="154.160.1.1",
                )
                self.assertEqual(dup_signup.status_code, 400)
                self.assertEqual(Workspace.objects.count(), 2)

        # 4. Owner of ws2 cannot invite an email that already belongs to ws1
        self.client.post(
            "/api/v1/login/",
            data=json.dumps(
                {"email": "owner2@ws2.example", "password": "StrongPassword123!"}
            ),
            content_type="application/json",
        )
        cross_invite = self.client.post(
            f"/api/v1/workspaces/{ws2.slug}/invites/",
            data=json.dumps({"email": "shared@person.example"}),
            content_type="application/json",
        )
        self.assertEqual(cross_invite.status_code, 409)

    def test_founder_and_invited_user_sign_in_from_other_country_across_methods(self):
        ws, owner = self._create_verified_workspace(
            workspace_name="Kumasi Exports",
            email="founder@kumasi.example",
        )

        # Register a passkey for founder while signed in
        reg_opt = self.client.post(
            "/api/v1/auth/passkey/register/options/",
            data=json.dumps({"name": "Laptop"}),
            content_type="application/json",
        ).json()["options"]
        priv_key, cred_id, reg_cred = _build_webauthn_registration_payload(reg_opt)
        self.assertEqual(
            self.client.post(
                "/api/v1/auth/passkey/register/verify/",
                data=json.dumps({"credential": reg_cred, "name": "Laptop"}),
                content_type="application/json",
            ).status_code,
            201,
        )

        # Invite a second user and let them accept from outside Ghana
        self.client.post(
            f"/api/v1/workspaces/{ws.slug}/invites/",
            data=json.dumps({"email": "staff@kumasi.example"}),
            content_type="application/json",
        )
        invite_token = extract_token_from_email_body(mail.outbox[-1].body)
        self.client.post("/api/v1/logout/")

        self.assertEqual(
            self.client.post(
                "/api/v1/invites/accept/",
                data=json.dumps(
                    {
                        "token": invite_token,
                        "password": "InvitedPassword88!",
                        "timezone": "America/New_York",
                    }
                ),
                content_type="application/json",
                REMOTE_ADDR="198.51.100.22",
            ).status_code,
            200,
        )
        self.client.post("/api/v1/logout/")

        # All subsequent logins from non-GH IPs succeed without calling geolocation
        with patch("mainapp.integrations.geolocation.urllib.request.urlopen") as mock_geo:
            # Founder password login from non-GH IP
            pw_login = self.client.post(
                "/api/v1/login/",
                data=json.dumps(
                    {
                        "email": "founder@kumasi.example",
                        "password": "StrongPassword123!",
                    }
                ),
                content_type="application/json",
                REMOTE_ADDR="203.0.113.5",
            )
            self.assertEqual(pw_login.status_code, 200)
            self.client.post("/api/v1/logout/")

            # Founder passkey login from non-GH IP
            pk_opt = self.client.post(
                "/api/v1/auth/passkey/login/options/",
                data=json.dumps({"email": "founder@kumasi.example"}),
                content_type="application/json",
                REMOTE_ADDR="203.0.113.5",
            ).json()["options"]
            assertion = _build_webauthn_assertion_payload(
                priv_key,
                cred_id,
                pk_opt,
                sign_count=2,
                user_handle=f"jojocrm-user-{owner.id}".encode("utf-8"),
            )
            pk_login = self.client.post(
                "/api/v1/auth/passkey/login/verify/",
                data=json.dumps({"credential": assertion}),
                content_type="application/json",
                REMOTE_ADDR="203.0.113.5",
            )
            self.assertEqual(pk_login.status_code, 200)
            self.client.post("/api/v1/logout/")

            # Founder Google OAuth login from non-GH IP
            now_ts = int(time.time())
            rsa_key, jwks_data = _make_rsa_jwk_and_private_key("kid-non-gh")
            start_res = self.client.get(
                "/api/v1/auth/google/start/",
                REMOTE_ADDR="203.0.113.5",
            )
            params = parse_qs(urlparse(start_res["Location"]).query)
            id_token = jwt.encode(
                {
                    "iss": "https://accounts.google.com",
                    "aud": "google-client-id-test",
                    "sub": "google-founder-non-gh",
                    "email": "founder@kumasi.example",
                    "email_verified": True,
                    "nonce": params["nonce"][0],
                    "iat": now_ts,
                    "exp": now_ts + 300,
                },
                rsa_key,
                algorithm="RS256",
                headers={"kid": "kid-non-gh"},
            )

            def fake_oauth_urlopen(req, timeout=None):
                if req.full_url.endswith("/token"):
                    return make_mock_urlopen(
                        json.dumps({"id_token": id_token}).encode("utf-8")
                    )
                return make_mock_urlopen(json.dumps(jwks_data).encode("utf-8"))

            with patch(
                "mainapp.integrations.auth_providers.urllib.request.urlopen",
                side_effect=fake_oauth_urlopen,
            ):
                oauth_res = self.client.get(
                    "/api/v1/auth/google/callback/",
                    {"code": "code-1", "state": params["state"][0]},
                    REMOTE_ADDR="203.0.113.5",
                )
                self.assertEqual(oauth_res.status_code, 302)
                self.assertTrue(oauth_res["Location"].endswith(f"/{ws.slug}"))

            self.client.post("/api/v1/logout/")

            # Invited user logs in from non-GH IP
            staff_login = self.client.post(
                "/api/v1/login/",
                data=json.dumps(
                    {
                        "email": "staff@kumasi.example",
                        "password": "InvitedPassword88!",
                    }
                ),
                content_type="application/json",
                REMOTE_ADDR="198.51.100.77",
            )
            self.assertEqual(staff_login.status_code, 200)
            self.assertEqual(staff_login.json()["workspace"]["slug"], ws.slug)

            mock_geo.assert_not_called()

    def test_permissions_people_directory_and_cross_workspace_isolation(self):
        ws1, _owner1 = self._create_verified_workspace(
            workspace_name="Alpha Ltd",
            email="owner@alpha.example",
        )
        self.client.post(
            f"/api/v1/workspaces/{ws1.slug}/invites/",
            data=json.dumps({"email": "member@alpha.example"}),
            content_type="application/json",
        )
        invite_id = WorkspaceInvite.objects.get().id
        invite_token = extract_token_from_email_body(mail.outbox[-1].body)

        # Also create a pending invite so we can verify non-manager cannot see or revoke it
        self.client.post(
            f"/api/v1/workspaces/{ws1.slug}/invites/",
            data=json.dumps({"email": "pending@alpha.example"}),
            content_type="application/json",
        )
        pending_id = WorkspaceInvite.objects.get(email="pending@alpha.example").id
        self.client.post("/api/v1/logout/")

        # Accept as member@alpha.example
        self.client.post(
            "/api/v1/invites/accept/",
            data=json.dumps(
                {"token": invite_token, "password": "MemberPassword88!"}
            ),
            content_type="application/json",
        )

        # Member sees read-only people directory with no pending invites exposed
        people_res = self.client.get(f"/api/v1/workspaces/{ws1.slug}/people/")
        self.assertEqual(people_res.status_code, 200)
        people_body = people_res.json()
        self.assertFalse(people_body["can_manage_invites"])
        self.assertEqual(len(people_body["members"]), 2)
        self.assertEqual(people_body["invites"], [])

        # Member cannot send or revoke invites
        send_denied = self.client.post(
            f"/api/v1/workspaces/{ws1.slug}/invites/",
            data=json.dumps({"email": "another@alpha.example"}),
            content_type="application/json",
        )
        self.assertEqual(send_denied.status_code, 403)

        revoke_denied = self.client.post(
            f"/api/v1/workspaces/{ws1.slug}/invites/{pending_id}/revoke/",
        )
        self.assertEqual(revoke_denied.status_code, 403)

        # Member cannot view auth/session for another workspace; ws2 user gets 404 on ws1
        self.client.post("/api/v1/logout/")
        _ws2, _owner2 = self._create_verified_workspace(
            workspace_name="Beta Ltd",
            email="owner@beta.example",
        )
        self.assertEqual(
            self.client.get(f"/api/v1/workspaces/{ws1.slug}/people/").status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                f"/api/v1/workspaces/{ws1.slug}/invites/",
                data=json.dumps({"email": "x@alpha.example"}),
                content_type="application/json",
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                f"/api/v1/workspaces/{ws1.slug}/invites/{invite_id}/revoke/",
            ).status_code,
            404,
        )

    def test_fail_closed_smtp_resend_cooldown_revoke_and_expiry(self):
        ws, _owner = self._create_verified_workspace(
            workspace_name="Tema Foods",
            email="owner@temafoods.example",
        )

        # Inviting existing member of same workspace is refused
        self_invite = self.client.post(
            f"/api/v1/workspaces/{ws.slug}/invites/",
            data=json.dumps({"email": "owner@temafoods.example"}),
            content_type="application/json",
        )
        self.assertEqual(self_invite.status_code, 400)

        # Missing SMTP credentials fail closed with 503 and persist no invite
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="smtp.gmail.com",
            EMAIL_PORT=465,
            EMAIL_HOST_USER="",
            EMAIL_HOST_PASSWORD="",
            DEFAULT_FROM_EMAIL="",
        ):
            fail_res = self.client.post(
                f"/api/v1/workspaces/{ws.slug}/invites/",
                data=json.dumps({"email": "failclosed@temafoods.example"}),
                content_type="application/json",
            )
            self.assertEqual(fail_res.status_code, 503)
            self.assertEqual(WorkspaceInvite.objects.count(), 0)

        # Send valid invite
        ok_res = self.client.post(
            f"/api/v1/workspaces/{ws.slug}/invites/",
            data=json.dumps({"email": "buyer@temafoods.example"}),
            content_type="application/json",
        )
        self.assertEqual(ok_res.status_code, 201)
        invite = WorkspaceInvite.objects.get()
        first_token = extract_token_from_email_body(mail.outbox[-1].body)

        # Cooldown enforces 429 within 60 seconds
        fast_resend = self.client.post(
            f"/api/v1/workspaces/{ws.slug}/invites/",
            data=json.dumps({"email": "buyer@temafoods.example"}),
            content_type="application/json",
        )
        self.assertEqual(fast_resend.status_code, 429)

        # Backdate last_sent_at past 60s; simulate SMTP failure -> rolls back cleanly, preserving first_token
        WorkspaceInvite.objects.filter(id=invite.id).update(
            last_sent_at=timezone.now() - timedelta(seconds=65)
        )
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST_USER="",
            EMAIL_HOST_PASSWORD="",
            DEFAULT_FROM_EMAIL="",
        ):
            failed_resend = self.client.post(
                f"/api/v1/workspaces/{ws.slug}/invites/",
                data=json.dumps({"email": "buyer@temafoods.example"}),
                content_type="application/json",
            )
            self.assertEqual(failed_resend.status_code, 503)

        self.assertEqual(
            self.client.get("/api/v1/invites/preview/", {"token": first_token}).status_code,
            200,
        )

        # Successful resend updates in-place (still 1 row) and invalidates first_token
        resend_ok = self.client.post(
            f"/api/v1/workspaces/{ws.slug}/invites/",
            data=json.dumps({"email": "buyer@temafoods.example"}),
            content_type="application/json",
        )
        self.assertEqual(resend_ok.status_code, 201)
        self.assertEqual(WorkspaceInvite.objects.count(), 1)
        second_token = extract_token_from_email_body(mail.outbox[-1].body)
        self.assertNotEqual(first_token, second_token)
        self.assertEqual(
            self.client.get("/api/v1/invites/preview/", {"token": first_token}).status_code,
            404,
        )
        self.assertEqual(
            self.client.get("/api/v1/invites/preview/", {"token": second_token}).status_code,
            200,
        )

        # Expired invite is refused; re-inviting refreshes it
        WorkspaceInvite.objects.filter(id=invite.id).update(
            expires_at=timezone.now() - timedelta(minutes=1),
            last_sent_at=timezone.now() - timedelta(seconds=65),
        )
        self.assertEqual(
            self.client.get("/api/v1/invites/preview/", {"token": second_token}).status_code,
            400,
        )
        refresh_res = self.client.post(
            f"/api/v1/workspaces/{ws.slug}/invites/",
            data=json.dumps({"email": "buyer@temafoods.example"}),
            content_type="application/json",
        )
        self.assertEqual(refresh_res.status_code, 201)
        self.assertEqual(WorkspaceInvite.objects.count(), 1)
        third_token = extract_token_from_email_body(mail.outbox[-1].body)

        # Revoking blocks preview & acceptance
        revoke_res = self.client.post(
            f"/api/v1/workspaces/{ws.slug}/invites/{invite.id}/revoke/",
        )
        self.assertEqual(revoke_res.status_code, 200)
        self.assertEqual(revoke_res.json()["invite"]["status"], "revoked")
        self.assertEqual(
            self.client.get("/api/v1/invites/preview/", {"token": third_token}).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/invites/accept/",
                data=json.dumps(
                    {"token": third_token, "password": "BuyerPassword99!"}
                ),
                content_type="application/json",
            ).status_code,
            400,
        )

        # After cooldown, re-inviting a previously revoked email creates a fresh invite
        WorkspaceInvite.objects.filter(id=invite.id).update(
            last_sent_at=timezone.now() - timedelta(seconds=65)
        )
        reinvite_res = self.client.post(
            f"/api/v1/workspaces/{ws.slug}/invites/",
            data=json.dumps({"email": "buyer@temafoods.example"}),
            content_type="application/json",
        )
        self.assertEqual(reinvite_res.status_code, 201)
        self.assertEqual(WorkspaceInvite.objects.count(), 2)
        fourth_token = extract_token_from_email_body(mail.outbox[-1].body)
        self.assertEqual(
            self.client.get("/api/v1/invites/preview/", {"token": fourth_token}).status_code,
            200,
        )
