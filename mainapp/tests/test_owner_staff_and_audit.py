import json
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.contrib.sessions.models import Session
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.utils import timezone
import jwt

from ..models import (
    AuditLog,
    EmailActionToken,
    ExternalIdentity,
    Role,
    User,
    Workspace,
)
from ..service.audit_service import record_audit_event
from ..service.role_service import ensure_workspace_default_roles
from .helpers import (
    CLEAR_GH_PAYLOAD,
    _build_webauthn_assertion_payload,
    _build_webauthn_registration_payload,
    _make_rsa_jwk_and_private_key,
    extract_token_from_email_body,
    make_mock_urlopen,
)


class OwnerStaffAndAuditTests(TestCase):
    def _create_verified_workspace(
        self,
        client=None,
        *,
        workspace_name="Accra North Supply",
        email="owner@accranorth.example",
        password="StrongPassword123!",
    ):
        client = client or self.client
        with override_settings(IPINFO_TOKEN="test-token"):
            with patch("mainapp.integrations.geolocation.urllib.request.urlopen") as mock_urlopen:
                mock_urlopen.return_value = make_mock_urlopen(
                    json.dumps(CLEAR_GH_PAYLOAD).encode("utf-8")
                )
                signup_res = client.post(
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
        verify_res = client.post(
            "/api/v1/verify-email/",
            data=json.dumps({"token": verify_token}),
            content_type="application/json",
        )
        self.assertEqual(verify_res.status_code, 200)
        mail.outbox.clear()
        workspace = Workspace.objects.get(slug=signup_res.json()["workspace"]["slug"])
        user = User.objects.select_related("role", "workspace").get(email=email)
        return workspace, user

    def _invite_and_accept_staff(
        self,
        owner_client,
        workspace,
        *,
        staff_email="staff@accranorth.example",
        password="StaffPassword123!",
        timezone_name="Africa/Accra",
    ):
        invite_res = owner_client.post(
            f"/api/v1/workspaces/{workspace.slug}/invites/",
            data=json.dumps({"email": staff_email}),
            content_type="application/json",
        )
        self.assertEqual(invite_res.status_code, 201)
        invite_token = extract_token_from_email_body(mail.outbox[-1].body)
        mail.outbox.clear()

        staff_client = Client()
        accept_res = staff_client.post(
            "/api/v1/invites/accept/",
            data=json.dumps(
                {
                    "token": invite_token,
                    "password": password,
                    "timezone": timezone_name,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(accept_res.status_code, 200)
        staff_user = User.objects.select_related("role", "workspace").get(email=staff_email)
        return staff_client, staff_user

    def _oauth_callback_login(
        self,
        client,
        provider: str,
        *,
        email: str,
        subject: str,
        kid: str = "s15-kid-1",
    ):
        rsa_key, jwks_data = _make_rsa_jwk_and_private_key(kid)
        start_res = client.get(f"/api/v1/auth/{provider}/start/")
        self.assertEqual(start_res.status_code, 302)
        auth_params = parse_qs(urlparse(start_res["Location"]).query)
        state = auth_params["state"][0]
        nonce = auth_params["nonce"][0]

        if provider == "google":
            claims = {
                "iss": "https://accounts.google.com",
                "aud": "client-id-123",
                "sub": subject,
                "nonce": nonce,
                "email": email,
                "email_verified": True,
                "exp": int(time.time()) + 300,
                "iat": int(time.time()),
            }
        else:
            ms_tenant = "9188040d-6c67-4c5b-b112-36a304b66dad"
            claims = {
                "iss": f"https://login.microsoftonline.com/{ms_tenant}/v2.0",
                "tid": ms_tenant,
                "aud": "client-id-123",
                "sub": subject,
                "nonce": nonce,
                "email": email,
                "xms_edov": True,
                "exp": int(time.time()) + 300,
                "iat": int(time.time()),
            }
        id_token = jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": kid})

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
            return client.get(
                f"/api/v1/auth/{provider}/callback/",
                {"code": "auth-code-xyz", "state": state},
            )

    def test_owner_and_staff_roles_created_distinguished_and_owner_protected(self):
        workspace, owner = self._create_verified_workspace()
        staff_client, staff_user = self._invite_and_accept_staff(self.client, workspace)

        workspace.refresh_from_db()
        self.assertEqual(workspace.created_by_id, owner.id)
        self.assertIsNotNone(owner.role)
        self.assertTrue(owner.role.is_owner)
        self.assertEqual(owner.role.name, "Owner")
        self.assertIsNotNone(staff_user.role)
        self.assertTrue(staff_user.role.is_default_staff)
        self.assertEqual(staff_user.role.name, "Staff")

        # Roles directory distinguishes Owner vs default Staff cleanly
        roles_res = self.client.get(f"/api/v1/workspaces/{workspace.slug}/roles/")
        self.assertEqual(roles_res.status_code, 200)
        roles_body = roles_res.json()
        self.assertEqual(roles_body["active_seat_count"], 2)
        roles_by_name = {item["name"]: item for item in roles_body["roles"]}
        self.assertIn("Owner", roles_by_name)
        self.assertIn("Staff", roles_by_name)

        owner_role = roles_by_name["Owner"]
        staff_role = roles_by_name["Staff"]
        self.assertTrue(owner_role["is_owner"])
        self.assertFalse(owner_role["is_removable"])
        self.assertFalse(owner_role["is_deactivatable"])
        self.assertTrue(owner_role["can_manage_seat_plan"])
        self.assertTrue(owner_role["can_manage_sms_wallet"])
        self.assertTrue(all(owner_role["switches"].values()))
        self.assertEqual(owner_role["active_member_count"], 1)

        self.assertTrue(staff_role["is_default_staff"])
        self.assertFalse(staff_role["is_owner"])
        self.assertFalse(staff_role["is_removable"])
        self.assertFalse(staff_role["can_manage_seat_plan"])
        self.assertFalse(staff_role["can_manage_sms_wallet"])
        self.assertFalse(any(staff_role["switches"].values()))
        self.assertEqual(staff_role["active_member_count"], 1)

        # Owner cannot be deactivated or removed even by themselves or a delegated role
        deact_owner = self.client.post(
            f"/api/v1/workspaces/{workspace.slug}/members/{owner.id}/deactivate/"
        )
        self.assertEqual(deact_owner.status_code, 403)
        del_owner = self.client.delete(
            f"/api/v1/workspaces/{workspace.slug}/members/{owner.id}/"
        )
        self.assertEqual(del_owner.status_code, 403)

        # Plain Staff cannot deactivate/reactivate/remove members
        staff_deact = staff_client.post(
            f"/api/v1/workspaces/{workspace.slug}/members/{owner.id}/deactivate/"
        )
        self.assertEqual(staff_deact.status_code, 403)

        # Delegated role with users_and_roles=True can manage non-Owners, not Owner,
        # and does NOT gain seat plan or SMS wallet authority.
        manager_role = Role.objects.create(
            workspace=workspace,
            name="People Manager",
            users_and_roles=True,
        )
        staff_user.role = manager_role
        staff_user.save(update_fields=["role"])

        self.assertEqual(
            staff_client.post(
                f"/api/v1/workspaces/{workspace.slug}/members/{owner.id}/deactivate/"
            ).status_code,
            403,
        )
        session_info = staff_client.get("/api/v1/auth/session/").json()
        self.assertTrue(session_info["permissions"]["can_manage_users_and_roles"])
        self.assertFalse(session_info["permissions"]["can_manage_seat_plan"])
        self.assertFalse(session_info["permissions"]["can_manage_sms_wallet"])

        other_client, other_staff = self._invite_and_accept_staff(
            staff_client,
            workspace,
            staff_email="other@accranorth.example",
        )
        self.assertEqual(
            staff_client.post(
                f"/api/v1/workspaces/{workspace.slug}/members/{other_staff.id}/deactivate/"
            ).status_code,
            200,
        )
        self.assertEqual(
            other_client.get(f"/api/v1/workspaces/{workspace.slug}/").status_code,
            401,
        )
        self.assertEqual(
            staff_client.post(
                f"/api/v1/workspaces/{workspace.slug}/members/{other_staff.id}/reactivate/"
            ).status_code,
            200,
        )

    def test_deactivate_ends_sessions_blocks_all_sign_in_and_reactivate_restores(self):
        workspace, owner = self._create_verified_workspace()
        staff_client, staff_user = self._invite_and_accept_staff(self.client, workspace)

        # Confirm Staff has an active DB session and both count as paid seats
        self.assertGreaterEqual(Session.objects.count(), 2)
        people_before = self.client.get(f"/api/v1/workspaces/{workspace.slug}/people/").json()
        self.assertEqual(people_before["active_seat_count"], 2)

        # Register a passkey for Staff so we prove passkey login is also blocked on deactivation
        reg_opts_res = staff_client.post(
            "/api/v1/auth/passkey/register/options/",
            data=json.dumps({"name": "Staff Laptop"}),
            content_type="application/json",
        )
        self.assertEqual(reg_opts_res.status_code, 200)
        private_key, cred_id, reg_cred = _build_webauthn_registration_payload(
            reg_opts_res.json()["options"]
        )
        self.assertEqual(
            staff_client.post(
                "/api/v1/auth/passkey/register/verify/",
                data=json.dumps({"credential": reg_cred, "name": "Staff Laptop"}),
                content_type="application/json",
            ).status_code,
            201,
        )

        # Link Google to Staff before deactivation so we can prove OAuth login is blocked
        with override_settings(
            GOOGLE_OAUTH_CLIENT_ID="client-id-123",
            GOOGLE_OAUTH_CLIENT_SECRET="secret-123",
        ):
            link_res = self._oauth_callback_login(
                staff_client,
                "google",
                email=staff_user.email,
                subject="google-staff-sub-1",
            )
            self.assertEqual(link_res.status_code, 302)

        # Owner deactivates Staff
        initial_session_version = staff_user.session_version
        deact_res = self.client.post(
            f"/api/v1/workspaces/{workspace.slug}/members/{staff_user.id}/deactivate/"
        )
        self.assertEqual(deact_res.status_code, 200)
        self.assertEqual(deact_res.json()["active_seat_count"], 1)
        self.assertFalse(deact_res.json()["member"]["is_active"])
        self.assertFalse(deact_res.json()["member"]["counts_as_active_seat"])

        staff_user.refresh_from_db()
        self.assertFalse(staff_user.is_active)
        self.assertEqual(staff_user.session_version, initial_session_version + 1)

        # Existing Staff session is immediately dead across auth/session and workspace APIs
        self.assertEqual(
            staff_client.get("/api/v1/auth/session/").json()["authenticated"],
            False,
        )
        self.assertEqual(
            staff_client.get(f"/api/v1/workspaces/{workspace.slug}/").status_code,
            401,
        )

        # Password sign-in is blocked (403 on valid password, 401 on wrong password)
        bad_pw_login = Client().post(
            "/api/v1/login/",
            data=json.dumps({"email": staff_user.email, "password": "WrongPassword!"}),
            content_type="application/json",
        )
        self.assertEqual(bad_pw_login.status_code, 401)
        right_pw_login = Client().post(
            "/api/v1/login/",
            data=json.dumps({"email": staff_user.email, "password": "StaffPassword123!"}),
            content_type="application/json",
        )
        self.assertEqual(right_pw_login.status_code, 403)

        # Google OAuth sign-in is blocked
        with override_settings(
            GOOGLE_OAUTH_CLIENT_ID="client-id-123",
            GOOGLE_OAUTH_CLIENT_SECRET="secret-123",
        ):
            oauth_blocked = self._oauth_callback_login(
                Client(),
                "google",
                email=staff_user.email,
                subject="google-staff-sub-1",
            )
            self.assertEqual(oauth_blocked.status_code, 302)
            self.assertIn("auth_error=", oauth_blocked["Location"])

        # Passkey sign-in is blocked (both email-scoped options and usernameless verify)
        pk_client = Client()
        email_opts_res = pk_client.post(
            "/api/v1/auth/passkey/login/options/",
            data=json.dumps({"email": staff_user.email}),
            content_type="application/json",
        )
        self.assertEqual(email_opts_res.status_code, 400)

        opts_res = pk_client.post(
            "/api/v1/auth/passkey/login/options/",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(opts_res.status_code, 200)
        assertion = _build_webauthn_assertion_payload(
            private_key, cred_id, opts_res.json()["options"], sign_count=2
        )
        pk_blocked = pk_client.post(
            "/api/v1/auth/passkey/login/verify/",
            data=json.dumps({"credential": assertion}),
            content_type="application/json",
        )
        self.assertIn(pk_blocked.status_code, (401, 403))
        self.assertEqual(
            pk_client.get(f"/api/v1/workspaces/{workspace.slug}/").status_code,
            401,
        )

        # Password reset request skips deactivated user (no email sent)
        self.assertEqual(
            Client()
            .post(
                "/api/v1/password-reset/request/",
                data=json.dumps({"email": staff_user.email}),
                content_type="application/json",
            )
            .status_code,
            200,
        )
        self.assertEqual(len(mail.outbox), 0)

        # Reactivate restores access and active paid seat count
        react_res = self.client.post(
            f"/api/v1/workspaces/{workspace.slug}/members/{staff_user.id}/reactivate/"
        )
        self.assertEqual(react_res.status_code, 200)
        self.assertEqual(react_res.json()["active_seat_count"], 2)
        self.assertTrue(react_res.json()["member"]["is_active"])
        self.assertTrue(react_res.json()["member"]["counts_as_active_seat"])

        restored_client = Client()
        login_again = restored_client.post(
            "/api/v1/login/",
            data=json.dumps({"email": staff_user.email, "password": "StaffPassword123!"}),
            content_type="application/json",
        )
        self.assertEqual(login_again.status_code, 200)
        self.assertEqual(
            restored_client.get(f"/api/v1/workspaces/{workspace.slug}/").status_code,
            200,
        )

    def test_settings_password_timezone_and_oauth_unlink_keep_login_working(self):
        workspace, owner = self._create_verified_workspace()

        # Timezone validation and update
        self.assertEqual(
            self.client.patch(
                "/api/v1/account/profile/",
                data=json.dumps({"timezone": "Mars/Olympus"}),
                content_type="application/json",
            ).status_code,
            400,
        )
        tz_res = self.client.patch(
            "/api/v1/account/profile/",
            data=json.dumps({"timezone": "Europe/London"}),
            content_type="application/json",
        )
        self.assertEqual(tz_res.status_code, 200)
        self.assertEqual(tz_res.json()["user"]["timezone"], "Europe/London")
        session_after_tz = self.client.get("/api/v1/auth/session/").json()
        self.assertTrue(session_after_tz["authenticated"])
        self.assertEqual(session_after_tz["workspace"]["slug"], workspace.slug)
        self.assertEqual(session_after_tz["user"]["timezone"], "Europe/London")

        # Password change verifies current password, keeps current session valid, swaps password
        self.assertEqual(
            self.client.post(
                "/api/v1/account/password/change/",
                data=json.dumps(
                    {
                        "current_password": "WrongPassword123!",
                        "new_password": "UpdatedPassword456!",
                    }
                ),
                content_type="application/json",
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/account/password/change/",
                data=json.dumps(
                    {
                        "current_password": "StrongPassword123!",
                        "new_password": "StrongPassword123!",
                    }
                ),
                content_type="application/json",
            ).status_code,
            400,
        )
        pw_ok = self.client.post(
            "/api/v1/account/password/change/",
            data=json.dumps(
                {
                    "current_password": "StrongPassword123!",
                    "new_password": "UpdatedPassword456!",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(pw_ok.status_code, 200)
        # Current session remains authenticated after update_session_auth_hash
        self.assertTrue(self.client.get("/api/v1/auth/session/").json()["authenticated"])
        self.assertEqual(
            Client()
            .post(
                "/api/v1/login/",
                data=json.dumps({"email": owner.email, "password": "StrongPassword123!"}),
                content_type="application/json",
            )
            .status_code,
            401,
        )
        self.assertEqual(
            Client()
            .post(
                "/api/v1/login/",
                data=json.dumps({"email": owner.email, "password": "UpdatedPassword456!"}),
                content_type="application/json",
            )
            .status_code,
            200,
        )

        # Link Google & Microsoft, then unlink Google without affecting password login
        with override_settings(
            GOOGLE_OAUTH_CLIENT_ID="client-id-123",
            GOOGLE_OAUTH_CLIENT_SECRET="secret-123",
            MICROSOFT_OAUTH_CLIENT_ID="client-id-123",
            MICROSOFT_OAUTH_CLIENT_SECRET="secret-123",
        ):
            self.assertEqual(
                self._oauth_callback_login(
                    self.client,
                    "google",
                    email=owner.email,
                    subject="google-owner-sub",
                ).status_code,
                302,
            )
            self.assertEqual(
                self._oauth_callback_login(
                    self.client,
                    "microsoft",
                    email=owner.email,
                    subject="ms-owner-sub",
                ).status_code,
                302,
            )

        methods_before = self.client.get("/api/v1/auth/methods/").json()
        self.assertTrue(methods_before["providers"]["google"]["linked"])
        self.assertTrue(methods_before["providers"]["microsoft"]["linked"])

        unlink_res = self.client.delete("/api/v1/auth/providers/google/")
        self.assertEqual(unlink_res.status_code, 200)
        self.assertTrue(unlink_res.json()["has_password"])

        methods_after = self.client.get("/api/v1/auth/methods/").json()
        self.assertFalse(methods_after["providers"]["google"]["linked"])
        self.assertTrue(methods_after["providers"]["microsoft"]["linked"])
        self.assertFalse(methods_after["mailbox_connection"]["connected"])
        self.assertEqual(
            ExternalIdentity.objects.filter(user=owner, provider="google").count(),
            0,
        )

        # Password login still works after unlinking Google
        self.assertEqual(
            Client()
            .post(
                "/api/v1/login/",
                data=json.dumps({"email": owner.email, "password": "UpdatedPassword456!"}),
                content_type="application/json",
            )
            .status_code,
            200,
        )

        # Re-linking Google with the same verified email succeeds
        with override_settings(
            GOOGLE_OAUTH_CLIENT_ID="client-id-123",
            GOOGLE_OAUTH_CLIENT_SECRET="secret-123",
        ):
            relink = self._oauth_callback_login(
                Client(),
                "google",
                email=owner.email,
                subject="google-owner-sub",
            )
            self.assertEqual(relink.status_code, 302)
            self.assertEqual(
                ExternalIdentity.objects.filter(user=owner, provider="google").count(),
                1,
            )

        # Safety check: cannot unlink the last sign-in method when password is unusable
        ExternalIdentity.objects.filter(user=owner, provider="google").delete()
        owner.set_unusable_password()
        owner.save(update_fields=["password"])
        ms_only_client = Client()
        with override_settings(
            MICROSOFT_OAUTH_CLIENT_ID="client-id-123",
            MICROSOFT_OAUTH_CLIENT_SECRET="secret-123",
        ):
            self.assertEqual(
                self._oauth_callback_login(
                    ms_only_client,
                    "microsoft",
                    email=owner.email,
                    subject="ms-owner-sub",
                ).status_code,
                302,
            )
        self.assertEqual(
            ms_only_client.delete("/api/v1/auth/providers/microsoft/").status_code,
            400,
        )

    def test_audit_log_records_person_actor_time_values_scoping_and_isolation(self):
        workspace, owner = self._create_verified_workspace()
        staff_client, staff_user = self._invite_and_accept_staff(self.client, workspace)

        # Perform Owner-triggered mutations + Staff-triggered self update + future actor kinds
        self.client.patch(
            "/api/v1/account/profile/",
            data=json.dumps({"timezone": "Europe/London"}),
            content_type="application/json",
        )
        staff_client.patch(
            "/api/v1/account/profile/",
            data=json.dumps({"timezone": "America/New_York"}),
            content_type="application/json",
        )
        self.client.post(
            f"/api/v1/workspaces/{workspace.slug}/members/{staff_user.id}/deactivate/"
        )
        # Reactivate restores access; re-sign in so Staff can query their scoped audit feed
        self.client.post(
            f"/api/v1/workspaces/{workspace.slug}/members/{staff_user.id}/reactivate/"
        )
        self.assertEqual(
            staff_client.post(
                "/api/v1/login/",
                data=json.dumps({"email": staff_user.email, "password": "StaffPassword123!"}),
                content_type="application/json",
            ).status_code,
            200,
        )

        # Prove future non-person actor kinds serialize and filter cleanly
        record_audit_event(
            workspace=workspace,
            object_type="deal",
            object_id="12",
            record_label="Tema Port Expansion",
            action=AuditLog.ACTION_UPDATE,
            field_name="stage",
            previous_value="Discovery",
            new_value="Proposal",
            actor_kind=AuditLog.ACTOR_KIND_AUTOMATION,
            actor_label="Auto-stage rule",
        )

        owner_audit = self.client.get(
            f"/api/v1/workspaces/{workspace.slug}/audit/?page=1&page_size=50"
        )
        self.assertEqual(owner_audit.status_code, 200)
        owner_entries = owner_audit.json()["results"]
        self.assertGreaterEqual(len(owner_entries), 6)

        tz_entry = next(
            e
            for e in owner_entries
            if e["field_name"] == "timezone" and e["actor"] == owner.email
        )
        self.assertEqual(tz_entry["actor_kind"], "person")
        self.assertEqual(tz_entry["previous_value"], "Africa/Accra")
        self.assertEqual(tz_entry["new_value"], "Europe/London")
        self.assertTrue(tz_entry["created_at"])

        status_deact = next(
            e
            for e in owner_entries
            if e["field_name"] == "status"
            and e["record_label"] == staff_user.email
            and e["new_value"] == "Deactivated"
        )
        self.assertEqual(status_deact["actor"], owner.email)
        self.assertEqual(status_deact["actor_kind"], "person")
        self.assertEqual(status_deact["previous_value"], "Active")

        # Filter by actor_kind=automation returns only automation entries
        auto_audit = self.client.get(
            f"/api/v1/workspaces/{workspace.slug}/audit/?actor_kind=automation"
        ).json()
        self.assertEqual(auto_audit["total_count"], 1)
        self.assertEqual(auto_audit["results"][0]["actor_kind"], "automation")

        # Staff scope excludes invites and Owner's private account/timezone changes,
        # while including Staff's own events and general record events.
        staff_audit = staff_client.get(
            f"/api/v1/workspaces/{workspace.slug}/audit/?page_size=50"
        )
        self.assertEqual(staff_audit.status_code, 200)
        staff_entries = staff_audit.json()["results"]
        self.assertFalse(
            any(
                e["object_type"] == "workspace_invite"
                and e["actor_user_id"] != staff_user.id
                for e in staff_entries
            )
        )
        self.assertFalse(
            any(
                e["field_name"] == "timezone" and e["actor"] == owner.email
                for e in staff_entries
            )
        )
        self.assertTrue(
            any(
                e["field_name"] == "timezone" and e["actor"] == staff_user.email
                for e in staff_entries
            )
        )
        self.assertTrue(any(e["object_type"] == "deal" for e in staff_entries))

        # Backfill helper creates Owner and Staff roles idempotently for legacy workspaces
        legacy_ws = Workspace.objects.create(name="Legacy Co", slug="legacy-co")
        legacy_owner = User.objects.create_user(
            email="legacy@co.example",
            workspace=legacy_ws,
            password="LegacyPassword123!",
            email_verified=True,
            email_verified_at=timezone.now(),
        )
        legacy_ws.created_by = legacy_owner
        legacy_ws.save(update_fields=["created_by"])
        l_owner_role, l_staff_role = ensure_workspace_default_roles(legacy_ws)
        legacy_owner.refresh_from_db()
        self.assertEqual(legacy_owner.role_id, l_owner_role.id)
        self.assertTrue(l_staff_role.is_default_staff)

        # Cross-workspace isolation
        self.assertEqual(
            self.client.get(f"/api/v1/workspaces/{legacy_ws.slug}/roles/").status_code,
            404,
        )
        self.assertEqual(
            self.client.get(f"/api/v1/workspaces/{legacy_ws.slug}/audit/").status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                f"/api/v1/workspaces/{legacy_ws.slug}/members/{legacy_owner.id}/deactivate/"
            ).status_code,
            404,
        )

    def _request_reset_token(self, email):
        mail.outbox.clear()
        res = Client().post(
            "/api/v1/password-reset/request/",
            data=json.dumps({"email": email}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        return extract_token_from_email_body(mail.outbox[-1].body)

    def test_password_change_revokes_other_sessions_and_unused_reset_tokens(self):
        workspace, owner = self._create_verified_workspace()
        second_client = Client()
        self.assertEqual(
            second_client.post(
                "/api/v1/login/",
                data=json.dumps({"email": owner.email, "password": "StrongPassword123!"}),
                content_type="application/json",
            ).status_code,
            200,
        )
        self._request_reset_token(owner.email)
        self.assertTrue(
            EmailActionToken.objects.filter(
                user=owner,
                purpose=EmailActionToken.PURPOSE_RESET_PASSWORD,
                used_at__isnull=True,
            ).exists()
        )
        version_before = User.objects.get(pk=owner.pk).session_version

        res = self.client.post(
            "/api/v1/account/password/change/",
            data=json.dumps(
                {
                    "current_password": "StrongPassword123!",
                    "new_password": "UpdatedPassword456!",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            User.objects.get(pk=owner.pk).session_version, version_before + 1
        )
        # The caller stays signed in; the other browser is signed out.
        self.assertTrue(self.client.get("/api/v1/auth/session/").json()["authenticated"])
        self.assertFalse(second_client.get("/api/v1/auth/session/").json()["authenticated"])
        self.assertEqual(
            second_client.get(f"/api/v1/workspaces/{workspace.slug}/").status_code,
            401,
        )
        # Outstanding reset links are retired.
        self.assertFalse(
            EmailActionToken.objects.filter(
                user=owner,
                purpose=EmailActionToken.PURPOSE_RESET_PASSWORD,
                used_at__isnull=True,
            ).exists()
        )

    def test_password_reset_confirm_revokes_existing_sessions(self):
        _workspace, owner = self._create_verified_workspace()
        version_before = User.objects.get(pk=owner.pk).session_version
        reset_token = self._request_reset_token(owner.email)

        resetting_client = Client()
        res = resetting_client.post(
            "/api/v1/password-reset/confirm/",
            data=json.dumps({"token": reset_token, "password": "ResetPassword789!"}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            User.objects.get(pk=owner.pk).session_version, version_before + 1
        )
        self.assertTrue(
            resetting_client.get("/api/v1/auth/session/").json()["authenticated"]
        )
        # The session that existed before the reset no longer works.
        self.assertFalse(self.client.get("/api/v1/auth/session/").json()["authenticated"])

    def test_unverified_session_signup_returns_verification_gate(self):
        with override_settings(IPINFO_TOKEN="test-token"):
            with patch("mainapp.integrations.geolocation.urllib.request.urlopen") as mock_urlopen:
                mock_urlopen.return_value = make_mock_urlopen(
                    json.dumps(CLEAR_GH_PAYLOAD).encode("utf-8")
                )
                first = self.client.post(
                    "/api/v1/signup/",
                    data=json.dumps(
                        {
                            "workspace_name": "Pending Works",
                            "business_type": "general_services",
                            "email": "pending@works.example",
                            "password": "StrongPassword123!",
                        }
                    ),
                    content_type="application/json",
                    REMOTE_ADDR="154.160.1.1",
                )
                self.assertEqual(first.status_code, 201)

                pending_user = User.objects.get(email="pending@works.example")
                self.client.force_login(pending_user)
                session_body = self.client.get("/api/v1/auth/session/").json()
                self.assertFalse(session_body["authenticated"])
                self.assertTrue(session_body["verification_required"])
                self.assertEqual(session_body["email"], "pending@works.example")
                self.assertEqual(session_body["workspace"]["slug"], "pending-works")

                second = self.client.post(
                    "/api/v1/signup/",
                    data=json.dumps(
                        {
                            "workspace_name": "Second Works",
                            "business_type": "general_services",
                            "email": "second@works.example",
                            "password": "StrongPassword123!",
                        }
                    ),
                    content_type="application/json",
                    REMOTE_ADDR="154.160.1.1",
                )
        self.assertEqual(second.status_code, 403)
        body = second.json()
        self.assertTrue(body["verification_required"])
        self.assertEqual(body["email"], "pending@works.example")
        self.assertEqual(body["workspace"]["slug"], "pending-works")
        self.assertFalse(Workspace.objects.filter(slug="second-works").exists())
