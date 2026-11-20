import json
from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from ..models import (
    Account,
    Activity,
    AuditLog,
    Contact,
    Deal,
    Job,
    Lead,
    Role,
    Ticket,
    User,
    Workspace,
)
from ..service.audit_service import record_audit_event
from .helpers import (
    CLEAR_GH_PAYLOAD,
    extract_token_from_email_body,
    make_mock_urlopen,
)


@override_settings(
    IPINFO_TOKEN="test-ipinfo-token",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="no-reply@jojocrm.example",
)
class SearchAndAuditTests(TestCase):
    def _create_verified_workspace(
        self,
        *,
        email: str,
        workspace_name: str,
        business_type: str = "general_services",
        password: str = "StrongPass123!",
    ) -> tuple[Client, Workspace, User]:
        client = Client()
        mail.outbox.clear()
        with patch(
            "mainapp.integrations.geolocation.urllib.request.urlopen",
            return_value=make_mock_urlopen(json.dumps(CLEAR_GH_PAYLOAD).encode("utf-8")),
        ):
            res = client.post(
                "/api/v1/signup/",
                data=json.dumps(
                    {
                        "email": email,
                        "password": password,
                        "workspace_name": workspace_name,
                        "business_type": business_type,
                    }
                ),
                content_type="application/json",
                REMOTE_ADDR="154.160.1.1",
            )
        self.assertEqual(res.status_code, 201, res.content)
        token = extract_token_from_email_body(mail.outbox[-1].body)
        verify_res = client.post(
            "/api/v1/verify-email/",
            data=json.dumps({"token": token}),
            content_type="application/json",
        )
        self.assertEqual(verify_res.status_code, 200, verify_res.content)
        slug = verify_res.json()["workspace"]["slug"]
        workspace = Workspace.objects.get(slug=slug)
        user = User.objects.get(email=email.lower())
        return client, workspace, user

    def _create_member_with_role(
        self,
        workspace: Workspace,
        *,
        email: str,
        role: Role,
        password: str = "MemberPass123!",
    ) -> tuple[Client, User]:
        member = User.objects.create_user(
            email=email,
            workspace=workspace,
            role=role,
            password=password,
            email_verified=True,
            email_verified_at=timezone.now(),
            is_active=True,
        )
        member_client = Client()
        login_res = member_client.post(
            "/api/v1/login/",
            data=json.dumps({"email": email, "password": password}),
            content_type="application/json",
        )
        self.assertEqual(login_res.status_code, 200, login_res.content)
        return member_client, member

    def test_global_search_covers_enabled_objects_record_codes_activities_and_tenant_isolation(self):
        client_a, ws_a, _owner_a = self._create_verified_workspace(
            email="owner-a@search.example",
            workspace_name="Alpha Services Ltd",
            business_type="general_services",
        )
        client_b, ws_b, _owner_b = self._create_verified_workspace(
            email="owner-b@search.example",
            workspace_name="Beta Services Ltd",
            business_type="general_services",
        )

        # Create distinctive records in Workspace A
        acc_res = client_a.post(
            f"/api/v1/workspaces/{ws_a.slug}/records/account/",
            data=json.dumps({"name": "Zirconium Holdings", "city": "Accra", "notes": "DistinctiveZirconNote"}),
            content_type="application/json",
        )
        self.assertEqual(acc_res.status_code, 201)
        acc_id = acc_res.json()["record"]["id"]

        con_res = client_a.post(
            f"/api/v1/workspaces/{ws_a.slug}/records/contact/",
            data=json.dumps({"name": "Kwame Zirconium", "account_id": acc_id, "email": "kwame@zircon.example"}),
            content_type="application/json",
        )
        self.assertEqual(con_res.status_code, 201)
        con_id = con_res.json()["record"]["id"]

        deal_res = client_a.post(
            f"/api/v1/workspaces/{ws_a.slug}/records/deal/",
            data=json.dumps(
                {
                    "title": "Zirconium Transformer Supply",
                    "account_id": acc_id,
                    "contact_id": con_id,
                    "amount": "42000.00",
                    "stage": "Qualified",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(deal_res.status_code, 201)
        deal_id = deal_res.json()["record"]["id"]

        act_res = client_a.post(
            f"/api/v1/workspaces/{ws_a.slug}/records/deal/{deal_id}/activities/",
            data=json.dumps({"activity_type": "note", "summary": "Discussed Zirconium delivery schedule"}),
            content_type="application/json",
        )
        self.assertEqual(act_res.status_code, 201)
        act_id = act_res.json()["record"]["id"]

        # Create same distinctive name in Workspace B
        b_acc = client_b.post(
            f"/api/v1/workspaces/{ws_b.slug}/records/account/",
            data=json.dumps({"name": "Zirconium Foreign Tenant"}),
            content_type="application/json",
        )
        self.assertEqual(b_acc.status_code, 201)

        # Search in Workspace A by distinctive name
        res = client_a.get(f"/api/v1/workspaces/{ws_a.slug}/search/?q=Zirconium")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        titles = [hit["title"] for hit in body["results"]]
        self.assertIn("Zirconium Holdings", titles)
        self.assertIn("Kwame Zirconium", titles)
        self.assertIn("Zirconium Transformer Supply", titles)
        self.assertNotIn("Zirconium Foreign Tenant", titles)

        # Activity hit opens its parent Deal inspector
        act_hit = next(hit for hit in body["results"] if hit["object_type"] == "activity")
        self.assertEqual(act_hit["id"], act_id)
        self.assertEqual(act_hit["inspect_object_type"], "deal")
        self.assertEqual(act_hit["inspect_pane_id"], "deals")
        self.assertEqual(act_hit["inspect_record_id"], deal_id)

        # Exact record code search finds Deal and Activity cleanly
        code_res = client_a.get(f"/api/v1/workspaces/{ws_a.slug}/search/?q=DL-{deal_id}")
        self.assertEqual(code_res.status_code, 200)
        self.assertEqual(code_res.json()["total_count"], 1)
        self.assertEqual(code_res.json()["results"][0]["object_type"], "deal")
        self.assertEqual(code_res.json()["results"][0]["id"], deal_id)

        # Notes text matching works
        note_res = client_a.get(f"/api/v1/workspaces/{ws_a.slug}/search/?q=DistinctiveZirconNote")
        self.assertEqual(note_res.status_code, 200)
        self.assertEqual(note_res.json()["total_count"], 1)
        self.assertEqual(note_res.json()["results"][0]["id"], acc_id)

        # Cross-workspace search blocked (404 so tenant slugs cannot be enumerated)
        cross_res = client_a.get(f"/api/v1/workspaces/{ws_b.slug}/search/?q=Zirconium")
        self.assertEqual(cross_res.status_code, 404)

    def test_global_search_healthcare_excludes_deal_and_uses_patient_label(self):
        client, ws, _owner = self._create_verified_workspace(
            email="clinic@health.example",
            workspace_name="Korle Bu Wellness",
            business_type="healthcare_wellness",
        )
        con_res = client.post(
            f"/api/v1/workspaces/{ws.slug}/records/contact/",
            data=json.dumps({"name": "Ama Mensah Patient", "phone": "+233201234567"}),
            content_type="application/json",
        )
        self.assertEqual(con_res.status_code, 201)

        res = client.get(f"/api/v1/workspaces/{ws.slug}/search/?q=Ama")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        searchable_keys = [obj["key"] for obj in body["searchable_objects"]]
        self.assertNotIn("deal", searchable_keys)
        self.assertIn("account", searchable_keys)
        self.assertIn("contact", searchable_keys)
        self.assertIn("appointment", searchable_keys)
        self.assertIn("ticket", searchable_keys)
        self.assertIn("activity", searchable_keys)

        contact_meta = next(obj for obj in body["searchable_objects"] if obj["key"] == "contact")
        self.assertEqual(contact_meta["label"], "Patient")
        self.assertEqual(body["results"][0]["object_label"], "Patient")

        # Explicitly filtering by disabled object_type=deal returns 404
        deal_res = client.get(f"/api/v1/workspaces/{ws.slug}/search/?object_type=deal&q=Ama")
        self.assertEqual(deal_res.status_code, 404)

    def test_global_search_enforces_s24_object_field_and_sharing_permissions(self):
        owner_client, ws, owner = self._create_verified_workspace(
            email="owner-perm@search.example",
            workspace_name="Guard Services Ltd",
            business_type="general_services",
        )
        clone_res = owner_client.post(
            f"/api/v1/workspaces/{ws.slug}/roles/",
            data=json.dumps({"name": "Restricted Rep"}),
            content_type="application/json",
        )
        self.assertEqual(clone_res.status_code, 201)
        rep_role = Role.objects.get(id=clone_res.json()["role"]["id"])

        other_clone = owner_client.post(
            f"/api/v1/workspaces/{ws.slug}/roles/",
            data=json.dumps({"name": "Finance Team"}),
            content_type="application/json",
        )
        self.assertEqual(other_clone.status_code, 201)
        finance_role = Role.objects.get(id=other_clone.json()["role"]["id"])

        patch_role = owner_client.patch(
            f"/api/v1/workspaces/{ws.slug}/roles/{rep_role.id}/",
            data=json.dumps(
                {
                    "object_rights": {"job": {"create": False, "read": False, "update": False, "delete": False}},
                    "field_rights": {
                        "deal": {"notes": "hidden", "amount": "hidden"},
                        "account": {"name": "hidden"},
                    },
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(patch_role.status_code, 200)
        rep_client, _rep_user = self._create_member_with_role(ws, email="rep@search.example", role=rep_role)

        # Workspace-visible Account (name hidden to Rep, city visible)
        acc = Account.objects.create(workspace=ws, owner=owner, name="TopSecretCoName", city="Kumasi")
        # Workspace-visible Deal with secret note (notes hidden to Rep)
        deal_pub = Deal.objects.create(
            workspace=ws, owner=owner, title="Public Solar Deal", notes="HiddenSecretClause99", amount="99000.00"
        )
        # Owner-only Deal + Activity on Owner-only Deal
        deal_priv = Deal.objects.create(
            workspace=ws, owner=owner, title="Private Executive Deal", visibility="owner"
        )
        Activity.objects.create(
            workspace=ws,
            owner=owner,
            target_object_type="deal",
            target_object_id=deal_priv.id,
            summary="PrivateDealActivityMarker",
            visibility="workspace",
        )
        # Other-role Deal
        Deal.objects.create(
            workspace=ws,
            owner=owner,
            title="FinanceOnlyDealMarker",
            visibility="role",
            shared_role=finance_role,
        )
        # Role-shared Deal visible to Rep
        deal_rep = Deal.objects.create(
            workspace=ws,
            owner=owner,
            title="RepSharedDealMarker",
            visibility="role",
            shared_role=rep_role,
        )
        # Job (unreadable to Rep)
        Job.objects.create(workspace=ws, owner=owner, title="UnreadableJobMarker", account=acc)

        # 1. Job omitted from searchable_objects & explicit object_type=job -> 403
        base_res = rep_client.get(f"/api/v1/workspaces/{ws.slug}/search/")
        self.assertEqual(base_res.status_code, 200)
        self.assertNotIn("job", [o["key"] for o in base_res.json()["searchable_objects"]])
        self.assertEqual(
            rep_client.get(f"/api/v1/workspaces/{ws.slug}/search/?object_type=job").status_code,
            403,
        )

        # 2. Hidden field values cannot be matched via search
        self.assertEqual(
            rep_client.get(f"/api/v1/workspaces/{ws.slug}/search/?q=HiddenSecretClause99").json()["total_count"],
            0,
        )
        self.assertEqual(
            rep_client.get(f"/api/v1/workspaces/{ws.slug}/search/?q=TopSecretCoName").json()["total_count"],
            0,
        )

        # 3. Account matched by visible city redacts hidden name to ACC-<id>
        acc_search = rep_client.get(f"/api/v1/workspaces/{ws.slug}/search/?q=Kumasi").json()
        self.assertEqual(acc_search["total_count"], 1)
        self.assertEqual(acc_search["results"][0]["title"], f"ACC-{acc.id}")
        self.assertNotIn("TopSecretCoName", json.dumps(acc_search))

        # 4. Deal search omits hidden amount in subtitle
        pub_deal_search = rep_client.get(f"/api/v1/workspaces/{ws.slug}/search/?q=Public+Solar").json()
        self.assertEqual(pub_deal_search["total_count"], 1)
        self.assertEqual(pub_deal_search["results"][0]["id"], deal_pub.id)
        self.assertNotIn("99,000", pub_deal_search["results"][0]["subtitle"])

        # 5. Sharing visibility enforced (owner-only, other-role, and activity on owner-only deal hidden)
        self.assertEqual(
            rep_client.get(f"/api/v1/workspaces/{ws.slug}/search/?q=Private+Executive").json()["total_count"],
            0,
        )
        self.assertEqual(
            rep_client.get(f"/api/v1/workspaces/{ws.slug}/search/?q=PrivateDealActivityMarker").json()["total_count"],
            0,
        )
        self.assertEqual(
            rep_client.get(f"/api/v1/workspaces/{ws.slug}/search/?q=FinanceOnlyDealMarker").json()["total_count"],
            0,
        )
        rep_shared = rep_client.get(f"/api/v1/workspaces/{ws.slug}/search/?q=RepSharedDealMarker").json()
        self.assertEqual(rep_shared["total_count"], 1)
        self.assertEqual(rep_shared["results"][0]["id"], deal_rep.id)

    def test_global_search_pagination_and_object_filter(self):
        client, ws, owner = self._create_verified_workspace(
            email="pager@search.example",
            workspace_name="Pagination Corp",
            business_type="general_services",
        )
        base_time = timezone.now() - timedelta(hours=2)
        for i in range(1, 21):
            acc = Account.objects.create(workspace=ws, owner=owner, name=f"OmniMatch Account {i:02d}")
            Account.objects.filter(pk=acc.pk).update(updated_at=base_time + timedelta(minutes=i))
        for i in range(1, 16):
            deal = Deal.objects.create(workspace=ws, owner=owner, title=f"OmniMatch Deal {i:02d}")
            Deal.objects.filter(pk=deal.pk).update(updated_at=base_time + timedelta(minutes=20 + i))

        seen_pairs: list[tuple[str, int]] = []
        for page in range(1, 5):
            res = client.get(f"/api/v1/workspaces/{ws.slug}/search/?q=OmniMatch&page={page}&page_size=10")
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertEqual(body["total_count"], 35)
            self.assertEqual(body["total_pages"], 4)
            self.assertEqual(body["counts_by_object"]["account"], 20)
            self.assertEqual(body["counts_by_object"]["deal"], 15)
            self.assertEqual(body["has_next"], page < 4)
            self.assertEqual(body["has_previous"], page > 1)
            for hit in body["results"]:
                seen_pairs.append((hit["object_type"], hit["id"]))

        self.assertEqual(len(seen_pairs), 35)
        self.assertEqual(len(set(seen_pairs)), 35)
        # Most recently updated Deals come first
        self.assertEqual(seen_pairs[0][0], "deal")

        # Filter to Deals only
        deal_p2 = client.get(
            f"/api/v1/workspaces/{ws.slug}/search/?q=OmniMatch&object_type=deal&page=2&page_size=10"
        ).json()
        self.assertEqual(deal_p2["total_count"], 15)
        self.assertEqual(deal_p2["all_objects_total_count"], 35)
        self.assertEqual(len(deal_p2["results"]), 5)
        self.assertTrue(all(hit["object_type"] == "deal" for hit in deal_p2["results"]))

    def test_relation_picker_pagination_beyond_250_and_compatible_account_filter(self):
        client, ws, owner = self._create_verified_workspace(
            email="scale@pickers.example",
            workspace_name="Scale Pickers Ltd",
            business_type="general_services",
        )
        accounts = [
            Account(workspace=ws, owner=owner, name=f"Vendor Account {i:03d}")
            for i in range(1, 266)
        ]
        Account.objects.bulk_create(accounts)
        all_accs = list(Account.objects.filter(workspace=ws).order_by("name"))
        acc_target = all_accs[259]  # Vendor Account 260 (beyond first 250)
        acc_other = all_accs[0]

        contacts = [
            Contact(workspace=ws, owner=owner, name=f"Directory Person {i:03d}", account=acc_other)
            for i in range(1, 256)
        ]
        contacts.append(Contact(workspace=ws, owner=owner, name="Target Account Contact 260", account=acc_target))
        contacts.append(Contact(workspace=ws, owner=owner, name="Standalone Deep Contact 261", account=None))
        Contact.objects.bulk_create(contacts)

        opts = client.get(f"/api/v1/workspaces/{ws.slug}/crm/options/").json()
        self.assertEqual(opts["accounts_total_count"], 265)
        self.assertTrue(opts["accounts_truncated"])
        self.assertEqual(len(opts["accounts"]), 250)
        self.assertTrue(opts["contacts_truncated"])

        # Server-backed search & pagination finds Account #260 cleanly
        acc_search = client.get(
            f"/api/v1/workspaces/{ws.slug}/records/account/?q=Vendor+Account+260&page=1&page_size=25"
        ).json()
        self.assertEqual(acc_search["total_count"], 1)
        self.assertEqual(acc_search["results"][0]["id"], acc_target.id)

        # Pagination beyond page 10 (25 * 10 = 250) reaches page 11
        acc_page11 = client.get(
            f"/api/v1/workspaces/{ws.slug}/records/account/?page=11&page_size=25"
        ).json()
        self.assertEqual(acc_page11["total_pages"], 11)
        self.assertEqual(len(acc_page11["results"]), 15)

        # compatible_account_id returns ONLY target account's contacts + standalone contacts
        compat = client.get(
            f"/api/v1/workspaces/{ws.slug}/records/contact/?compatible_account_id={acc_target.id}&page=1&page_size=25"
        ).json()
        self.assertEqual(compat["total_count"], 2)
        compat_names = {row["name"] for row in compat["results"]}
        self.assertEqual(
            compat_names,
            {"Target Account Contact 260", "Standalone Deep Contact 261"},
        )

    def test_deal_stage_change_audit_and_workspace_audit_filters_and_pagination(self):
        client, ws, owner = self._create_verified_workspace(
            email="flow@audit.example",
            workspace_name="Accra Engineering Services",
            business_type="general_services",
        )

        # Acceptance 4 flow: Create Lead -> Convert -> Move Deal across stages -> Create Job
        lead_res = client.post(
            f"/api/v1/workspaces/{ws.slug}/records/lead/",
            data=json.dumps(
                {
                    "name": "Kojo Boateng",
                    "company": "Volta Grid Co",
                    "email": "kojo@voltagrid.example",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(lead_res.status_code, 201)
        lead_id = lead_res.json()["record"]["id"]

        conv_res = client.post(
            f"/api/v1/workspaces/{ws.slug}/records/lead/{lead_id}/convert/",
            data=json.dumps({"deal_title": "Volta Grid Upgrade", "deal_amount": "85000.00"}),
            content_type="application/json",
        )
        self.assertEqual(conv_res.status_code, 200)
        deal_id = conv_res.json()["deal"]["id"]
        acc_id = conv_res.json()["account"]["id"]
        initial_stage = conv_res.json()["deal"]["stage"]

        move_res = client.patch(
            f"/api/v1/workspaces/{ws.slug}/records/deal/{deal_id}/",
            data=json.dumps({"stage": "Proposal"}),
            content_type="application/json",
        )
        self.assertEqual(move_res.status_code, 200)

        move2_res = client.patch(
            f"/api/v1/workspaces/{ws.slug}/records/deal/{deal_id}/",
            data=json.dumps({"stage": "Won"}),
            content_type="application/json",
        )
        self.assertEqual(move2_res.status_code, 200)

        job_res = client.post(
            f"/api/v1/workspaces/{ws.slug}/records/job/",
            data=json.dumps({"title": "Site Commissioning", "account_id": acc_id}),
            content_type="application/json",
        )
        self.assertEqual(job_res.status_code, 201)

        # 1. Per-record audit endpoint shows stage moves with person, time, old stage, new stage
        rec_audit = client.get(
            f"/api/v1/workspaces/{ws.slug}/records/deal/{deal_id}/audit/?page=1&page_size=10"
        )
        self.assertEqual(rec_audit.status_code, 200)
        rec_entries = rec_audit.json()["results"]
        stage_entries = [e for e in rec_entries if e["field_name"] == "stage"]
        self.assertEqual(len(stage_entries), 2)
        self.assertEqual(stage_entries[0]["actor"], owner.email)
        self.assertEqual(stage_entries[0]["actor_kind"], "person")
        self.assertEqual(stage_entries[0]["previous_value"], "Proposal")
        self.assertEqual(stage_entries[0]["new_value"], "Won")
        self.assertTrue(stage_entries[0]["created_at"])
        self.assertEqual(stage_entries[1]["previous_value"], initial_stage)
        self.assertEqual(stage_entries[1]["new_value"], "Proposal")

        # 2. Workspace audit filters by object_type, action, actor_kind, and q (value search 'Proposal')
        filtered = client.get(
            f"/api/v1/workspaces/{ws.slug}/audit/?object_type=deal&action=update&actor_kind=person&q=Proposal"
        )
        self.assertEqual(filtered.status_code, 200)
        filtered_body = filtered.json()
        self.assertEqual(filtered_body["total_count"], 2)
        self.assertTrue(all(e["object_type"] == "deal" and e["action"] == "update" for e in filtered_body["results"]))
        self.assertTrue(all(e["can_inspect"] is True and e["inspect_pane_id"] == "deals" for e in filtered_body["results"]))

        # 3. Create enough audit entries to test pagination across pages
        for i in range(25):
            record_audit_event(
                workspace=ws,
                object_type="account",
                object_id=acc_id,
                record_label="Volta Grid Co",
                action=AuditLog.ACTION_UPDATE,
                field_name="notes",
                previous_value=f"note-{i}",
                new_value=f"note-{i + 1}",
                actor_kind=AuditLog.ACTOR_KIND_SCRIPT,
                actor_label="SyncScript",
            )

        p1 = client.get(f"/api/v1/workspaces/{ws.slug}/audit/?actor_kind=script&page=1&page_size=10").json()
        p2 = client.get(f"/api/v1/workspaces/{ws.slug}/audit/?actor_kind=script&page=2&page_size=10").json()
        p3 = client.get(f"/api/v1/workspaces/{ws.slug}/audit/?actor_kind=script&page=3&page_size=10").json()
        self.assertEqual(p1["total_count"], 25)
        self.assertEqual(p1["total_pages"], 3)
        self.assertEqual(len(p1["results"]), 10)
        self.assertEqual(len(p2["results"]), 10)
        self.assertEqual(len(p3["results"]), 5)
        all_ids = [e["id"] for e in (p1["results"] + p2["results"] + p3["results"])]
        self.assertEqual(len(set(all_ids)), 25)

        # Invalid filters return 400
        self.assertEqual(client.get(f"/api/v1/workspaces/{ws.slug}/audit/?action=invalid").status_code, 400)
        self.assertEqual(client.get(f"/api/v1/workspaces/{ws.slug}/audit/?object_type=nonexistent").status_code, 400)

    def test_audit_strict_s24_enforcement_objects_hidden_fields_live_and_deleted_sharing(self):
        owner_client, ws, owner = self._create_verified_workspace(
            email="owner-audit@guard.example",
            workspace_name="Audit Guard Co",
            business_type="general_services",
        )

        # Clone a non-Owner role WITH users_and_roles=True to verify CRM S2.4 rules still apply!
        clone_res = owner_client.post(
            f"/api/v1/workspaces/{ws.slug}/roles/",
            data=json.dumps({"name": "Scoped Manager"}),
            content_type="application/json",
        )
        self.assertEqual(clone_res.status_code, 201)
        mgr_role = Role.objects.get(id=clone_res.json()["role"]["id"])

        other_res = owner_client.post(
            f"/api/v1/workspaces/{ws.slug}/roles/",
            data=json.dumps({"name": "Executive Role"}),
            content_type="application/json",
        )
        exec_role = Role.objects.get(id=other_res.json()["role"]["id"])

        patch_res = owner_client.patch(
            f"/api/v1/workspaces/{ws.slug}/roles/{mgr_role.id}/",
            data=json.dumps(
                {
                    "switches": {"users_and_roles": True},
                    "object_rights": {"job": {"create": False, "read": False, "update": False, "delete": False}},
                    "field_rights": {
                        "deal": {"amount": "hidden", "estimated_margin": "hidden"},
                        "account": {"name": "hidden"},
                    },
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(patch_res.status_code, 200)
        mgr_client, _mgr_user = self._create_member_with_role(ws, email="mgr@guard.example", role=mgr_role)

        # 1. Account with hidden name -> create + update city
        acc_res = owner_client.post(
            f"/api/v1/workspaces/{ws.slug}/records/account/",
            data=json.dumps({"name": "ConfidentialGoldMine", "city": "Accra"}),
            content_type="application/json",
        )
        acc_id = acc_res.json()["record"]["id"]
        owner_client.patch(
            f"/api/v1/workspaces/{ws.slug}/records/account/{acc_id}/",
            data=json.dumps({"city": "Takoradi"}),
            content_type="application/json",
        )

        # 2. Workspace-visible Deal -> update stage (visible) and amount (hidden)
        pub_deal_res = owner_client.post(
            f"/api/v1/workspaces/{ws.slug}/records/deal/",
            data=json.dumps({"title": "Visible Deal", "amount": "1000.00", "stage": "New"}),
            content_type="application/json",
        )
        pub_deal_id = pub_deal_res.json()["record"]["id"]
        owner_client.patch(
            f"/api/v1/workspaces/{ws.slug}/records/deal/{pub_deal_id}/",
            data=json.dumps({"stage": "Qualified", "amount": "99999.00"}),
            content_type="application/json",
        )

        # 3. Live owner-only Deal + Activity on it, and other-role Deal
        priv_deal_res = owner_client.post(
            f"/api/v1/workspaces/{ws.slug}/records/deal/",
            data=json.dumps({"title": "OwnerOnlyDealSecret", "visibility": "owner"}),
            content_type="application/json",
        )
        priv_deal_id = priv_deal_res.json()["record"]["id"]
        owner_client.post(
            f"/api/v1/workspaces/{ws.slug}/records/deal/{priv_deal_id}/activities/",
            data=json.dumps({"activity_type": "note", "summary": "OwnerOnlyDealActivitySecret"}),
            content_type="application/json",
        )
        other_role_deal_res = owner_client.post(
            f"/api/v1/workspaces/{ws.slug}/records/deal/",
            data=json.dumps(
                {
                    "title": "ExecOnlyDealSecret",
                    "visibility": "role",
                    "shared_role_id": exec_role.id,
                }
            ),
            content_type="application/json",
        )
        other_role_deal_id = other_role_deal_res.json()["record"]["id"]

        # 4. Deleted owner-only Deal (started workspace -> edited -> restricted to owner -> deleted)
        del_priv_res = owner_client.post(
            f"/api/v1/workspaces/{ws.slug}/records/deal/",
            data=json.dumps({"title": "DeletedPrivateDealSecret", "visibility": "workspace"}),
            content_type="application/json",
        )
        del_priv_id = del_priv_res.json()["record"]["id"]
        owner_client.patch(
            f"/api/v1/workspaces/{ws.slug}/records/deal/{del_priv_id}/",
            data=json.dumps({"stage": "Proposal", "visibility": "owner"}),
            content_type="application/json",
        )
        owner_client.delete(f"/api/v1/workspaces/{ws.slug}/records/deal/{del_priv_id}/")

        # 5. Deleted workspace-visible Deal (remains visible in audit history)
        del_pub_res = owner_client.post(
            f"/api/v1/workspaces/{ws.slug}/records/deal/",
            data=json.dumps({"title": "DeletedPublicDeal", "visibility": "workspace"}),
            content_type="application/json",
        )
        del_pub_id = del_pub_res.json()["record"]["id"]
        owner_client.delete(f"/api/v1/workspaces/{ws.slug}/records/deal/{del_pub_id}/")

        # 6. Unreadable Job
        owner_client.post(
            f"/api/v1/workspaces/{ws.slug}/records/job/",
            data=json.dumps({"title": "HiddenJobSecret", "account_id": acc_id}),
            content_type="application/json",
        )

        # Query workspace audit as Scoped Manager (non-Owner with users_and_roles=True)
        mgr_audit = mgr_client.get(f"/api/v1/workspaces/{ws.slug}/audit/?page_size=100").json()
        mgr_entries = mgr_audit["results"]
        mgr_dump = json.dumps(mgr_entries)

        # Unreadable Job excluded from results and available_object_types
        self.assertNotIn("job", [e["object_type"] for e in mgr_entries])
        self.assertNotIn("job", [opt["key"] for opt in mgr_audit["available_object_types"]])

        # Hidden Deal.amount field omitted from both workspace audit & per-record audit
        self.assertFalse(
            any(e["object_type"] == "deal" and e["field_name"] == "amount" for e in mgr_entries)
        )
        rec_deal_audit = mgr_client.get(
            f"/api/v1/workspaces/{ws.slug}/records/deal/{pub_deal_id}/audit/"
        ).json()["results"]
        self.assertTrue(any(e["field_name"] == "stage" for e in rec_deal_audit))
        self.assertFalse(any(e["field_name"] == "amount" for e in rec_deal_audit))

        # Hidden Account.name redacted & cannot be probed via search q
        self.assertNotIn("ConfidentialGoldMine", mgr_dump)
        acc_entries = [e for e in mgr_entries if e["object_type"] == "account" and e["object_id"] == str(acc_id)]
        self.assertTrue(len(acc_entries) >= 2)
        self.assertTrue(all(e["record_label"] == f"ACC-{acc_id}" for e in acc_entries))
        probe_res = mgr_client.get(f"/api/v1/workspaces/{ws.slug}/audit/?q=ConfidentialGoldMine").json()
        self.assertEqual(probe_res["total_count"], 0)

        # Live owner-only Deal, other-role Deal, and Activity on owner-only Deal excluded
        self.assertNotIn("OwnerOnlyDealSecret", mgr_dump)
        self.assertNotIn("OwnerOnlyDealActivitySecret", mgr_dump)
        self.assertNotIn("ExecOnlyDealSecret", mgr_dump)
        self.assertEqual(
            mgr_client.get(f"/api/v1/workspaces/{ws.slug}/records/deal/{priv_deal_id}/audit/").status_code,
            404,
        )
        self.assertEqual(
            mgr_client.get(f"/api/v1/workspaces/{ws.slug}/records/deal/{other_role_deal_id}/audit/").status_code,
            404,
        )

        # Deleted owner-only Deal omitted across ALL its historical entries (create, update, delete)
        self.assertFalse(
            any(e["object_type"] == "deal" and e["object_id"] == str(del_priv_id) for e in mgr_entries)
        )
        self.assertNotIn("DeletedPrivateDealSecret", mgr_dump)

        # Deleted workspace-visible Deal IS still visible (with can_inspect=False)
        del_pub_entries = [
            e for e in mgr_entries if e["object_type"] == "deal" and e["object_id"] == str(del_pub_id)
        ]
        self.assertEqual(len(del_pub_entries), 2)
        self.assertTrue(all(e["can_inspect"] is False for e in del_pub_entries))

        # Owner still sees everything (hidden fields, private deals, deleted private deals, jobs)
        owner_audit = owner_client.get(f"/api/v1/workspaces/{ws.slug}/audit/?page_size=100").json()
        owner_dump = json.dumps(owner_audit["results"])
        self.assertIn("ConfidentialGoldMine", owner_dump)
        self.assertIn("OwnerOnlyDealSecret", owner_dump)
        self.assertIn("DeletedPrivateDealSecret", owner_dump)
        self.assertIn("HiddenJobSecret", owner_dump)
        self.assertTrue(
            any(
                e["object_type"] == "deal" and e["field_name"] == "amount"
                for e in owner_audit["results"]
            )
        )
