import json
from unittest.mock import patch

from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, TestCase, override_settings
from redis.exceptions import ConnectionError as RedisConnectionError

from jojoCRM.settings import build_cache_settings
from mainapp.models import Account, Contact, Deal, Lead, Role, User, Workspace
from mainapp.service.catalog_service import (
    get_workspace_table_record_counts,
    serialize_workspace_catalog_summary,
)
from mainapp.service.role_service import ensure_workspace_default_roles
from mainapp.tests.helpers import (
    CLEAR_GH_PAYLOAD,
    extract_token_from_email_body,
    make_mock_urlopen,
)


@override_settings(
    IPINFO_TOKEN="test-token-do-not-log",
    GEO_LOOKUP_IP="",
    TRUSTED_PROXY_IPS=[],
    BYPASS_GEO_CHECK=False,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="noreply@jojocrm.example",
    EMAIL_HOST_USER="noreply@jojocrm.example",
    APP_BASE_URL="http://localhost:5173",
)
class ResponseAndWorkspaceCachingTests(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.gh_patcher = patch(
            "mainapp.integrations.geolocation.urllib.request.urlopen",
            side_effect=lambda *args, **kwargs: make_mock_urlopen(
                json.dumps(CLEAR_GH_PAYLOAD).encode("utf-8")
            ),
        )
        self.gh_patcher.start()
        self.addCleanup(self.gh_patcher.stop)
        self.addCleanup(cache.clear)

    def _create_verified_workspace(
        self,
        *,
        client: Client | None = None,
        workspace_name: str = "Kora Industrial Ltd",
        slug: str = "",
        business_type: str = "general_services",
        email: str = "owner@kora.example",
        password: str = "StrongPass!2026",
    ):
        from django.core import mail

        target_client = client or self.client
        mail.outbox.clear()
        payload = {
            "workspace_name": workspace_name,
            "business_type": business_type,
            "email": email,
            "password": password,
        }
        if slug:
            payload["slug"] = slug
        signup_res = target_client.post(
            "/api/v1/signup/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(signup_res.status_code, 201, signup_res.content)
        token = extract_token_from_email_body(mail.outbox[0].body)
        verify_res = target_client.post(
            "/api/v1/verify-email/",
            data=json.dumps({"token": token}),
            content_type="application/json",
        )
        self.assertEqual(verify_res.status_code, 200, verify_res.content)
        body = verify_res.json()
        ws = Workspace.objects.get(slug=body["workspace"]["slug"])
        user = User.objects.get(email=email.lower())
        return ws, user, body

    def _create_staff_member(
        self,
        workspace: Workspace,
        *,
        email: str,
        role: Role | None = None,
        password: str = "StaffPass!2026",
    ) -> tuple[User, Client]:
        _owner_role, staff_role = ensure_workspace_default_roles(workspace)
        user = User.objects.create_user(
            email=email,
            workspace=workspace,
            role=role or staff_role,
            password=password,
            email_verified=True,
            is_active=True,
        )
        client = Client()
        login_res = client.post(
            "/api/v1/login/",
            data=json.dumps({"email": email, "password": password}),
            content_type="application/json",
        )
        self.assertEqual(login_res.status_code, 200, login_res.content)
        return user, client

    def test_build_cache_settings_fails_closed_in_production_without_shared_backend(self):
        with self.assertRaises(ImproperlyConfigured):
            build_cache_settings(debug=False, running_tests=False)

        with self.assertRaises(ImproperlyConfigured):
            build_cache_settings(
                debug=False,
                running_tests=False,
                cache_backend="django.core.cache.backends.locmem.LocMemCache",
            )

        with self.assertRaises(ImproperlyConfigured):
            build_cache_settings(
                debug=False,
                running_tests=False,
                cache_backend="django.core.cache.backends.dummy.DummyCache",
            )

        prod_cfg = build_cache_settings(
            debug=False,
            running_tests=False,
            redis_url="redis://127.0.0.1:6379/2",
            cache_key_prefix="jojocrm_prod",
            cache_default_timeout=600,
        )
        self.assertEqual(
            prod_cfg["default"]["BACKEND"],
            "django.core.cache.backends.redis.RedisCache",
        )
        self.assertEqual(prod_cfg["default"]["LOCATION"], "redis://127.0.0.1:6379/2")
        self.assertEqual(prod_cfg["default"]["KEY_PREFIX"], "jojocrm_prod")
        self.assertEqual(prod_cfg["default"]["TIMEOUT"], 600)
        self.assertEqual(prod_cfg["default"]["OPTIONS"]["socket_timeout"], 1)

    def test_business_type_catalog_http_caching_and_no_reserialization_on_hit(self):
        first = self.client.get("/api/v1/catalog/business-types/")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first["X-Cache"], "MISS")
        self.assertIn("public", first["Cache-Control"])
        self.assertIn("max-age=3600", first["Cache-Control"])
        self.assertIn("Accept-Encoding", first["Vary"])
        etag = first["ETag"]
        self.assertTrue(etag.startswith('"') and etag.endswith('"'))
        first_body = first.json()
        self.assertEqual(len(first_body["business_types"]), 20)

        with patch(
            "mainapp.views.public_views.serialize_business_type_catalog",
            side_effect=AssertionError("Static catalog should not be re-serialized on cache hit"),
        ):
            second = self.client.get("/api/v1/catalog/business-types/")
            self.assertEqual(second.status_code, 200)
            self.assertEqual(second["X-Cache"], "HIT")
            self.assertEqual(second["ETag"], etag)
            self.assertEqual(second.get("Vary"), "Accept-Encoding")
            self.assertNotIn("Cookie", second.get("Vary", ""))
            self.assertGreaterEqual(len(second.json()["business_types"][0].get("starter_roles", [])), 3)
            self.assertEqual(second.json(), first_body)

            conditional_exact = self.client.get(
                "/api/v1/catalog/business-types/",
                HTTP_IF_NONE_MATCH=etag,
            )
            self.assertEqual(conditional_exact.status_code, 304)
            self.assertEqual(conditional_exact.content, b"")
            self.assertEqual(conditional_exact["ETag"], etag)
            self.assertEqual(conditional_exact["X-Cache"], "HIT")
            self.assertNotIn("text/html", conditional_exact.get("Content-Type", ""))
            self.assertIn("public", conditional_exact["Cache-Control"])

            conditional_weak = self.client.get(
                "/api/v1/catalog/business-types/",
                HTTP_IF_NONE_MATCH=f'"other", W/{etag}',
            )
            self.assertEqual(conditional_weak.status_code, 304)

            conditional_wildcard = self.client.get(
                "/api/v1/catalog/business-types/",
                HTTP_IF_NONE_MATCH="*",
            )
            self.assertEqual(conditional_wildcard.status_code, 304)

            mismatched = self.client.get(
                "/api/v1/catalog/business-types/",
                HTTP_IF_NONE_MATCH='"deadbeef0000"',
            )
            self.assertEqual(mismatched.status_code, 200)
            self.assertEqual(mismatched["X-Cache"], "HIT")

    def test_transient_cache_backend_error_degrades_safely_to_database(self):
        ws, _owner, _ = self._create_verified_workspace()
        Account.objects.create(workspace=ws, name="Accra Solar Ltd")

        with (
            patch(
                "mainapp.service.cache_service.cache.get",
                side_effect=RedisConnectionError("Redis offline"),
            ),
            patch(
                "mainapp.service.cache_service.cache.get_many",
                side_effect=RedisConnectionError("Redis offline"),
            ),
            patch(
                "mainapp.service.cache_service.cache.set",
                side_effect=RedisConnectionError("Redis offline"),
            ),
            patch(
                "mainapp.service.cache_service.cache.set_many",
                side_effect=RedisConnectionError("Redis offline"),
            ),
        ):
            catalog_res = self.client.get("/api/v1/catalog/business-types/")
            self.assertEqual(catalog_res.status_code, 200)
            self.assertEqual(catalog_res["X-Cache"], "MISS")

            dash_res = self.client.get(f"/api/v1/workspaces/{ws.slug}/dashboard/")
            self.assertEqual(dash_res.status_code, 200)
            self.assertEqual(dash_res["X-Cache"], "MISS")
            self.assertEqual(dash_res.json()["counts_by_object"]["account"], 1)

            opts_res = self.client.get(f"/api/v1/workspaces/{ws.slug}/crm/options/")
            self.assertEqual(opts_res.status_code, 200)
            self.assertEqual(opts_res["X-Cache"], "MISS")
            self.assertEqual(opts_res.json()["accounts"][0]["name"], "Accra Solar Ltd")

    def test_live_redis_cache_roundtrip_and_304_when_available(self):
        redis_cfg = {
            "default": {
                "BACKEND": "django.core.cache.backends.redis.RedisCache",
                "LOCATION": "redis://127.0.0.1:6379/15",
                "KEY_PREFIX": "jojocrm_test_s27",
                "TIMEOUT": 60,
                "OPTIONS": {"socket_connect_timeout": 1, "socket_timeout": 1},
            }
        }
        with override_settings(CACHES=redis_cfg):
            try:
                cache.clear()
            except Exception:
                self.skipTest("Local Redis server not reachable on 127.0.0.1:6379/15")
            try:
                res1 = self.client.get("/api/v1/catalog/business-types/")
                self.assertEqual(res1.status_code, 200)
                self.assertEqual(res1["X-Cache"], "MISS")

                res2 = self.client.get("/api/v1/catalog/business-types/")
                self.assertEqual(res2.status_code, 200)
                self.assertEqual(res2["X-Cache"], "HIT")
                self.assertEqual(res2["ETag"], res1["ETag"])

                res3 = self.client.get(
                    "/api/v1/catalog/business-types/",
                    HTTP_IF_NONE_MATCH=res1["ETag"],
                )
                self.assertEqual(res3.status_code, 304)
                self.assertEqual(res3["X-Cache"], "HIT")
            finally:
                cache.clear()

    def test_workspace_metadata_endpoints_cache_hits_query_reduction_and_304(self):
        ws, owner, _ = self._create_verified_workspace()
        acc = Account.objects.create(workspace=ws, name="Tema Logistics", city="Tema")
        Contact.objects.create(workspace=ws, name="Ama Boateng", account=acc)
        Deal.objects.create(workspace=ws, title="Annual Fleet Retainer", account=acc, amount="12000.00")

        # Auth session must never be cached by browsers across sign-outs.
        sess_res = self.client.get("/api/v1/auth/session/")
        self.assertEqual(sess_res.status_code, 200)
        self.assertIn("no-store", sess_res["Cache-Control"])
        self.assertEqual(sess_res["Vary"], "Cookie")

        # Service-level catalog summary & record counts should serve subsequent reads without SQL.
        summary1 = serialize_workspace_catalog_summary(ws)
        with self.assertNumQueries(0):
            summary2 = serialize_workspace_catalog_summary(ws)
        self.assertEqual(summary1, summary2)

        counts1 = get_workspace_table_record_counts(ws)
        with self.assertNumQueries(0):
            counts2 = get_workspace_table_record_counts(ws)
        self.assertEqual(counts1, counts2)
        self.assertEqual(counts2["account"], 1)

        # HTTP endpoints: MISS -> HIT (reduced queries) -> 304 Not Modified
        endpoints = [
            f"/api/v1/workspaces/{ws.slug}/",
            f"/api/v1/workspaces/{ws.slug}/packs/",
            f"/api/v1/workspaces/{ws.slug}/dashboard/",
            f"/api/v1/workspaces/{ws.slug}/crm/options/",
        ]
        for url in endpoints:
            miss_res = self.client.get(url)
            self.assertEqual(miss_res.status_code, 200, url)
            self.assertEqual(miss_res["X-Cache"], "MISS", url)
            self.assertEqual(miss_res["Cache-Control"], "private, no-cache", url)
            self.assertEqual(miss_res["Vary"], "Cookie", url)
            etag = miss_res["ETag"]
            self.assertTrue(etag, url)

            # On cache hit, no domain SQL runs beyond session + user + workspace + role lookup.
            with self.assertNumQueries(4):
                hit_res = self.client.get(url)
            self.assertEqual(hit_res.status_code, 200, url)
            self.assertEqual(hit_res["X-Cache"], "HIT", url)
            self.assertEqual(hit_res["ETag"], etag, url)
            self.assertEqual(hit_res.json(), miss_res.json(), url)

            not_mod = self.client.get(url, HTTP_IF_NONE_MATCH=etag)
            self.assertEqual(not_mod.status_code, 304, url)
            self.assertEqual(not_mod["ETag"], etag, url)
            self.assertEqual(not_mod["X-Cache"], "HIT", url)

        # Sanity check owner referenced cleanly
        self.assertTrue(owner.is_active)

    def test_invalidation_on_crm_record_mutations_and_lead_conversion(self):
        ws, _owner, _ = self._create_verified_workspace()

        # Warm caches
        self.assertEqual(self.client.get(f"/api/v1/workspaces/{ws.slug}/packs/")["X-Cache"], "MISS")
        self.assertEqual(self.client.get(f"/api/v1/workspaces/{ws.slug}/dashboard/")["X-Cache"], "MISS")
        self.assertEqual(self.client.get(f"/api/v1/workspaces/{ws.slug}/crm/options/")["X-Cache"], "MISS")

        # Create a Deal: invalidates `records` (packs & dashboard), while `crm/options` stays HIT.
        deal_res = self.client.post(
            f"/api/v1/workspaces/{ws.slug}/records/deal/",
            data=json.dumps({"title": "Solar Setup", "amount": "5000.00"}),
            content_type="application/json",
        )
        self.assertEqual(deal_res.status_code, 201)

        opts_after_deal = self.client.get(f"/api/v1/workspaces/{ws.slug}/crm/options/")
        self.assertEqual(opts_after_deal["X-Cache"], "HIT")

        dash_after_deal = self.client.get(f"/api/v1/workspaces/{ws.slug}/dashboard/")
        self.assertEqual(dash_after_deal["X-Cache"], "MISS")
        self.assertEqual(dash_after_deal.json()["counts_by_object"]["deal"], 1)

        packs_after_deal = self.client.get(f"/api/v1/workspaces/{ws.slug}/packs/")
        self.assertEqual(packs_after_deal["X-Cache"], "MISS")
        deal_table = next(t for t in packs_after_deal.json()["tables"] if t["key"] == "deal")
        self.assertEqual(deal_table["record_count"], 1)

        # Create Account: invalidates `records` + `lookup_records` (dashboard, packs, AND crm/options).
        acc_res = self.client.post(
            f"/api/v1/workspaces/{ws.slug}/records/account/",
            data=json.dumps({"name": "Kumasi Mills"}),
            content_type="application/json",
        )
        self.assertEqual(acc_res.status_code, 201)
        acc_id = acc_res.json()["record"]["id"]

        opts_after_acc = self.client.get(f"/api/v1/workspaces/{ws.slug}/crm/options/")
        self.assertEqual(opts_after_acc["X-Cache"], "MISS")
        self.assertEqual([a["name"] for a in opts_after_acc.json()["accounts"]], ["Kumasi Mills"])

        # Edit Account name -> immediately reflected in options
        patch_acc = self.client.patch(
            f"/api/v1/workspaces/{ws.slug}/records/account/{acc_id}/",
            data=json.dumps({"name": "Kumasi Agro Mills"}),
            content_type="application/json",
        )
        self.assertEqual(patch_acc.status_code, 200)
        opts_after_edit = self.client.get(f"/api/v1/workspaces/{ws.slug}/crm/options/")
        self.assertEqual(opts_after_edit["X-Cache"], "MISS")
        self.assertEqual([a["name"] for a in opts_after_edit.json()["accounts"]], ["Kumasi Agro Mills"])

        # Delete Account -> immediately removed from options & dashboard
        del_acc = self.client.delete(f"/api/v1/workspaces/{ws.slug}/records/account/{acc_id}/")
        self.assertEqual(del_acc.status_code, 200)
        opts_after_del = self.client.get(f"/api/v1/workspaces/{ws.slug}/crm/options/")
        self.assertEqual(opts_after_del["X-Cache"], "MISS")
        self.assertEqual(opts_after_del.json()["accounts"], [])

        # Convert Lead -> creating a Lead only bumps `records`, so `crm/options` stays HIT until conversion.
        lead = Lead.objects.create(workspace=ws, name="Kwaku Mensah", company="Volta Foods")
        self.assertEqual(self.client.get(f"/api/v1/workspaces/{ws.slug}/crm/options/")["X-Cache"], "HIT")

        conv_res = self.client.post(
            f"/api/v1/workspaces/{ws.slug}/records/lead/{lead.id}/convert/",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(conv_res.status_code, 200)

        opts_after_conv = self.client.get(f"/api/v1/workspaces/{ws.slug}/crm/options/")
        self.assertEqual(opts_after_conv["X-Cache"], "MISS")
        self.assertEqual([a["name"] for a in opts_after_conv.json()["accounts"]], ["Volta Foods"])
        self.assertEqual([c["name"] for c in opts_after_conv.json()["contacts"]], ["Kwaku Mensah"])

    def test_invalidation_on_table_toggle_pack_enable_labels_and_pipeline_changes(self):
        ws, _owner, _ = self._create_verified_workspace()

        # Warm all metadata endpoints
        for url in (
            f"/api/v1/workspaces/{ws.slug}/",
            f"/api/v1/workspaces/{ws.slug}/packs/",
            f"/api/v1/workspaces/{ws.slug}/dashboard/",
            f"/api/v1/workspaces/{ws.slug}/crm/options/",
        ):
            self.assertEqual(self.client.get(url)["X-Cache"], "MISS")
            self.assertEqual(self.client.get(url)["X-Cache"], "HIT")

        # Toggle empty pack table `job` off
        toggle_res = self.client.patch(
            f"/api/v1/workspaces/{ws.slug}/tables/job/",
            data=json.dumps({"is_enabled": False}),
            content_type="application/json",
        )
        self.assertEqual(toggle_res.status_code, 200)

        ws_after_toggle = self.client.get(f"/api/v1/workspaces/{ws.slug}/")
        self.assertEqual(ws_after_toggle["X-Cache"], "MISS")
        self.assertNotIn("job", ws_after_toggle.json()["workspace"]["enabled_object_keys"])

        dash_after_toggle = self.client.get(f"/api/v1/workspaces/{ws.slug}/dashboard/")
        self.assertEqual(dash_after_toggle["X-Cache"], "MISS")
        self.assertNotIn("job", dash_after_toggle.json()["readable_object_keys"])

        # Enable healthcare pack -> `visit` appears immediately across workspace summary, packs, and dashboard
        pack_res = self.client.post(
            f"/api/v1/workspaces/{ws.slug}/packs/",
            data=json.dumps({"pack_key": "healthcare_wellness"}),
            content_type="application/json",
        )
        self.assertEqual(pack_res.status_code, 200)
        ws_after_pack = self.client.get(f"/api/v1/workspaces/{ws.slug}/")
        self.assertEqual(ws_after_pack["X-Cache"], "MISS")
        self.assertIn("visit", ws_after_pack.json()["workspace"]["enabled_object_keys"])
        packs_after = self.client.get(f"/api/v1/workspaces/{ws.slug}/packs/")
        self.assertIn("visit", packs_after.json()["workspace"]["enabled_object_keys"])

        # Update workspace contact_label -> workspace summary updates immediately
        label_res = self.client.patch(
            f"/api/v1/workspaces/{ws.slug}/",
            data=json.dumps({"contact_label": "Client"}),
            content_type="application/json",
        )
        self.assertEqual(label_res.status_code, 200)
        ws_after_label = self.client.get(f"/api/v1/workspaces/{ws.slug}/")
        self.assertEqual(ws_after_label["X-Cache"], "MISS")
        self.assertEqual(ws_after_label.json()["workspace"]["contact_label"], "Client")

        # Pipeline stage changes -> options immediately reflect updated stages
        self.assertEqual(self.client.get(f"/api/v1/workspaces/{ws.slug}/crm/options/")["X-Cache"], "MISS")
        self.assertEqual(self.client.get(f"/api/v1/workspaces/{ws.slug}/crm/options/")["X-Cache"], "HIT")

        add_stage_res = self.client.post(
            f"/api/v1/workspaces/{ws.slug}/pipelines/stages/",
            data=json.dumps({"name": "Contract Sent", "tone": "indigo"}),
            content_type="application/json",
        )
        self.assertEqual(add_stage_res.status_code, 201)
        opts_after_stage = self.client.get(f"/api/v1/workspaces/{ws.slug}/crm/options/")
        self.assertEqual(opts_after_stage["X-Cache"], "MISS")
        self.assertIn("Contract Sent", opts_after_stage.json()["constants"]["deal_stages"])

        stage_ids = [s["id"] for s in opts_after_stage.json()["constants"]["deal_pipeline_stages"]]
        reordered_ids = list(reversed(stage_ids))
        reorder_res = self.client.post(
            f"/api/v1/workspaces/{ws.slug}/pipelines/stages/reorder/",
            data=json.dumps({"stage_ids": reordered_ids}),
            content_type="application/json",
        )
        self.assertEqual(reorder_res.status_code, 200)
        opts_after_reorder = self.client.get(f"/api/v1/workspaces/{ws.slug}/crm/options/")
        self.assertEqual(opts_after_reorder["X-Cache"], "MISS")
        self.assertEqual(
            [s["id"] for s in opts_after_reorder.json()["constants"]["deal_pipeline_stages"]],
            reordered_ids,
        )

    def test_invalidation_on_role_update_and_member_changes(self):
        ws, _owner, _ = self._create_verified_workspace()
        _owner_role, staff_role = ensure_workspace_default_roles(ws)
        staff_user, staff_client = self._create_staff_member(ws, email="staff@kora.example")
        Deal.objects.create(workspace=ws, title="Generator Deal", amount="9500.00")

        # Warm Staff and Owner caches
        self.assertEqual(staff_client.get(f"/api/v1/workspaces/{ws.slug}/dashboard/")["X-Cache"], "MISS")
        self.assertEqual(staff_client.get(f"/api/v1/workspaces/{ws.slug}/dashboard/")["X-Cache"], "HIT")
        self.assertEqual(staff_client.get(f"/api/v1/workspaces/{ws.slug}/crm/options/")["X-Cache"], "MISS")
        self.assertEqual(self.client.get(f"/api/v1/workspaces/{ws.slug}/")["X-Cache"], "MISS")
        self.assertEqual(self.client.get(f"/api/v1/workspaces/{ws.slug}/crm/options/")["X-Cache"], "MISS")

        # Owner updates Staff role to hide deal.amount and revoke lead.read + lead.update
        role_res = self.client.patch(
            f"/api/v1/workspaces/{ws.slug}/roles/{staff_role.id}/",
            data=json.dumps(
                {
                    "object_rights": {"lead": {"read": False, "update": False}},
                    "field_rights": {"deal": {"amount": "hidden"}},
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(role_res.status_code, 200)

        staff_dash = staff_client.get(f"/api/v1/workspaces/{ws.slug}/dashboard/")
        self.assertEqual(staff_dash["X-Cache"], "MISS")
        self.assertNotIn("lead", staff_dash.json()["readable_object_keys"])
        self.assertIsNone(staff_dash.json()["deal_summary"]["total_amount"])

        staff_opts = staff_client.get(f"/api/v1/workspaces/{ws.slug}/crm/options/")
        self.assertEqual(staff_opts["X-Cache"], "MISS")
        self.assertFalse(staff_opts.json()["can_convert_lead"])

        # Deactivate member -> active_seat_count & crm/options assignees update immediately
        self.assertEqual(self.client.get(f"/api/v1/workspaces/{ws.slug}/")["X-Cache"], "MISS")
        self.assertEqual(self.client.get(f"/api/v1/workspaces/{ws.slug}/")["X-Cache"], "HIT")

        deact_res = self.client.post(f"/api/v1/workspaces/{ws.slug}/members/{staff_user.id}/deactivate/")
        self.assertEqual(deact_res.status_code, 200)

        owner_ws = self.client.get(f"/api/v1/workspaces/{ws.slug}/")
        self.assertEqual(owner_ws["X-Cache"], "MISS")
        self.assertEqual(owner_ws.json()["active_seat_count"], 1)

        owner_opts = self.client.get(f"/api/v1/workspaces/{ws.slug}/crm/options/")
        self.assertEqual(owner_opts["X-Cache"], "MISS")
        self.assertEqual([a["email"] for a in owner_opts.json()["assignees"]], ["owner@kora.example"])

        # Reactivate member -> active_seat_count & assignees restore immediately
        react_res = self.client.post(f"/api/v1/workspaces/{ws.slug}/members/{staff_user.id}/reactivate/")
        self.assertEqual(react_res.status_code, 200)
        owner_opts2 = self.client.get(f"/api/v1/workspaces/{ws.slug}/crm/options/")
        self.assertEqual(owner_opts2["X-Cache"], "MISS")
        self.assertEqual(
            [a["email"] for a in owner_opts2.json()["assignees"]],
            ["owner@kora.example", "staff@kora.example"],
        )

    def test_strict_workspace_isolation_across_tenants(self):
        client_a = Client()
        client_b = Client()
        ws_a, _owner_a, _ = self._create_verified_workspace(
            client=client_a,
            workspace_name="Alpha Services",
            slug="alpha-services",
            business_type="general_services",
            email="owner-a@alpha.example",
        )
        ws_b, _owner_b, _ = self._create_verified_workspace(
            client=client_b,
            workspace_name="Beta Services",
            slug="beta-services",
            business_type="general_services",
            email="owner-b@beta.example",
        )

        # Warm both workspaces while empty -> ETags must still be distinct per workspace
        dash_a1 = client_a.get(f"/api/v1/workspaces/{ws_a.slug}/dashboard/")
        dash_b1 = client_b.get(f"/api/v1/workspaces/{ws_b.slug}/dashboard/")
        self.assertNotEqual(dash_a1["ETag"], dash_b1["ETag"])

        opts_a1 = client_a.get(f"/api/v1/workspaces/{ws_a.slug}/crm/options/")
        opts_b1 = client_b.get(f"/api/v1/workspaces/{ws_b.slug}/crm/options/")
        self.assertEqual(opts_a1["X-Cache"], "MISS")
        self.assertEqual(opts_b1["X-Cache"], "MISS")

        # Create Account only in Workspace A
        Account.objects.create(workspace=ws_a, name="Alpha Secret Client")

        # Workspace A is invalidated; Workspace B remains HIT with zero records from A
        opts_a2 = client_a.get(f"/api/v1/workspaces/{ws_a.slug}/crm/options/")
        self.assertEqual(opts_a2["X-Cache"], "MISS")
        self.assertEqual([a["name"] for a in opts_a2.json()["accounts"]], ["Alpha Secret Client"])

        opts_b2 = client_b.get(f"/api/v1/workspaces/{ws_b.slug}/crm/options/")
        self.assertEqual(opts_b2["X-Cache"], "HIT")
        self.assertEqual(opts_b2.json()["accounts"], [])

        dash_b2 = client_b.get(f"/api/v1/workspaces/{ws_b.slug}/dashboard/")
        self.assertEqual(dash_b2["X-Cache"], "HIT")
        self.assertEqual(dash_b2.json()["total_records"], 0)

        # Cross-tenant access continues to be refused even when caches are warm
        cross_res = client_a.get(f"/api/v1/workspaces/{ws_b.slug}/dashboard/")
        self.assertEqual(cross_res.status_code, 404)

    def test_strict_caller_and_private_record_isolation_within_workspace(self):
        ws, owner, _ = self._create_verified_workspace()
        _owner_role, staff_role = ensure_workspace_default_roles(ws)
        staff_a, client_a = self._create_staff_member(
            ws, email="staff-a@kora.example", role=staff_role
        )
        _staff_b, client_b = self._create_staff_member(
            ws, email="staff-b@kora.example", role=staff_role
        )

        Account.objects.create(
            workspace=ws,
            name="Shared Workspace Account",
            owner=owner,
            visibility="workspace",
        )
        Account.objects.create(
            workspace=ws,
            name="Owner Private Account",
            owner=owner,
            visibility="owner",
        )
        Account.objects.create(
            workspace=ws,
            name="Staff A Private Account",
            owner=staff_a,
            visibility="owner",
        )

        # Warm Owner, Staff A, and Staff B caches
        owner_opts = self.client.get(f"/api/v1/workspaces/{ws.slug}/crm/options/")
        a_opts = client_a.get(f"/api/v1/workspaces/{ws.slug}/crm/options/")
        b_opts = client_b.get(f"/api/v1/workspaces/{ws.slug}/crm/options/")

        self.assertEqual(
            [a["name"] for a in owner_opts.json()["accounts"]],
            ["Owner Private Account", "Shared Workspace Account", "Staff A Private Account"],
        )
        self.assertEqual(
            [a["name"] for a in a_opts.json()["accounts"]],
            ["Shared Workspace Account", "Staff A Private Account"],
        )
        self.assertEqual(
            [a["name"] for a in b_opts.json()["accounts"]],
            ["Shared Workspace Account"],
        )

        # Repeat reads hit each caller's own cache without leaking private rows
        a_opts_hit = client_a.get(f"/api/v1/workspaces/{ws.slug}/crm/options/")
        b_opts_hit = client_b.get(f"/api/v1/workspaces/{ws.slug}/crm/options/")
        self.assertEqual(a_opts_hit["X-Cache"], "HIT")
        self.assertEqual(b_opts_hit["X-Cache"], "HIT")
        self.assertEqual(
            [a["name"] for a in a_opts_hit.json()["accounts"]],
            ["Shared Workspace Account", "Staff A Private Account"],
        )
        self.assertEqual(
            [a["name"] for a in b_opts_hit.json()["accounts"]],
            ["Shared Workspace Account"],
        )
        self.assertNotEqual(a_opts_hit["ETag"], b_opts_hit["ETag"])

    def test_workspace_pipelines_endpoint_caches_with_etag_and_invalidates(self):
        ws, _owner, _ = self._create_verified_workspace()

        p1 = self.client.get(f"/api/v1/workspaces/{ws.slug}/pipelines/")
        self.assertEqual(p1.status_code, 200)
        self.assertEqual(p1["X-Cache"], "MISS")
        etag = p1["ETag"]
        self.assertEqual(p1["Vary"], "Cookie")

        p2 = self.client.get(f"/api/v1/workspaces/{ws.slug}/pipelines/")
        self.assertEqual(p2.status_code, 200)
        self.assertEqual(p2["X-Cache"], "HIT")
        self.assertEqual(p2["ETag"], etag)

        p304 = self.client.get(
            f"/api/v1/workspaces/{ws.slug}/pipelines/",
            HTTP_IF_NONE_MATCH=etag,
        )
        self.assertEqual(p304.status_code, 304)

        add_res = self.client.post(
            f"/api/v1/workspaces/{ws.slug}/pipelines/stages/",
            data=json.dumps({"name": "Technical Review", "tone": "indigo"}),
            content_type="application/json",
        )
        self.assertEqual(add_res.status_code, 201)

        p3 = self.client.get(f"/api/v1/workspaces/{ws.slug}/pipelines/")
        self.assertEqual(p3.status_code, 200)
        self.assertEqual(p3["X-Cache"], "MISS")
        self.assertNotEqual(p3["ETag"], etag)
        self.assertTrue(any(s["name"] == "Technical Review" for s in p3.json()["stages"]))
