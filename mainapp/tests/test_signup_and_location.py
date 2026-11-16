import json
import socket
from unittest.mock import patch

from django.contrib.auth.hashers import check_password
from django.core import mail
from django.test import TestCase, override_settings

from ..integrations.geolocation import LOCATION_REFUSAL_MESSAGE
from ..models import User, Workspace
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
class SignupLocationAndWorkspaceTests(TestCase):
    def _post_signup(self, data=None, **extra):
        payload = {
            "workspace_name": "Accra North Supply",
            "business_type": "general_services",
            "email": "founder@accranorth.example",
            "password": "StrongPassword123!",
        }
        if data is not None:
            payload.update(data)
        return self.client.post(
            "/api/v1/signup/",
            data=json.dumps(payload),
            content_type="application/json",
            REMOTE_ADDR=extra.pop("REMOTE_ADDR", "154.160.1.1"),
            **extra,
        )

    @patch("mainapp.integrations.geolocation.urllib.request.urlopen")
    def test_clear_gh_creates_workspace_sends_verification_and_opens_after_verify(
        self, mock_urlopen
    ):
        mock_urlopen.return_value = make_mock_urlopen(
            json.dumps(CLEAR_GH_PAYLOAD).encode("utf-8")
        )

        response = self._post_signup()
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["workspace"]["name"], "Accra North Supply")
        self.assertEqual(body["workspace"]["slug"], "accra-north-supply")
        self.assertEqual(body["workspace"]["business_type"], "general_services")
        self.assertEqual(body["workspace"]["business_type_label"], "General services")
        self.assertEqual(body["workspace"]["contact_label"], "Contact")
        self.assertEqual(body["workspace"]["contact_plural_label"], "Contacts")
        self.assertEqual(body["workspace"]["active_table_count"], 17)
        self.assertEqual(body["workspace"]["core_table_count"], 5)
        self.assertEqual(body["workspace"]["pack_table_count"], 12)
        self.assertEqual(
            body["user"],
            {
                "email": "founder@accranorth.example",
                "timezone": "Africa/Accra",
                "email_verified": False,
            },
        )
        self.assertTrue(body["verification_required"])

        self.assertEqual(Workspace.objects.count(), 1)
        self.assertEqual(User.objects.count(), 1)
        workspace = Workspace.objects.get()
        user = User.objects.get()
        self.assertEqual(workspace.slug, "accra-north-supply")
        self.assertEqual(workspace.created_by, user)
        self.assertEqual(list(workspace.users.all()), [user])
        self.assertEqual(user.timezone, "Africa/Accra")
        self.assertFalse(user.email_verified)
        self.assertTrue(check_password("StrongPassword123!", user.password))
        self.assertNotEqual(user.password, "StrongPassword123!")

        # Verification message was sent
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["founder@accranorth.example"])
        raw_token = extract_token_from_email_body(mail.outbox[0].body)

        # Workspace stays closed before verification
        closed_ws = self.client.get("/api/v1/workspaces/accra-north-supply/")
        self.assertIn(closed_ws.status_code, (401, 403))

        # Using the verification link opens the workspace
        verify_res = self.client.post(
            "/api/v1/verify-email/",
            data=json.dumps({"token": raw_token}),
            content_type="application/json",
        )
        self.assertEqual(verify_res.status_code, 200)
        self.assertTrue(verify_res.json()["user"]["email_verified"])

        ws_response = self.client.get("/api/v1/workspaces/accra-north-supply/")
        self.assertEqual(ws_response.status_code, 200)
        self.assertEqual(ws_response.json()["workspace"]["slug"], "accra-north-supply")

    @patch("mainapp.integrations.geolocation.urllib.request.urlopen")
    def test_refusal_cases_return_identical_message_and_create_no_rows(
        self, mock_urlopen
    ):
        failure_cases = [
            (
                "NG country",
                lambda: make_mock_urlopen(
                    json.dumps(
                        {
                            "ip": "102.89.1.1",
                            "country": "NG",
                            "privacy": {
                                "vpn": False,
                                "proxy": False,
                                "tor": False,
                                "relay": False,
                                "hosting": False,
                            },
                        }
                    ).encode("utf-8")
                ),
            ),
            (
                "unknown / missing country",
                lambda: make_mock_urlopen(
                    json.dumps(
                        {
                            "ip": "154.160.1.1",
                            "privacy": {
                                "vpn": False,
                                "proxy": False,
                                "tor": False,
                                "relay": False,
                                "hosting": False,
                            },
                        }
                    ).encode("utf-8")
                ),
            ),
            (
                "timeout",
                lambda: (_ for _ in ()).throw(socket.timeout("timed out")),
            ),
            (
                "malformed body",
                lambda: make_mock_urlopen(b"{not-valid-json"),
            ),
            (
                "GH plus VPN",
                lambda: make_mock_urlopen(
                    json.dumps(
                        {
                            "ip": "154.160.1.2",
                            "country": "GH",
                            "privacy": {
                                "vpn": True,
                                "proxy": False,
                                "tor": False,
                                "relay": False,
                                "hosting": False,
                            },
                        }
                    ).encode("utf-8")
                ),
            ),
            (
                "GH plus hosting",
                lambda: make_mock_urlopen(
                    json.dumps(
                        {
                            "ip": "154.160.1.3",
                            "country": "GH",
                            "privacy": {
                                "vpn": False,
                                "proxy": False,
                                "tor": False,
                                "relay": False,
                                "hosting": True,
                            },
                        }
                    ).encode("utf-8")
                ),
            ),
            (
                "GH with missing anonymity field",
                lambda: make_mock_urlopen(
                    json.dumps(
                        {
                            "ip": "154.160.1.4",
                            "country": "GH",
                            "privacy": {
                                "vpn": False,
                                "proxy": False,
                                "tor": False,
                                "relay": False,
                            },
                        }
                    ).encode("utf-8")
                ),
            ),
        ]

        expected_body = {"error": LOCATION_REFUSAL_MESSAGE}

        for label, side_effect_fn in failure_cases:
            with self.subTest(case=label):
                mock_urlopen.side_effect = lambda *_args, **_kwargs: side_effect_fn()
                response = self._post_signup(
                    data={
                        "workspace_name": "",
                        "slug": "admin",
                        "email": "not-an-email",
                    }
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json(), expected_body)
                self.assertEqual(Workspace.objects.count(), 0)
                self.assertEqual(User.objects.count(), 0)

    @patch("mainapp.integrations.geolocation.urllib.request.urlopen")
    def test_client_cannot_pass_country_or_ip(self, mock_urlopen):
        def inspect_request(req, timeout=None):
            self.assertIn("198.51.100.77", req.full_url)
            return make_mock_urlopen(
                json.dumps(
                    {
                        "ip": "198.51.100.77",
                        "country": "US",
                        "privacy": {
                            "vpn": False,
                            "proxy": False,
                            "tor": False,
                            "relay": False,
                            "hosting": False,
                        },
                    }
                ).encode("utf-8")
            )

        mock_urlopen.side_effect = inspect_request

        response = self._post_signup(
            data={
                "country": "GH",
                "ip": "154.160.1.1",
                "GEO_LOOKUP_IP": "154.160.1.1",
            },
            REMOTE_ADDR="198.51.100.77",
            HTTP_X_FORWARDED_FOR="154.160.1.1",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"error": LOCATION_REFUSAL_MESSAGE})
        self.assertEqual(Workspace.objects.count(), 0)

    @patch("mainapp.integrations.geolocation.urllib.request.urlopen")
    def test_geo_lookup_ip_ignored_when_debug_is_false_and_used_when_debug_is_true(
        self, mock_urlopen
    ):
        seen_urls = []

        def fake_urlopen(req, timeout=None):
            seen_urls.append(req.full_url)
            return make_mock_urlopen(json.dumps(CLEAR_GH_PAYLOAD).encode("utf-8"))

        mock_urlopen.side_effect = fake_urlopen

        with override_settings(DEBUG=False, GEO_LOOKUP_IP="154.160.99.99"):
            self._post_signup(REMOTE_ADDR="192.0.2.55")
            self.assertIn("192.0.2.55", seen_urls[-1])
            self.assertNotIn("154.160.99.99", seen_urls[-1])

        Workspace.objects.all().delete()
        User.objects.all().delete()

        with override_settings(DEBUG=True, GEO_LOOKUP_IP="154.160.99.99"):
            self._post_signup(REMOTE_ADDR="192.0.2.55")
            self.assertIn("154.160.99.99", seen_urls[-1])

    @patch("mainapp.integrations.geolocation.urllib.request.urlopen")
    def test_trusted_proxy_forwards_client_ip_only_when_configured(self, mock_urlopen):
        seen_urls = []

        def fake_urlopen(req, timeout=None):
            seen_urls.append(req.full_url)
            return make_mock_urlopen(json.dumps(CLEAR_GH_PAYLOAD).encode("utf-8"))

        mock_urlopen.side_effect = fake_urlopen

        with override_settings(TRUSTED_PROXY_IPS=["10.0.0.1"]):
            self._post_signup(
                REMOTE_ADDR="10.0.0.1",
                HTTP_X_FORWARDED_FOR="154.160.50.10, 10.0.0.1",
            )
            self.assertIn("154.160.50.10", seen_urls[-1])

    @patch("mainapp.integrations.geolocation.urllib.request.urlopen")
    def test_validation_rejects_duplicate_slug_duplicate_email_empty_name_and_reserved_slugs(
        self, mock_urlopen
    ):
        mock_urlopen.return_value = make_mock_urlopen(
            json.dumps(CLEAR_GH_PAYLOAD).encode("utf-8")
        )

        # Create first workspace
        first = self._post_signup(
            data={
                "workspace_name": "Tema Logistics",
                "email": "owner@temalogistics.example",
            }
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(Workspace.objects.count(), 1)

        # Duplicate slug
        dup_slug = self._post_signup(
            data={
                "workspace_name": "Tema Logistics",
                "email": "second@temalogistics.example",
            }
        )
        self.assertEqual(dup_slug.status_code, 400)
        self.assertIn("slug", dup_slug.json()["error"].lower())
        self.assertNotEqual(dup_slug.json()["error"], LOCATION_REFUSAL_MESSAGE)
        self.assertEqual(Workspace.objects.count(), 1)

        # Duplicate email
        dup_email = self._post_signup(
            data={
                "workspace_name": "Kumasi Retail",
                "email": "OWNER@temalogistics.example",
            }
        )
        self.assertEqual(dup_email.status_code, 400)
        self.assertIn("email", dup_email.json()["error"].lower())
        self.assertNotEqual(dup_email.json()["error"], LOCATION_REFUSAL_MESSAGE)
        self.assertEqual(Workspace.objects.count(), 1)

        # Empty name
        empty_name = self._post_signup(
            data={
                "workspace_name": "   ",
                "email": "new@kumasi.example",
            }
        )
        self.assertEqual(empty_name.status_code, 400)
        self.assertEqual(Workspace.objects.count(), 1)

        # Reserved slugs
        for reserved in (
            "api",
            "admin",
            "static",
            "assets",
            "signup",
            "login",
            "verify-email",
            "forgot-password",
            "reset-password",
        ):
            with self.subTest(reserved_slug=reserved):
                res = self._post_signup(
                    data={
                        "workspace_name": reserved.replace("-", " ").title(),
                        "slug": reserved,
                        "email": f"user-{reserved}@example.com",
                    }
                )
                self.assertEqual(res.status_code, 400)
                self.assertIn("reserved", res.json()["error"].lower())
                self.assertEqual(Workspace.objects.count(), 1)

    @patch("mainapp.integrations.geolocation.urllib.request.urlopen")
    def test_validation_rejects_weak_common_and_email_similar_passwords_on_signup(
        self, mock_urlopen
    ):
        mock_urlopen.return_value = make_mock_urlopen(
            json.dumps(CLEAR_GH_PAYLOAD).encode("utf-8")
        )

        for weak_password in ("short1", "password", "12345678", "founder@accranorth"):
            with self.subTest(password=weak_password):
                response = self._post_signup(
                    data={
                        "workspace_name": "Kumasi Hardware",
                        "email": "founder@accranorth.example",
                        "password": weak_password,
                    }
                )
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.json()["error"])
                self.assertEqual(Workspace.objects.count(), 0)
                self.assertEqual(User.objects.count(), 0)

    @patch("mainapp.integrations.geolocation.urllib.request.urlopen")
    def test_signup_accepts_custom_slug(self, mock_urlopen):
        mock_urlopen.return_value = make_mock_urlopen(
            json.dumps(CLEAR_GH_PAYLOAD).encode("utf-8")
        )

        first = self._post_signup(
            data={
                "workspace_name": "Kora Industrial Ltd",
                "email": "first@kora.example",
            }
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json()["workspace"]["slug"], "kora-industrial-ltd")

        second = self._post_signup(
            data={
                "workspace_name": "Kora Industrial Ltd",
                "slug": "kora-accra",
                "email": "second@kora.example",
            }
        )
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.json()["workspace"]["slug"], "kora-accra")
        self.assertEqual(Workspace.objects.count(), 2)

    @patch("mainapp.integrations.geolocation.urllib.request.urlopen")
    def test_bypass_geo_check_setting_allows_signup_without_calling_ipinfo(
        self, mock_urlopen
    ):
        with override_settings(BYPASS_GEO_CHECK=True, IPINFO_TOKEN=""):
            response = self._post_signup(REMOTE_ADDR="8.8.8.8")
            self.assertEqual(response.status_code, 201)
            mock_urlopen.assert_not_called()
            self.assertEqual(Workspace.objects.count(), 1)
