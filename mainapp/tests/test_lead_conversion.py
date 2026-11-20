import json
from unittest.mock import patch

from django.core import mail
from django.test import Client, TestCase, override_settings

from ..models import (
    Account,
    Activity,
    AuditLog,
    Contact,
    Deal,
    Lead,
    Role,
    User,
    Workspace,
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
class LeadConversionTests(TestCase):
    def _create_verified_workspace(
        self,
        *,
        workspace_name="Accra Services Ltd",
        business_type="general_services",
        email="owner@accraservices.example",
        password="StrongPassword123!",
    ):
        client = Client()
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
        return client, workspace, user

    def _invite_and_accept_staff(
        self,
        owner_client,
        workspace,
        *,
        staff_email="staff@accraservices.example",
        password="StaffPassword123!",
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
                    "timezone": "Africa/Accra",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(accept_res.status_code, 200)
        staff_user = User.objects.select_related("role", "workspace").get(email=staff_email)
        return staff_client, staff_user

    def _create_lead(
        self,
        client,
        slug,
        *,
        name="Abena Koranteng",
        company="Volta Aluminum Co",
        email="a.koranteng@valco.com.gh",
        phone="+233 24 411 9082",
        sms_consent=True,
        notes="Smelter line 2 enquiry.",
    ):
        res = client.post(
            f"/api/v1/workspaces/{slug}/records/lead/",
            data=json.dumps(
                {
                    "name": name,
                    "company": company,
                    "email": email,
                    "phone": phone,
                    "sms_consent": sms_consent,
                    "notes": notes,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 201)
        return res.json()["record"]

    def test_general_services_lead_conversion_creates_linked_records_and_marks_lead(self):
        client, workspace, owner = self._create_verified_workspace()
        slug = workspace.slug
        lead = self._create_lead(client, slug)

        # Log pre-conversion activity on the Lead
        act_res = client.post(
            f"/api/v1/workspaces/{slug}/records/lead/{lead['id']}/activities/",
            data=json.dumps(
                {
                    "activity_type": "call",
                    "summary": "Discussed smelter control panel requirements.",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(act_res.status_code, 201)

        preview_res = client.get(
            f"/api/v1/workspaces/{slug}/records/lead/{lead['id']}/conversion-preview/"
        )
        self.assertEqual(preview_res.status_code, 200)
        preview = preview_res.json()
        self.assertFalse(preview["already_converted"])
        self.assertEqual(preview["account_matches"], [])
        self.assertEqual(preview["contact_matches"], [])
        self.assertEqual(preview["defaults"]["account_name"], "Volta Aluminum Co")
        self.assertEqual(preview["defaults"]["deal_stage"], "Qualified")
        self.assertTrue(preview["capabilities"]["can_convert_lead"])

        convert_res = client.post(
            f"/api/v1/workspaces/{slug}/records/lead/{lead['id']}/convert/",
            data=json.dumps(
                {
                    "account_name": "Volta Aluminum Co (VALCO)",
                    "account_city": "Tema",
                    "contact_name": "Abena Koranteng",
                    "contact_title": "Plant Procurement Lead",
                    "deal_title": "VALCO Smelter Control Panels",
                    "deal_amount": "85000.00",
                    "deal_stage": "Proposal",
                    "target_close_date": "2026-12-20",
                    "win_probability": 70,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(convert_res.status_code, 200, convert_res.content)
        payload = convert_res.json()

        self.assertEqual(payload["created"], {"account": True, "contact": True, "deal": True})
        conv_lead = payload["lead"]
        account = payload["account"]
        contact = payload["contact"]
        deal = payload["deal"]

        # Three new records exist and point at the lead
        self.assertEqual(account["source_lead_id"], lead["id"])
        self.assertEqual(account["city"], "Tema")
        self.assertEqual(account["linked_records"]["lead"]["id"], lead["id"])

        self.assertEqual(contact["source_lead_id"], lead["id"])
        self.assertEqual(contact["account_id"], account["id"])
        self.assertEqual(contact["title"], "Plant Procurement Lead")
        self.assertTrue(contact["sms_consent"])
        self.assertEqual(contact["linked_records"]["lead"]["id"], lead["id"])

        self.assertEqual(deal["source_lead_id"], lead["id"])
        self.assertEqual(deal["account_id"], account["id"])
        self.assertEqual(deal["contact_id"], contact["id"])
        self.assertEqual(deal["amount"], "85000.00")
        self.assertEqual(deal["stage"], "Proposal")
        self.assertEqual(deal["target_close_date"], "2026-12-20")
        self.assertEqual(deal["win_probability"], 70)
        self.assertEqual(deal["linked_records"]["lead"]["id"], lead["id"])

        # Lead stays listed with Converted mark and points to the three records
        list_res = client.get(f"/api/v1/workspaces/{slug}/records/lead/").json()
        self.assertEqual(list_res["total_count"], 1)
        listed_lead = list_res["results"][0]
        self.assertEqual(listed_lead["id"], lead["id"])
        self.assertEqual(listed_lead["status"], "converted")
        self.assertTrue(listed_lead["converted"])
        self.assertEqual(listed_lead["converted_account_id"], account["id"])
        self.assertEqual(listed_lead["converted_contact_id"], contact["id"])
        self.assertEqual(listed_lead["converted_deal_id"], deal["id"])
        self.assertEqual(conv_lead["linked_records"]["account"]["id"], account["id"])
        self.assertEqual(conv_lead["linked_records"]["contact"]["id"], contact["id"])
        self.assertEqual(conv_lead["linked_records"]["deal"]["id"], deal["id"])

        # Pre-conversion activity remains on Lead, plus conversion Notes on all 4 records
        self.assertEqual(len(conv_lead["activities"]), 2)
        self.assertEqual(
            {a["activity_type"] for a in conv_lead["activities"]},
            {"call", "note"},
        )
        self.assertEqual(
            Activity.objects.filter(
                workspace=workspace,
                target_object_type="account",
                target_object_id=account["id"],
            ).count(),
            1,
        )
        self.assertEqual(
            Activity.objects.filter(
                workspace=workspace,
                target_object_type="contact",
                target_object_id=contact["id"],
            ).count(),
            1,
        )
        self.assertEqual(
            Activity.objects.filter(
                workspace=workspace,
                target_object_type="deal",
                target_object_id=deal["id"],
            ).count(),
            1,
        )

        # Audit logs recorded for Account/Contact/Deal creations and Lead conversion
        self.assertTrue(
            AuditLog.objects.filter(
                workspace=workspace,
                object_type="lead",
                object_id=str(lead["id"]),
                action=AuditLog.ACTION_UPDATE,
                field_name="status",
                previous_value="open",
                new_value="converted",
                actor_user=owner,
            ).exists()
        )
        for obj_type, rec_id in (("account", account["id"]), ("contact", contact["id"]), ("deal", deal["id"])):
            self.assertTrue(
                AuditLog.objects.filter(
                    workspace=workspace,
                    object_type=obj_type,
                    object_id=str(rec_id),
                    action=AuditLog.ACTION_CREATE,
                ).exists()
            )

        # Dashboard summary reflects updated counts and converted lead status
        dash = client.get(f"/api/v1/workspaces/{slug}/dashboard/").json()
        self.assertEqual(dash["open_counts"]["lead_open"], 0)
        self.assertEqual(dash["counts_by_object"]["account"], 1)
        self.assertEqual(dash["counts_by_object"]["contact"], 1)
        self.assertEqual(dash["counts_by_object"]["deal"], 1)
        lead_recent = next(
            r for r in dash["recent_records"] if r["object_key"] == "lead" and r["id"] == lead["id"]
        )
        self.assertIn("Converted", lead_recent["meta"])

    def test_second_conversion_and_delete_refused_and_identity_locked(self):
        client, workspace, _owner = self._create_verified_workspace()
        slug = workspace.slug
        lead = self._create_lead(client, slug)

        first = client.post(
            f"/api/v1/workspaces/{slug}/records/lead/{lead['id']}/convert/",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 200)

        # Second conversion is refused and creates no extra records
        second = client.post(
            f"/api/v1/workspaces/{slug}/records/leads/{lead['id']}/convert/",
            data=json.dumps({"deal_title": "Duplicate Attempt"}),
            content_type="application/json",
        )
        self.assertEqual(second.status_code, 409)
        self.assertEqual(Account.objects.filter(workspace=workspace).count(), 1)
        self.assertEqual(Contact.objects.filter(workspace=workspace).count(), 1)
        self.assertEqual(Deal.objects.filter(workspace=workspace).count(), 1)

        # Converted lead stays on record (delete refused)
        del_res = client.delete(f"/api/v1/workspaces/{slug}/records/lead/{lead['id']}/")
        self.assertEqual(del_res.status_code, 409)
        self.assertTrue(Lead.objects.filter(workspace=workspace, id=lead["id"]).exists())

        # Identity fields are locked once converted
        bad_patch = client.patch(
            f"/api/v1/workspaces/{slug}/records/lead/{lead['id']}/",
            data=json.dumps({"name": "Changed Name"}),
            content_type="application/json",
        )
        self.assertEqual(bad_patch.status_code, 400)

        # Notes and SMS consent remain editable (even if unchanged identity fields are sent)
        ok_patch = client.patch(
            f"/api/v1/workspaces/{slug}/records/lead/{lead['id']}/",
            data=json.dumps(
                {
                    "name": lead["name"],
                    "company": lead["company"],
                    "email": lead["email"],
                    "phone": lead["phone"],
                    "sms_consent": False,
                    "notes": "Updated follow-up notes after conversion.",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(ok_patch.status_code, 200)
        self.assertFalse(ok_patch.json()["record"]["sms_consent"])
        self.assertEqual(
            ok_patch.json()["record"]["notes"],
            "Updated follow-up notes after conversion.",
        )

    def test_blank_company_fallback_and_empty_override_validation(self):
        client, workspace, _owner = self._create_verified_workspace()
        slug = workspace.slug
        lead = self._create_lead(
            client,
            slug,
            name="Kwesi Appiah",
            company="",
            email="",
            phone="",
        )

        bad_override = client.post(
            f"/api/v1/workspaces/{slug}/records/lead/{lead['id']}/convert/",
            data=json.dumps({"account_name": "   "}),
            content_type="application/json",
        )
        self.assertEqual(bad_override.status_code, 400)

        res = client.post(
            f"/api/v1/workspaces/{slug}/records/lead/{lead['id']}/convert/"
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["account"]["name"], "Kwesi Appiah")
        self.assertEqual(data["contact"]["name"], "Kwesi Appiah")
        self.assertEqual(data["deal"]["title"], "Kwesi Appiah Deal")
        self.assertEqual(data["deal"]["stage"], "Qualified")
        self.assertEqual(data["deal"]["amount"], "0.00")

    def test_conversion_matches_and_reuse_existing_account_and_standalone_contact(self):
        client, workspace, _owner = self._create_verified_workspace()
        slug = workspace.slug

        existing_acc = client.post(
            f"/api/v1/workspaces/{slug}/records/account/",
            data=json.dumps(
                {
                    "name": "Volta River Authority",
                    "city": "Accra",
                    "email": "procurement@vra.com.gh",
                }
            ),
            content_type="application/json",
        ).json()["record"]
        other_acc = client.post(
            f"/api/v1/workspaces/{slug}/records/account/",
            data=json.dumps({"name": "Ghana Grid Company", "city": "Tema"}),
            content_type="application/json",
        ).json()["record"]

        # Standalone contact matching by email/phone (normalized spacing)
        standalone_con = client.post(
            f"/api/v1/workspaces/{slug}/records/contact/",
            data=json.dumps(
                {
                    "name": "Nii Armah",
                    "email": "n.armah@vra.com.gh",
                    "phone": "+233208124401",
                    "title": "Telemetry Buyer",
                }
            ),
            content_type="application/json",
        ).json()["record"]
        bound_con = client.post(
            f"/api/v1/workspaces/{slug}/records/contact/",
            data=json.dumps(
                {
                    "name": "Ama Serwaa",
                    "account_id": other_acc["id"],
                    "email": "ama@gridco.example",
                }
            ),
            content_type="application/json",
        ).json()["record"]

        lead = self._create_lead(
            client,
            slug,
            name="Nii Armah",
            company="volta river authority",
            email="n.armah@vra.com.gh",
            phone="+233 20 812 4401",
        )

        preview = client.get(
            f"/api/v1/workspaces/{slug}/records/lead/{lead['id']}/conversion-preview/"
        ).json()
        self.assertEqual(len(preview["account_matches"]), 1)
        self.assertEqual(preview["account_matches"][0]["id"], existing_acc["id"])
        self.assertIn("Company name", preview["account_matches"][0]["match_reasons"])

        self.assertEqual(len(preview["contact_matches"]), 1)
        self.assertEqual(preview["contact_matches"][0]["id"], standalone_con["id"])
        self.assertEqual(
            set(preview["contact_matches"][0]["match_reasons"]),
            {"Email", "Phone", "Name"},
        )

        # Mismatched Contact (belongs to other_acc) with existing_acc is refused
        mismatch_res = client.post(
            f"/api/v1/workspaces/{slug}/records/lead/{lead['id']}/convert/",
            data=json.dumps(
                {
                    "account_id": existing_acc["id"],
                    "contact_id": bound_con["id"],
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(mismatch_res.status_code, 400)

        # Reuse existing Account and standalone Contact cleanly
        reuse_res = client.post(
            f"/api/v1/workspaces/{slug}/records/lead/{lead['id']}/convert/",
            data=json.dumps(
                {
                    "account_id": existing_acc["id"],
                    "contact_id": standalone_con["id"],
                    "deal_title": "VRA Telemetry Upgrade",
                    "deal_amount": "45000.00",
                    "deal_stage": "New",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(reuse_res.status_code, 200, reuse_res.content)
        data = reuse_res.json()
        self.assertEqual(data["created"], {"account": False, "contact": False, "deal": True})
        self.assertEqual(data["account"]["id"], existing_acc["id"])
        self.assertEqual(data["account"]["city"], "Accra")
        self.assertIsNone(data["account"]["source_lead_id"])
        self.assertEqual(data["account"]["linked_records"]["source_leads"][0]["id"], lead["id"])

        self.assertEqual(data["contact"]["id"], standalone_con["id"])
        self.assertEqual(data["contact"]["account_id"], existing_acc["id"])
        self.assertEqual(data["contact"]["title"], "Telemetry Buyer")
        self.assertEqual(data["contact"]["linked_records"]["source_leads"][0]["id"], lead["id"])

        self.assertEqual(data["deal"]["source_lead_id"], lead["id"])
        self.assertEqual(Account.objects.filter(workspace=workspace).count(), 2)
        self.assertEqual(Contact.objects.filter(workspace=workspace).count(), 2)
        self.assertEqual(Deal.objects.filter(workspace=workspace).count(), 1)

    def test_healthcare_and_packs_without_deal_do_not_offer_conversion(self):
        hc_client, hc_ws, _hc_owner = self._create_verified_workspace(
            workspace_name="Korle Clinic",
            business_type="healthcare_wellness",
            email="clinic@korle.example",
        )
        hc_lead = self._create_lead(
            hc_client,
            hc_ws.slug,
            name="Esi Mensah",
            company="Korle Wellness Walk-in",
        )

        # Healthcare does not include Deal, so conversion is unavailable
        opts = hc_client.get(f"/api/v1/workspaces/{hc_ws.slug}/crm/options/").json()
        self.assertFalse(opts["lead_conversion_available"])
        self.assertFalse(opts["can_convert_lead"])

        leads_list = hc_client.get(f"/api/v1/workspaces/{hc_ws.slug}/records/lead/").json()
        self.assertFalse(leads_list["permissions"]["conversion_available"])
        self.assertFalse(leads_list["permissions"]["can_convert_lead"])

        self.assertEqual(
            hc_client.get(
                f"/api/v1/workspaces/{hc_ws.slug}/records/lead/{hc_lead['id']}/conversion-preview/"
            ).status_code,
            404,
        )
        self.assertEqual(
            hc_client.post(
                f"/api/v1/workspaces/{hc_ws.slug}/records/lead/{hc_lead['id']}/convert/",
                data=json.dumps({}),
                content_type="application/json",
            ).status_code,
            404,
        )

        # Turning on Deal later enables conversion dynamically
        enable_deal = hc_client.patch(
            f"/api/v1/workspaces/{hc_ws.slug}/tables/deal/",
            data=json.dumps({"is_enabled": True}),
            content_type="application/json",
        )
        self.assertEqual(enable_deal.status_code, 200)
        self.assertEqual(
            hc_client.post(
                f"/api/v1/workspaces/{hc_ws.slug}/records/lead/{hc_lead['id']}/convert/",
                data=json.dumps({"deal_title": "Corporate Wellness Retainer"}),
                content_type="application/json",
            ).status_code,
            200,
        )

    def test_role_permissions_and_cross_workspace_isolation(self):
        owner_client, ws, _owner = self._create_verified_workspace()
        staff_client, _staff = self._invite_and_accept_staff(owner_client, ws)
        lead1 = self._create_lead(owner_client, ws.slug, name="Lead One", company="Co One")
        lead2 = self._create_lead(owner_client, ws.slug, name="Lead Two", company="Co Two")

        staff_role = Role.objects.get(workspace=ws, is_default_staff=True)

        # Revoking deal.create blocks Staff conversion with 403
        owner_client.patch(
            f"/api/v1/workspaces/{ws.slug}/roles/{staff_role.id}/",
            data=json.dumps({"object_rights": {"deal": {"create": False}}}),
            content_type="application/json",
        )
        self.assertEqual(
            staff_client.post(
                f"/api/v1/workspaces/{ws.slug}/records/lead/{lead1['id']}/convert/",
                data=json.dumps({}),
                content_type="application/json",
            ).status_code,
            403,
        )

        # Restoring deal.create but revoking account.create blocks creating new Account,
        # while reusing an existing readable Account succeeds
        owner_client.patch(
            f"/api/v1/workspaces/{ws.slug}/roles/{staff_role.id}/",
            data=json.dumps(
                {
                    "object_rights": {
                        "deal": {"create": True},
                        "account": {"create": False, "read": True},
                    }
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(
            staff_client.post(
                f"/api/v1/workspaces/{ws.slug}/records/lead/{lead1['id']}/convert/",
                data=json.dumps({}),
                content_type="application/json",
            ).status_code,
            403,
        )

        existing_acc = owner_client.post(
            f"/api/v1/workspaces/{ws.slug}/records/account/",
            data=json.dumps({"name": "Shared Account Ltd"}),
            content_type="application/json",
        ).json()["record"]

        reuse_preview = staff_client.get(
            f"/api/v1/workspaces/{ws.slug}/records/lead/{lead2['id']}/conversion-preview/"
        )
        self.assertEqual(reuse_preview.status_code, 200)
        reuse_caps = reuse_preview.json()["capabilities"]
        self.assertFalse(reuse_caps["can_create_account"])
        self.assertTrue(reuse_caps["can_reuse_account"])
        self.assertTrue(reuse_caps["can_convert_lead"])

        # Reusing a standalone Contact requires contact.update
        standalone_con = owner_client.post(
            f"/api/v1/workspaces/{ws.slug}/records/contact/",
            data=json.dumps({"name": "Standalone Kofi"}),
            content_type="application/json",
        ).json()["record"]
        owner_client.patch(
            f"/api/v1/workspaces/{ws.slug}/roles/{staff_role.id}/",
            data=json.dumps({"object_rights": {"contact": {"create": False, "read": True, "update": False}}}),
            content_type="application/json",
        )
        standalone_forbidden = staff_client.post(
            f"/api/v1/workspaces/{ws.slug}/records/lead/{lead2['id']}/convert/",
            data=json.dumps({"account_id": existing_acc["id"], "contact_id": standalone_con["id"]}),
            content_type="application/json",
        )
        self.assertEqual(standalone_forbidden.status_code, 403)
        owner_client.patch(
            f"/api/v1/workspaces/{ws.slug}/roles/{staff_role.id}/",
            data=json.dumps({"object_rights": {"contact": {"create": True, "read": True, "update": True}}}),
            content_type="application/json",
        )

        reuse_ok = staff_client.post(
            f"/api/v1/workspaces/{ws.slug}/records/lead/{lead2['id']}/convert/",
            data=json.dumps({"account_id": existing_acc["id"]}),
            content_type="application/json",
        )
        self.assertEqual(reuse_ok.status_code, 200)

        # Cross-workspace conversion is refused with 404
        other_client, other_ws, _other_owner = self._create_verified_workspace(
            workspace_name="Other General Co",
            email="other@example.com",
        )
        self.assertEqual(
            other_client.post(
                f"/api/v1/workspaces/{ws.slug}/records/lead/{lead1['id']}/convert/",
                data=json.dumps({}),
                content_type="application/json",
            ).status_code,
            404,
        )
        self.assertEqual(
            other_client.post(
                f"/api/v1/workspaces/{other_ws.slug}/records/lead/{lead1['id']}/convert/",
                data=json.dumps({}),
                content_type="application/json",
            ).status_code,
            404,
        )
