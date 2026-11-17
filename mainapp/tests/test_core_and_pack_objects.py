import json
from unittest.mock import patch

from django.core import mail
from django.test import Client, TestCase, override_settings

from ..models import (
    Account,
    Activity,
    Appointment,
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
class CoreAndPackObjectsTests(TestCase):
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

    def _create_sample_eight_objects(self, client, slug, owner_id):
        acc = client.post(
            f"/api/v1/workspaces/{slug}/records/account/",
            data=json.dumps(
                {
                    "name": "Meridian Port Services",
                    "city": "Tema",
                    "phone": "+233 30 321 4400",
                    "email": "ops@meridian.com.gh",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(acc.status_code, 201)
        account_id = acc.json()["record"]["id"]

        con = client.post(
            f"/api/v1/workspaces/{slug}/records/contact/",
            data=json.dumps(
                {
                    "name": "Kofi Boateng",
                    "title": "Engineering Lead",
                    "account_id": account_id,
                    "email": "kofi@meridian.com.gh",
                    "phone": "+233 24 551 2201",
                    "sms_consent": True,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(con.status_code, 201)
        contact_id = con.json()["record"]["id"]

        lead = client.post(
            f"/api/v1/workspaces/{slug}/records/lead/",
            data=json.dumps(
                {
                    "name": "Akua Mensah",
                    "company": "Ghana Water Co",
                    "email": "akua@gwcl.example",
                    "phone": "+233 24 100 2000",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(lead.status_code, 201)
        lead_id = lead.json()["record"]["id"]

        deal = client.post(
            f"/api/v1/workspaces/{slug}/records/deal/",
            data=json.dumps(
                {
                    "title": "Tema Conveyor Retrofit",
                    "account_id": account_id,
                    "contact_id": contact_id,
                    "amount": "64000.00",
                    "stage": "Qualified",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(deal.status_code, 201)
        deal_id = deal.json()["record"]["id"]

        job = client.post(
            f"/api/v1/workspaces/{slug}/records/job/",
            data=json.dumps(
                {
                    "title": "Berth 3 Valve Inspection",
                    "status": "In Progress",
                    "assignee_id": owner_id,
                    "due_date": "2026-12-01",
                    "account_id": account_id,
                    "contact_id": contact_id,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(job.status_code, 201)
        job_id = job.json()["record"]["id"]

        apt = client.post(
            f"/api/v1/workspaces/{slug}/records/appointment/",
            data=json.dumps(
                {
                    "title": "Site Walkthrough",
                    "scheduled_at": "2026-11-20T10:30:00Z",
                    "status": "Confirmed",
                    "assignee_id": owner_id,
                    "contact_id": contact_id,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(apt.status_code, 201)
        apt_id = apt.json()["record"]["id"]

        tck = client.post(
            f"/api/v1/workspaces/{slug}/records/ticket/",
            data=json.dumps(
                {
                    "title": "Valve Seal Replacement",
                    "status": "Open",
                    "priority": "High",
                    "account_id": account_id,
                    "contact_id": contact_id,
                    "assignee_id": owner_id,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(tck.status_code, 201)
        tck_id = tck.json()["record"]["id"]

        act = client.post(
            f"/api/v1/workspaces/{slug}/records/activity/",
            data=json.dumps(
                {
                    "activity_type": "note",
                    "target_object_type": "deal",
                    "target_object_id": deal_id,
                    "summary": "Initial engineering review completed.",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(act.status_code, 201)
        act_id = act.json()["record"]["id"]

        return {
            "account": account_id,
            "contact": contact_id,
            "lead": lead_id,
            "deal": deal_id,
            "job": job_id,
            "appointment": apt_id,
            "ticket": tck_id,
            "activity": act_id,
        }

    def test_general_services_crud_and_audit_across_all_eight_objects(self):
        owner_client, workspace, owner = self._create_verified_workspace()
        slug = workspace.slug
        ids = self._create_sample_eight_objects(owner_client, slug, owner.id)

        # Standalone Contact without Account is allowed ("A Contact can sit on an Account")
        standalone_con = owner_client.post(
            f"/api/v1/workspaces/{slug}/records/contact/",
            data=json.dumps({"name": "Ama Serwaa", "email": "ama@independent.example"}),
            content_type="application/json",
        )
        self.assertEqual(standalone_con.status_code, 201)
        self.assertIsNone(standalone_con.json()["record"]["account_id"])

        # Lead stays open and separate
        lead_detail = owner_client.get(
            f"/api/v1/workspaces/{slug}/records/lead/{ids['lead']}/"
        ).json()["record"]
        self.assertEqual(lead_detail["status"], "open")

        # Update each of the 8 objects as Owner
        updates = {
            "account": {"city": "Accra"},
            "contact": {"title": "Principal Engineer"},
            "lead": {"company": "GWCL Head Office"},
            "deal": {"amount": "72500.00", "stage": "Proposal"},
            "job": {"status": "Completed"},
            "appointment": {"status": "Completed"},
            "ticket": {"status": "Resolved"},
            "activity": {"summary": "Updated review notes.", "is_completed": True},
        }
        for obj_key, payload in updates.items():
            res = owner_client.patch(
                f"/api/v1/workspaces/{slug}/records/{obj_key}/{ids[obj_key]}/",
                data=json.dumps(payload),
                content_type="application/json",
            )
            self.assertEqual(res.status_code, 200, f"Failed update for {obj_key}: {res.content}")
            self.assertTrue(res.json()["changed"])

        # Verify audit entries exist for create and update
        self.assertTrue(
            AuditLog.objects.filter(
                workspace=workspace,
                object_type="deal",
                object_id=str(ids["deal"]),
                action=AuditLog.ACTION_UPDATE,
                field_name="stage",
                previous_value="Qualified",
                new_value="Proposal",
                actor_kind=AuditLog.ACTOR_KIND_PERSON,
                actor_user=owner,
            ).exists()
        )

        # Delete in dependency-safe order (activity, ticket, appointment, job, deal, lead, contact, account)
        for obj_key in ("activity", "ticket", "appointment", "job", "deal", "lead", "contact", "account"):
            del_res = owner_client.delete(
                f"/api/v1/workspaces/{slug}/records/{obj_key}/{ids[obj_key]}/"
            )
            self.assertEqual(del_res.status_code, 200, f"Failed delete for {obj_key}: {del_res.content}")

        self.assertEqual(Account.objects.filter(workspace=workspace).count(), 0)
        self.assertEqual(Contact.objects.filter(workspace=workspace).count(), 1)  # standalone_con remains

    def test_role_object_rights_allow_and_block_edits(self):
        owner_client, workspace, owner = self._create_verified_workspace()
        staff_client, staff = self._invite_and_accept_staff(owner_client, workspace)
        slug = workspace.slug
        ids = self._create_sample_eight_objects(owner_client, slug, owner.id)

        # Default Staff has create/read/update=True and delete=False
        for obj_key, rec_id in ids.items():
            get_res = staff_client.get(f"/api/v1/workspaces/{slug}/records/{obj_key}/{rec_id}/")
            self.assertEqual(get_res.status_code, 200)
            del_res = staff_client.delete(f"/api/v1/workspaces/{slug}/records/{obj_key}/{rec_id}/")
            self.assertEqual(del_res.status_code, 403)

        # Staff can edit each object while update right is granted
        self.assertEqual(
            staff_client.patch(
                f"/api/v1/workspaces/{slug}/records/account/{ids['account']}/",
                data=json.dumps({"city": "Takoradi"}),
                content_type="application/json",
            ).status_code,
            200,
        )

        # Staff cannot modify roles; Owner cannot be restricted
        owner_role = Role.objects.get(workspace=workspace, is_owner=True)
        staff_role = Role.objects.get(workspace=workspace, is_default_staff=True)
        self.assertEqual(
            staff_client.patch(
                f"/api/v1/workspaces/{slug}/roles/{staff_role.id}/",
                data=json.dumps({"object_rights": {"account": {"update": False}}}),
                content_type="application/json",
            ).status_code,
            403,
        )
        self.assertEqual(
            owner_client.patch(
                f"/api/v1/workspaces/{slug}/roles/{owner_role.id}/",
                data=json.dumps({"object_rights": {"account": {"update": False}}}),
                content_type="application/json",
            ).status_code,
            403,
        )

        # Owner revokes update rights on all 8 objects for Staff
        revoke_payload = {
            "object_rights": {
                obj_key: {"update": False}
                for obj_key in ids
            }
        }
        role_res = owner_client.patch(
            f"/api/v1/workspaces/{slug}/roles/{staff_role.id}/",
            data=json.dumps(revoke_payload),
            content_type="application/json",
        )
        self.assertEqual(role_res.status_code, 200)

        # Staff is now blocked from editing each of the 8 objects
        patch_bodies = {
            "account": {"city": "Kumasi"},
            "contact": {"title": "VP"},
            "lead": {"company": "New Co"},
            "deal": {"amount": "90000.00"},
            "job": {"status": "Completed"},
            "appointment": {"status": "Cancelled"},
            "ticket": {"priority": "Low"},
            "activity": {"summary": "Blocked edit"},
        }
        for obj_key, body in patch_bodies.items():
            blocked_res = staff_client.patch(
                f"/api/v1/workspaces/{slug}/records/{obj_key}/{ids[obj_key]}/",
                data=json.dumps(body),
                content_type="application/json",
            )
            self.assertEqual(blocked_res.status_code, 403, f"Expected 403 for {obj_key}")
            # Owner still succeeds
            ok_res = owner_client.patch(
                f"/api/v1/workspaces/{slug}/records/{obj_key}/{ids[obj_key]}/",
                data=json.dumps(body),
                content_type="application/json",
            )
            self.assertEqual(ok_res.status_code, 200, f"Expected 200 for Owner on {obj_key}")

        # Revoking read/create on Deal blocks Staff GET and POST on Deal
        owner_client.patch(
            f"/api/v1/workspaces/{slug}/roles/{staff_role.id}/",
            data=json.dumps({"object_rights": {"deal": {"create": False, "read": False}}}),
            content_type="application/json",
        )
        self.assertEqual(
            staff_client.get(f"/api/v1/workspaces/{slug}/records/deal/").status_code,
            403,
        )
        self.assertEqual(
            staff_client.post(
                f"/api/v1/workspaces/{slug}/records/deal/",
                data=json.dumps({"title": "Blocked Deal"}),
                content_type="application/json",
            ).status_code,
            403,
        )

    def test_activity_four_types_on_records_and_rejects_email_type(self):
        owner_client, workspace, owner = self._create_verified_workspace()
        slug = workspace.slug
        ids = self._create_sample_eight_objects(owner_client, slug, owner.id)

        for act_type, text in (
            ("note", "Captured site measurements."),
            ("task", "Prepare revised BOQ."),
            ("call", "Spoke with Kofi on delivery slot."),
            ("meeting", "On-site walkthrough at Tema Berth 3."),
        ):
            res = owner_client.post(
                f"/api/v1/workspaces/{slug}/records/account/{ids['account']}/activities/",
                data=json.dumps({"activity_type": act_type, "summary": text}),
                content_type="application/json",
            )
            self.assertEqual(res.status_code, 201)

        bad_type = owner_client.post(
            f"/api/v1/workspaces/{slug}/records/account/{ids['account']}/activities/",
            data=json.dumps({"activity_type": "email", "summary": "Should not save in S2.1"}),
            content_type="application/json",
        )
        self.assertEqual(bad_type.status_code, 400)

        detail = owner_client.get(
            f"/api/v1/workspaces/{slug}/records/account/{ids['account']}/"
        ).json()["record"]
        saved_types = {a["activity_type"] for a in detail["activities"]}
        self.assertEqual(saved_types, {"note", "task", "call", "meeting"})

    def test_job_and_appointment_links_autofill_and_deletion_guards(self):
        owner_client, workspace, owner = self._create_verified_workspace()
        slug = workspace.slug

        acc_a = owner_client.post(
            f"/api/v1/workspaces/{slug}/records/account/",
            data=json.dumps({"name": "Alpha Co"}),
            content_type="application/json",
        ).json()["record"]
        acc_b = owner_client.post(
            f"/api/v1/workspaces/{slug}/records/account/",
            data=json.dumps({"name": "Beta Co"}),
            content_type="application/json",
        ).json()["record"]
        con_a = owner_client.post(
            f"/api/v1/workspaces/{slug}/records/contact/",
            data=json.dumps({"name": "Kwame Owusu", "account_id": acc_a["id"]}),
            content_type="application/json",
        ).json()["record"]

        # Job without Account or Contact is refused
        bad_job = owner_client.post(
            f"/api/v1/workspaces/{slug}/records/job/",
            data=json.dumps({"title": "Unlinked Job", "status": "Pending"}),
            content_type="application/json",
        )
        self.assertEqual(bad_job.status_code, 400)

        # Appointment without Contact is refused
        bad_apt = owner_client.post(
            f"/api/v1/workspaces/{slug}/records/appointment/",
            data=json.dumps({"title": "No Contact", "scheduled_at": "2026-11-22T09:00:00Z"}),
            content_type="application/json",
        )
        self.assertEqual(bad_apt.status_code, 400)

        # Mismatched Account + Contact is refused
        mismatch_deal = owner_client.post(
            f"/api/v1/workspaces/{slug}/records/deal/",
            data=json.dumps(
                {
                    "title": "Mismatch Deal",
                    "account_id": acc_b["id"],
                    "contact_id": con_a["id"],
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(mismatch_deal.status_code, 400)

        # Omitting Account on Job/Deal when Contact belongs to Alpha Co auto-fills Alpha Co
        auto_job = owner_client.post(
            f"/api/v1/workspaces/{slug}/records/job/",
            data=json.dumps(
                {
                    "title": "Pump Calibration",
                    "contact_id": con_a["id"],
                    "assignee_id": owner.id,
                    "due_date": "2026-12-05",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(auto_job.status_code, 201)
        job_rec = auto_job.json()["record"]
        self.assertEqual(job_rec["account_id"], acc_a["id"])
        self.assertEqual(job_rec["contact_id"], con_a["id"])

        # Account-only Job prevents deleting Account B with 409
        acc_only_job = owner_client.post(
            f"/api/v1/workspaces/{slug}/records/job/",
            data=json.dumps({"title": "Beta Survey", "account_id": acc_b["id"]}),
            content_type="application/json",
        )
        self.assertEqual(acc_only_job.status_code, 201)
        self.assertEqual(
            owner_client.delete(f"/api/v1/workspaces/{slug}/records/account/{acc_b['id']}/").status_code,
            409,
        )

        # Appointment prevents deleting Contact with 409
        apt_res = owner_client.post(
            f"/api/v1/workspaces/{slug}/records/appointment/",
            data=json.dumps(
                {
                    "scheduled_at": "2026-11-23T11:00:00Z",
                    "contact_id": con_a["id"],
                    "assignee_id": owner.id,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(apt_res.status_code, 201)
        self.assertEqual(
            owner_client.delete(f"/api/v1/workspaces/{slug}/records/contact/{con_a['id']}/").status_code,
            409,
        )

        # Changing Contact's Account while paired Job exists is refused with 409
        self.assertEqual(
            owner_client.patch(
                f"/api/v1/workspaces/{slug}/records/contact/{con_a['id']}/",
                data=json.dumps({"account_id": acc_b["id"]}),
                content_type="application/json",
            ).status_code,
            409,
        )

    def test_cross_workspace_isolation_and_pack_table_gating(self):
        client_a, ws_a, owner_a = self._create_verified_workspace(
            workspace_name="Alpha General",
            business_type="general_services",
            email="alpha@example.com",
        )
        client_b, ws_b, _owner_b = self._create_verified_workspace(
            workspace_name="Beta Food Co",
            business_type="food_beverage",
            email="beta@example.com",
        )
        ids_a = self._create_sample_eight_objects(client_a, ws_a.slug, owner_a.id)

        # Cross-workspace read/write/delete is refused with 404
        self.assertEqual(
            client_b.get(f"/api/v1/workspaces/{ws_a.slug}/records/account/").status_code,
            404,
        )
        self.assertEqual(
            client_b.get(
                f"/api/v1/workspaces/{ws_a.slug}/records/account/{ids_a['account']}/"
            ).status_code,
            404,
        )
        self.assertEqual(
            client_b.get(
                f"/api/v1/workspaces/{ws_b.slug}/records/account/{ids_a['account']}/"
            ).status_code,
            404,
        )
        # Linking Workspace A Contact into Workspace B Appointment is rejected
        cross_apt = client_b.post(
            f"/api/v1/workspaces/{ws_b.slug}/records/appointment/",
            data=json.dumps(
                {
                    "title": "Cross Link",
                    "scheduled_at": "2026-11-24T12:00:00Z",
                    "contact_id": ids_a["contact"],
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(cross_apt.status_code, 400)

        # Food and beverage does not offer Deal table
        self.assertEqual(
            client_b.get(f"/api/v1/workspaces/{ws_b.slug}/records/deal/").status_code,
            404,
        )
        self.assertEqual(
            client_b.post(
                f"/api/v1/workspaces/{ws_b.slug}/records/deal/",
                data=json.dumps({"title": "Disallowed Deal"}),
                content_type="application/json",
            ).status_code,
            404,
        )

        # Healthcare labels Contact as Patient, excludes Deal, and books Appointment on Patient
        hc_client, hc_ws, hc_owner = self._create_verified_workspace(
            workspace_name="Korle Clinic",
            business_type="healthcare_wellness",
            email="clinic@example.com",
        )
        self.assertEqual(
            hc_client.get(f"/api/v1/workspaces/{hc_ws.slug}/records/deal/").status_code,
            404,
        )
        patient = hc_client.post(
            f"/api/v1/workspaces/{hc_ws.slug}/records/contact/",
            data=json.dumps({"name": "Yaw Badu", "phone": "+233 24 900 1122"}),
            content_type="application/json",
        )
        self.assertEqual(patient.status_code, 201)
        hc_apt = hc_client.post(
            f"/api/v1/workspaces/{hc_ws.slug}/records/appointment/",
            data=json.dumps(
                {
                    "title": "Morning Checkup",
                    "scheduled_at": "2026-11-25T09:00:00Z",
                    "status": "Scheduled",
                    "assignee_id": hc_owner.id,
                    "contact_id": patient.json()["record"]["id"],
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(hc_apt.status_code, 201)

        # Non-empty pack table protection: Appointment holds 1 row so cannot be turned off (409)
        packs_res = hc_client.get(f"/api/v1/workspaces/{hc_ws.slug}/packs/").json()
        apt_tbl = next(t for t in packs_res["tables"] if t["key"] == "appointment")
        self.assertEqual(apt_tbl["record_count"], 1)
        self.assertFalse(apt_tbl["can_disable"])
        self.assertEqual(
            hc_client.patch(
                f"/api/v1/workspaces/{hc_ws.slug}/tables/appointment/",
                data=json.dumps({"is_enabled": False}),
                content_type="application/json",
            ).status_code,
            409,
        )

    def test_dashboard_summary_and_chart_summaries_and_metadata(self):
        owner_client, ws, owner = self._create_verified_workspace(
            workspace_name="Accra Insights Ltd",
            business_type="general_services",
            email="insights@accra.example",
        )
        staff_client, staff_user = self._invite_and_accept_staff(
            owner_client,
            ws,
            staff_email="rep@accra.example",
        )

        # Empty workspace reports total_records == 0
        empty_dash = owner_client.get(f"/api/v1/workspaces/{ws.slug}/dashboard/")
        self.assertEqual(empty_dash.status_code, 200)
        self.assertEqual(empty_dash.json()["total_records"], 0)
        self.assertEqual(empty_dash.json()["recent_records"], [])

        ids = self._create_sample_eight_objects(owner_client, ws.slug, owner.id)

        # Dashboard returns live counts, pipeline summary, open metrics, and recent records
        dash_res = owner_client.get(f"/api/v1/workspaces/{ws.slug}/dashboard/")
        self.assertEqual(dash_res.status_code, 200)
        dash = dash_res.json()
        self.assertGreaterEqual(dash["total_records"], 8)
        self.assertEqual(dash["counts_by_object"]["account"], 1)
        self.assertEqual(dash["counts_by_object"]["deal"], 1)
        self.assertIsNotNone(dash["deal_summary"])
        self.assertEqual(dash["deal_summary"]["total_count"], 1)
        self.assertGreater(len(dash["recent_records"]), 0)

        # Account city_counts and Ticket priority_counts summaries
        acc_list = owner_client.get(f"/api/v1/workspaces/{ws.slug}/records/account/").json()
        self.assertEqual(acc_list["summary"]["city_counts"], [{"city": "Tema", "count": 1}])

        tck_list = owner_client.get(f"/api/v1/workspaces/{ws.slug}/records/ticket/").json()
        self.assertEqual(
            tck_list["summary"]["priority_counts"],
            {"High": 1, "Normal": 0, "Low": 0},
        )

        # Record detail exposes activities_total_count and options expose truncation metadata
        acc_detail = owner_client.get(
            f"/api/v1/workspaces/{ws.slug}/records/account/{ids['account']}/"
        ).json()
        self.assertIn("activities_total_count", acc_detail["record"])
        self.assertFalse(acc_detail["record"]["has_more_activities"])

        opts = owner_client.get(f"/api/v1/workspaces/{ws.slug}/crm/options/").json()
        self.assertEqual(opts["accounts_total_count"], 1)
        self.assertFalse(opts["accounts_truncated"])
        self.assertEqual(opts["contacts_total_count"], 1)
        self.assertFalse(opts["contacts_truncated"])

        # Removing read permission on deal hides deal summary and counts from Staff dashboard
        staff_role = Role.objects.get(workspace=ws, is_default_staff=True)
        owner_client.patch(
            f"/api/v1/workspaces/{ws.slug}/roles/{staff_role.id}/",
            data=json.dumps({"object_rights": {"deal": {"read": False}}}),
            content_type="application/json",
        )
        staff_dash = staff_client.get(f"/api/v1/workspaces/{ws.slug}/dashboard/").json()
        self.assertNotIn("deal", staff_dash["readable_object_keys"])
        self.assertIsNone(staff_dash["deal_summary"])
        self.assertFalse(any(r["object_key"] == "deal" for r in staff_dash["recent_records"]))
        self.assertEqual(staff_user.workspace_id, ws.id)
