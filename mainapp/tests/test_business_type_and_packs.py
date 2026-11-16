import json
from unittest.mock import patch

from django.core import mail
from django.test import Client, TestCase, override_settings

from ..catalog import BUSINESS_TYPES, CATALOG_OBJECT_KEYS, CORE_OBJECT_KEYS
from ..integrations.geolocation import LOCATION_REFUSAL_MESSAGE
from ..models import (
    AuditLog,
    Role,
    User,
    Workspace,
    WorkspacePack,
    WorkspaceTable,
)
from ..service.catalog_service import (
    register_record_count_resolver,
    unregister_record_count_resolver,
)
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
class BusinessTypeAndPacksTests(TestCase):
    def tearDown(self):
        for key in tuple(CORE_OBJECT_KEYS) + tuple(CATALOG_OBJECT_KEYS):
            unregister_record_count_resolver(key)
        super().tearDown()

    def _create_verified_workspace(
        self,
        client=None,
        *,
        workspace_name="Accra Services",
        business_type="general_services",
        email="owner@accraservices.example",
        password="StrongPassword123!",
    ):
        client = client or self.client
        with patch("mainapp.integrations.geolocation.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = make_mock_urlopen(
                json.dumps(CLEAR_GH_PAYLOAD).encode("utf-8")
            )
            signup_res = client.post(
                "/api/v1/signup/",
                data=json.dumps(
                    {
                        "workspace_name": workspace_name,
                        "business_type": business_type,
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
        return workspace, user, verify_res.json()

    def test_public_catalog_lists_5_core_25_catalog_and_20_business_types(self):
        res = self.client.get("/api/v1/catalog/business-types/")
        self.assertEqual(res.status_code, 200)
        body = res.json()

        self.assertEqual(len(body["core_objects"]), 5)
        self.assertEqual(len(body["catalog_objects"]), 25)
        self.assertEqual(len(body["business_types"]), 20)

        valid_catalog_keys = set(CATALOG_OBJECT_KEYS)
        for bt in body["business_types"]:
            self.assertEqual(bt["pack_table_count"], 12)
            self.assertEqual(bt["total_starter_tables"], 17)
            self.assertEqual(len(bt["pack_object_keys"]), 12)
            self.assertEqual(len(set(bt["pack_object_keys"])), 12)
            self.assertTrue(set(bt["pack_object_keys"]).issubset(valid_catalog_keys))

    def test_signup_requires_valid_business_type_after_geo_check(self):
        # Non-GH IP is still refused first before business_type validation
        with patch("mainapp.integrations.geolocation.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = make_mock_urlopen(
                json.dumps(
                    {
                        "ip": "8.8.8.8",
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
            geo_refusal = self.client.post(
                "/api/v1/signup/",
                data=json.dumps(
                    {
                        "workspace_name": "Missing Type Co",
                        "email": "geo@fail.example",
                        "password": "StrongPassword123!",
                    }
                ),
                content_type="application/json",
                REMOTE_ADDR="8.8.8.8",
            )
            self.assertEqual(geo_refusal.status_code, 403)
            self.assertEqual(geo_refusal.json()["error"], LOCATION_REFUSAL_MESSAGE)

        # Clear GH IP still rejects missing or unknown business_type with 400
        with patch("mainapp.integrations.geolocation.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = make_mock_urlopen(
                json.dumps(CLEAR_GH_PAYLOAD).encode("utf-8")
            )
            for invalid_type in (None, "", "space_mining"):
                payload = {
                    "workspace_name": "Invalid Type Co",
                    "email": "badtype@co.example",
                    "password": "StrongPassword123!",
                }
                if invalid_type is not None:
                    payload["business_type"] = invalid_type
                res = self.client.post(
                    "/api/v1/signup/",
                    data=json.dumps(payload),
                    content_type="application/json",
                    REMOTE_ADDR="154.160.1.1",
                )
                self.assertEqual(res.status_code, 400)
        self.assertEqual(Workspace.objects.count(), 0)

    def test_general_services_and_healthcare_and_specialized_contact_labels(self):
        # 1. General services -> 17 tables including Lead, Deal, Job, Appointment, Ticket, Invoice
        gs_ws, _gs_user, gs_payload = self._create_verified_workspace(
            client=Client(),
            workspace_name="Accra General Co",
            business_type="general_services",
            email="gs@accra.example",
        )
        gs_keys = set(gs_payload["workspace"]["enabled_object_keys"])
        self.assertEqual(gs_payload["workspace"]["active_table_count"], 17)
        self.assertEqual(gs_payload["workspace"]["core_table_count"], 5)
        self.assertEqual(gs_payload["workspace"]["pack_table_count"], 12)
        self.assertEqual(gs_payload["workspace"]["contact_label"], "Contact")
        self.assertEqual(gs_payload["workspace"]["contact_plural_label"], "Contacts")
        for required_key in (
            "account",
            "contact",
            "activity",
            "file",
            "tag",
            "lead",
            "deal",
            "job",
            "appointment",
            "ticket",
            "invoice",
        ):
            self.assertIn(required_key, gs_keys)
        self.assertEqual(
            WorkspaceTable.objects.filter(workspace=gs_ws, is_enabled=True).count(),
            17,
        )

        # 2. Healthcare -> Contact labeled Patient, includes Appointment and Visit, excludes Deal
        hc_client = Client()
        _hc_ws, _hc_user, hc_payload = self._create_verified_workspace(
            client=hc_client,
            workspace_name="Korle Clinic",
            business_type="healthcare_wellness",
            email="clinic@accra.example",
        )
        hc_keys = set(hc_payload["workspace"]["enabled_object_keys"])
        self.assertEqual(hc_payload["workspace"]["active_table_count"], 17)
        self.assertEqual(hc_payload["workspace"]["contact_label"], "Patient")
        self.assertEqual(hc_payload["workspace"]["contact_plural_label"], "Patients")
        self.assertIn("appointment", hc_keys)
        self.assertIn("visit", hc_keys)
        self.assertNotIn("deal", hc_keys)

        # 3. Education -> Student or Guardian
        _ed_ws, _ed_user, ed_payload = self._create_verified_workspace(
            client=Client(),
            workspace_name="Legon Academy",
            business_type="education",
            email="edu@accra.example",
        )
        self.assertEqual(ed_payload["workspace"]["contact_label"], "Student or Guardian")
        self.assertEqual(
            ed_payload["workspace"]["contact_plural_label"], "Students or Guardians"
        )
        self.assertEqual(ed_payload["workspace"]["active_table_count"], 17)

        # 4. Nonprofit, faith, and membership -> Donor or Member
        _np_ws, _np_user, np_payload = self._create_verified_workspace(
            client=Client(),
            workspace_name="Hope Foundation",
            business_type="nonprofit_faith_membership",
            email="ngo@accra.example",
        )
        self.assertEqual(np_payload["workspace"]["contact_label"], "Donor or Member")
        self.assertEqual(
            np_payload["workspace"]["contact_plural_label"], "Donors or Members"
        )
        self.assertEqual(np_payload["workspace"]["active_table_count"], 17)

    def test_all_twenty_business_types_create_17_tables_at_signup(self):
        for idx, bt in enumerate(BUSINESS_TYPES):
            c = Client()
            ws, _user, payload = self._create_verified_workspace(
                client=c,
                workspace_name=f"Tenant {idx} Co",
                business_type=bt.key,
                email=f"tenant{idx}@ghana.example",
            )
            self.assertEqual(payload["workspace"]["active_table_count"], 17)
            self.assertEqual(
                WorkspaceTable.objects.filter(workspace=ws, is_enabled=True).count(),
                17,
            )
            self.assertEqual(
                WorkspacePack.objects.get(workspace=ws, is_primary=True).pack_key,
                bt.key,
            )

    def test_enable_second_pack_preserves_records_and_empty_vs_nonempty_toggle_rules(self):
        workspace, owner, _payload = self._create_verified_workspace(
            workspace_name="Kora General",
            business_type="general_services",
            email="owner@kora.example",
        )

        # Simulate saved records in Deal (from General services pack)
        register_record_count_resolver("deal", lambda ws: 3 if ws.id == workspace.id else 0)

        # Enable Healthcare pack as a second pack
        add_pack_res = self.client.post(
            f"/api/v1/workspaces/{workspace.slug}/packs/",
            data=json.dumps({"pack_key": "healthcare_wellness"}),
            content_type="application/json",
        )
        self.assertEqual(add_pack_res.status_code, 200)
        add_body = add_pack_res.json()
        self.assertEqual(
            add_body["workspace"]["enabled_packs"],
            ["general_services", "healthcare_wellness"],
        )
        # Contact label remains primary ("Contact") unless explicitly changed
        self.assertEqual(add_body["workspace"]["contact_label"], "Contact")

        enabled_keys = set(add_body["workspace"]["enabled_object_keys"])
        # Tables from pack 1 (including Deal with 3 records) stay enabled, and new Healthcare tables appear
        self.assertIn("deal", enabled_keys)
        self.assertIn("supplier", enabled_keys)
        self.assertIn("visit", enabled_keys)
        self.assertIn("inventory", enabled_keys)
        self.assertIn("customer_subscription", enabled_keys)
        self.assertEqual(add_body["active_table_count"], 20)

        deal_row = next(t for t in add_body["tables"] if t["key"] == "deal")
        self.assertEqual(deal_row["record_count"], 3)
        self.assertFalse(deal_row["can_disable"])

        # Attempting to turn off Deal while it holds 3 records is refused with 409
        refuse_deal = self.client.patch(
            f"/api/v1/workspaces/{workspace.slug}/tables/deal/",
            data=json.dumps({"is_enabled": False}),
            content_type="application/json",
        )
        self.assertEqual(refuse_deal.status_code, 409)
        self.assertIn("3 records", refuse_deal.json()["error"])
        self.assertTrue(
            WorkspaceTable.objects.get(workspace=workspace, object_key="deal").is_enabled
        )

        # Turning off a core table (Account) is refused with 400
        refuse_core = self.client.patch(
            f"/api/v1/workspaces/{workspace.slug}/tables/account/",
            data=json.dumps({"is_enabled": False}),
            content_type="application/json",
        )
        self.assertEqual(refuse_core.status_code, 400)

        # Turning off an empty pack table (Visit, 0 records) succeeds
        disable_visit = self.client.patch(
            f"/api/v1/workspaces/{workspace.slug}/tables/visit/",
            data=json.dumps({"is_enabled": False}),
            content_type="application/json",
        )
        self.assertEqual(disable_visit.status_code, 200)
        self.assertNotIn(
            "visit",
            disable_visit.json()["workspace"]["enabled_object_keys"],
        )
        self.assertFalse(
            WorkspaceTable.objects.get(workspace=workspace, object_key="visit").is_enabled
        )

        # Turning Visit back on succeeds
        enable_visit = self.client.patch(
            f"/api/v1/workspaces/{workspace.slug}/tables/visit/",
            data=json.dumps({"is_enabled": True}),
            content_type="application/json",
        )
        self.assertEqual(enable_visit.status_code, 200)
        self.assertIn(
            "visit",
            enable_visit.json()["workspace"]["enabled_object_keys"],
        )

        # Audit log recorded pack activation and table toggles by the Owner
        self.assertTrue(
            AuditLog.objects.filter(
                workspace=workspace,
                object_type="workspace_pack",
                actor_user=owner,
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                workspace=workspace,
                object_type="workspace_table",
                field_name="is_enabled",
                actor_user=owner,
            ).exists()
        )

    def test_rename_business_type_label_contact_label_permissions_and_isolation(self):
        workspace, owner, _ = self._create_verified_workspace(
            workspace_name="Tema Ops",
            business_type="healthcare_wellness",
            email="owner@temaops.example",
        )

        # Owner renames business_type_label and customizes contact_label
        rename_res = self.client.patch(
            f"/api/v1/workspaces/{workspace.slug}/",
            data=json.dumps(
                {
                    "business_type_label": "Outpatient Clinic & Pharmacy",
                    "contact_label": "Member Patient",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(rename_res.status_code, 200)
        self.assertEqual(
            rename_res.json()["workspace"]["business_type_label"],
            "Outpatient Clinic & Pharmacy",
        )
        self.assertEqual(rename_res.json()["workspace"]["business_type"], "healthcare_wellness")
        self.assertEqual(rename_res.json()["workspace"]["contact_label"], "Member Patient")
        self.assertEqual(
            rename_res.json()["workspace"]["contact_plural_label"],
            "Member Patients",
        )

        # Empty label is refused with 400
        bad_rename = self.client.patch(
            f"/api/v1/workspaces/{workspace.slug}/",
            data=json.dumps({"business_type_label": "   "}),
            content_type="application/json",
        )
        self.assertEqual(bad_rename.status_code, 400)

        # Resetting contact_label restores Healthcare default ("Patient")
        reset_res = self.client.patch(
            f"/api/v1/workspaces/{workspace.slug}/",
            data=json.dumps({"reset_contact_label": True}),
            content_type="application/json",
        )
        self.assertEqual(reset_res.status_code, 200)
        self.assertEqual(reset_res.json()["workspace"]["contact_label"], "Patient")

        # Invite a Staff member (plain Staff cannot mutate packs, tables, or labels)
        self.client.post(
            f"/api/v1/workspaces/{workspace.slug}/invites/",
            data=json.dumps({"email": "staff@temaops.example"}),
            content_type="application/json",
        )
        invite_token = extract_token_from_email_body(mail.outbox[-1].body)
        staff_client = Client()
        staff_client.post(
            "/api/v1/invites/accept/",
            data=json.dumps(
                {
                    "token": invite_token,
                    "password": "StaffPassword123!",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(
            staff_client.get(f"/api/v1/workspaces/{workspace.slug}/packs/").status_code,
            200,
        )
        self.assertEqual(
            staff_client.post(
                f"/api/v1/workspaces/{workspace.slug}/packs/",
                data=json.dumps({"pack_key": "general_services"}),
                content_type="application/json",
            ).status_code,
            403,
        )
        self.assertEqual(
            staff_client.patch(
                f"/api/v1/workspaces/{workspace.slug}/tables/visit/",
                data=json.dumps({"is_enabled": False}),
                content_type="application/json",
            ).status_code,
            403,
        )
        self.assertEqual(
            staff_client.patch(
                f"/api/v1/workspaces/{workspace.slug}/",
                data=json.dumps({"business_type_label": "Unauthorized"}),
                content_type="application/json",
            ).status_code,
            403,
        )

        # Role with theme_and_layouts (or users_and_roles) CAN manage packs/tables/labels
        staff_user = User.objects.get(email="staff@temaops.example")
        layout_role = Role.objects.create(
            workspace=workspace,
            name="Operations Lead",
            theme_and_layouts=True,
        )
        staff_user.role = layout_role
        staff_user.save(update_fields=["role"])

        self.assertEqual(
            staff_client.patch(
                f"/api/v1/workspaces/{workspace.slug}/",
                data=json.dumps({"business_type_label": "Tema Wellness Center"}),
                content_type="application/json",
            ).status_code,
            200,
        )
        self.assertEqual(
            staff_client.post(
                f"/api/v1/workspaces/{workspace.slug}/packs/",
                data=json.dumps({"pack_key": "general_services"}),
                content_type="application/json",
            ).status_code,
            200,
        )

        # Cross-workspace caller receives 404
        other_ws, _other_owner, _ = self._create_verified_workspace(
            client=Client(),
            workspace_name="Other Org",
            business_type="retail_trading",
            email="other@org.example",
        )
        self.assertEqual(
            staff_client.get(f"/api/v1/workspaces/{other_ws.slug}/packs/").status_code,
            404,
        )
        self.assertEqual(
            staff_client.patch(
                f"/api/v1/workspaces/{other_ws.slug}/tables/deal/",
                data=json.dumps({"is_enabled": False}),
                content_type="application/json",
            ).status_code,
            404,
        )
        self.assertEqual(
            staff_client.patch(
                f"/api/v1/workspaces/{other_ws.slug}/",
                data=json.dumps({"business_type_label": "Hijack"}),
                content_type="application/json",
            ).status_code,
            404,
        )
        self.assertEqual(
            AuditLog.objects.filter(
                workspace=workspace,
                object_type="workspace",
                field_name="business_type_label",
            ).count(),
            2,
        )
        self.assertEqual(owner.workspace_id, workspace.id)
