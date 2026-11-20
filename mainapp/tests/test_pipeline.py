import json
from unittest.mock import patch

from django.core import mail
from django.test import Client, TestCase, override_settings

from ..models import (
    AuditLog,
    Deal,
    Pipeline,
    PipelineStage,
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
class PipelineTests(TestCase):
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

    def test_general_services_default_pipeline_stages_and_tones(self):
        owner_client, workspace, _owner = self._create_verified_workspace()

        pipe_res = owner_client.get(f"/api/v1/workspaces/{workspace.slug}/pipelines/")
        self.assertEqual(pipe_res.status_code, 200)
        pipe_data = pipe_res.json()
        self.assertEqual(pipe_data["pipeline"]["name"], "Sales Pipeline")
        self.assertTrue(pipe_data["permissions"]["can_manage_sales_settings"])
        self.assertEqual(
            [(s["name"], s["position"], s["tone"]) for s in pipe_data["stages"]],
            [
                ("New", 1, "neutral"),
                ("Qualified", 2, "info"),
                ("Proposal", 3, "warn"),
                ("Won", 4, "success"),
                ("Lost", 5, "danger"),
            ],
        )

        board_res = owner_client.get(f"/api/v1/workspaces/{workspace.slug}/deals/board/")
        self.assertEqual(board_res.status_code, 200)
        self.assertEqual(
            [s["name"] for s in board_res.json()["stages"]],
            ["New", "Qualified", "Proposal", "Won", "Lost"],
        )

        opts_res = owner_client.get(f"/api/v1/workspaces/{workspace.slug}/crm/options/")
        self.assertEqual(opts_res.status_code, 200)
        self.assertEqual(
            opts_res.json()["constants"]["deal_stages"],
            ["New", "Qualified", "Proposal", "Won", "Lost"],
        )

    def test_sales_settings_permission_gating(self):
        owner_client, workspace, _owner = self._create_verified_workspace()
        staff_client, staff_user = self._invite_and_accept_staff(owner_client, workspace)
        slug = workspace.slug

        # Regular Staff can read pipeline (200) but cannot mutate pipeline or stages (403)
        read_res = staff_client.get(f"/api/v1/workspaces/{slug}/pipelines/")
        self.assertEqual(read_res.status_code, 200)
        self.assertFalse(read_res.json()["permissions"]["can_manage_sales_settings"])

        stages = read_res.json()["stages"]
        first_id = stages[0]["id"]

        self.assertEqual(
            staff_client.patch(
                f"/api/v1/workspaces/{slug}/pipelines/",
                data=json.dumps({"name": "Blocked Rename"}),
                content_type="application/json",
            ).status_code,
            403,
        )
        self.assertEqual(
            staff_client.post(
                f"/api/v1/workspaces/{slug}/pipelines/stages/",
                data=json.dumps({"name": "Blocked Stage"}),
                content_type="application/json",
            ).status_code,
            403,
        )
        self.assertEqual(
            staff_client.patch(
                f"/api/v1/workspaces/{slug}/pipelines/stages/{first_id}/",
                data=json.dumps({"name": "Blocked Edit"}),
                content_type="application/json",
            ).status_code,
            403,
        )
        self.assertEqual(
            staff_client.put(
                f"/api/v1/workspaces/{slug}/pipelines/stages/reorder/",
                data=json.dumps({"stage_ids": [s["id"] for s in reversed(stages)]}),
                content_type="application/json",
            ).status_code,
            403,
        )
        self.assertEqual(
            staff_client.delete(
                f"/api/v1/workspaces/{slug}/pipelines/stages/{first_id}/"
            ).status_code,
            403,
        )

        # Role with theme_and_layouts CAN manage pipeline settings
        sales_admin_role = Role.objects.create(
            workspace=workspace,
            name="Sales Ops",
            theme_and_layouts=True,
        )
        staff_user.role = sales_admin_role
        staff_user.save(update_fields=["role"])

        add_res = staff_client.post(
            f"/api/v1/workspaces/{slug}/pipelines/stages/",
            data=json.dumps({"name": "Negotiation", "tone": "warn"}),
            content_type="application/json",
        )
        self.assertEqual(add_res.status_code, 201)
        self.assertTrue(add_res.json()["permissions"]["can_manage_sales_settings"])

        # Role with users_and_roles CAN also manage pipeline settings
        users_admin_role = Role.objects.create(
            workspace=workspace,
            name="Workspace Admin",
            users_and_roles=True,
        )
        staff_user.role = users_admin_role
        staff_user.save(update_fields=["role"])
        rename_pipe = staff_client.patch(
            f"/api/v1/workspaces/{slug}/pipelines/",
            data=json.dumps({"name": "Commercial Pipeline"}),
            content_type="application/json",
        )
        self.assertEqual(rename_pipe.status_code, 200)
        self.assertEqual(rename_pipe.json()["pipeline"]["name"], "Commercial Pipeline")

    def test_pipeline_rename_add_edit_reorder_and_per_deal_audit(self):
        owner_client, workspace, owner = self._create_verified_workspace()
        slug = workspace.slug

        deal_res = owner_client.post(
            f"/api/v1/workspaces/{slug}/records/deal/",
            data=json.dumps(
                {
                    "title": "Kumasi Solar Microgrid",
                    "amount": "120000.00",
                    "stage": "Proposal",
                    "win_probability": 60,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(deal_res.status_code, 201)
        deal_id = deal_res.json()["record"]["id"]

        # Duplicate stage name rejected
        dup_res = owner_client.post(
            f"/api/v1/workspaces/{slug}/pipelines/stages/",
            data=json.dumps({"name": "proposal"}),
            content_type="application/json",
        )
        self.assertEqual(dup_res.status_code, 400)

        # Add Negotiation stage
        add_res = owner_client.post(
            f"/api/v1/workspaces/{slug}/pipelines/stages/",
            data=json.dumps({"name": "Negotiation", "tone": "warn"}),
            content_type="application/json",
        )
        self.assertEqual(add_res.status_code, 201)
        stages = add_res.json()["stages"]
        proposal_stage = next(s for s in stages if s["name"] == "Proposal")
        neg_stage = next(s for s in stages if s["name"] == "Negotiation")

        # Rename Proposal -> Technical Proposal and change tone -> info
        edit_res = owner_client.patch(
            f"/api/v1/workspaces/{slug}/pipelines/stages/{proposal_stage['id']}/",
            data=json.dumps({"name": "Technical Proposal", "tone": "info"}),
            content_type="application/json",
        )
        self.assertEqual(edit_res.status_code, 200)
        self.assertEqual(edit_res.json()["updated_deals_count"], 1)

        # Deal reflects renamed stage and tone, and records per-deal AuditLog
        deal_detail = owner_client.get(
            f"/api/v1/workspaces/{slug}/records/deal/{deal_id}/"
        ).json()["record"]
        self.assertEqual(deal_detail["stage"], "Technical Proposal")
        self.assertEqual(deal_detail["stage_tone"], "info")

        self.assertTrue(
            AuditLog.objects.filter(
                workspace=workspace,
                object_type="pipeline_stage",
                object_id=str(proposal_stage["id"]),
                action=AuditLog.ACTION_UPDATE,
                field_name="name",
                previous_value="Proposal",
                new_value="Technical Proposal",
                actor_user=owner,
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                workspace=workspace,
                object_type="deal",
                object_id=str(deal_id),
                action=AuditLog.ACTION_UPDATE,
                field_name="stage",
                previous_value="Proposal",
                new_value="Technical Proposal",
                actor_user=owner,
            ).exists()
        )

        # Reorder stages: put Negotiation right after Technical Proposal (before Won & Lost)
        reordered_names = ["New", "Qualified", "Technical Proposal", "Negotiation", "Won", "Lost"]
        by_name = {s["name"]: s["id"] for s in edit_res.json()["stages"]}
        reorder_ids = [by_name[name] for name in reordered_names]

        # Invalid reorder missing a stage returns 400
        self.assertEqual(
            owner_client.put(
                f"/api/v1/workspaces/{slug}/pipelines/stages/reorder/",
                data=json.dumps({"stage_ids": reorder_ids[:-1]}),
                content_type="application/json",
            ).status_code,
            400,
        )

        reorder_res = owner_client.put(
            f"/api/v1/workspaces/{slug}/pipelines/stages/reorder/",
            data=json.dumps({"stage_ids": reorder_ids}),
            content_type="application/json",
        )
        self.assertEqual(reorder_res.status_code, 200)
        self.assertEqual(
            [s["name"] for s in reorder_res.json()["stages"]],
            reordered_names,
        )

        # Board, options, and dashboard all reflect the new order
        board_data = owner_client.get(f"/api/v1/workspaces/{slug}/deals/board/").json()
        self.assertEqual([s["name"] for s in board_data["stages"]], reordered_names)
        opts_data = owner_client.get(f"/api/v1/workspaces/{slug}/crm/options/").json()
        self.assertEqual(opts_data["constants"]["deal_stages"], reordered_names)
        dash_data = owner_client.get(f"/api/v1/workspaces/{slug}/dashboard/").json()
        self.assertEqual(
            [s["name"] for s in dash_data["deal_summary"]["stages"]],
            reordered_names,
        )
        self.assertTrue(
            AuditLog.objects.filter(
                workspace=workspace,
                object_type="pipeline",
                field_name="stage_order",
                actor_user=owner,
            ).exists()
        )
        self.assertEqual(neg_stage["tone"], "warn")

    def test_stage_removal_moves_deals_and_guards_last_stage(self):
        owner_client, workspace, owner = self._create_verified_workspace()
        slug = workspace.slug

        for idx in range(2):
            res = owner_client.post(
                f"/api/v1/workspaces/{slug}/records/deal/",
                data=json.dumps(
                    {
                        "title": f"Proposal Deal {idx + 1}",
                        "amount": "25000.00",
                        "stage": "Proposal",
                    }
                ),
                content_type="application/json",
            )
            self.assertEqual(res.status_code, 201)

        stages = owner_client.get(f"/api/v1/workspaces/{slug}/pipelines/").json()["stages"]
        by_name = {s["name"]: s for s in stages}
        proposal = by_name["Proposal"]
        qualified = by_name["Qualified"]
        self.assertEqual(proposal["deal_count"], 2)

        # Deleting stage with deals without destination fails 400
        no_target = owner_client.delete(
            f"/api/v1/workspaces/{slug}/pipelines/stages/{proposal['id']}/"
        )
        self.assertEqual(no_target.status_code, 400)

        # Deleting stage with self as destination fails 400
        self_target = owner_client.delete(
            f"/api/v1/workspaces/{slug}/pipelines/stages/{proposal['id']}/",
            data=json.dumps({"move_to_stage_id": proposal["id"]}),
            content_type="application/json",
        )
        self.assertEqual(self_target.status_code, 400)

        # Deleting Proposal moving deals to Qualified succeeds
        remove_res = owner_client.delete(
            f"/api/v1/workspaces/{slug}/pipelines/stages/{proposal['id']}/",
            data=json.dumps({"move_to_stage_id": qualified["id"]}),
            content_type="application/json",
        )
        self.assertEqual(remove_res.status_code, 200)
        self.assertEqual(remove_res.json()["moved_deals_count"], 2)

        updated_stages = remove_res.json()["stages"]
        self.assertEqual(
            [s["name"] for s in updated_stages],
            ["New", "Qualified", "Won", "Lost"],
        )
        self.assertEqual(
            next(s for s in updated_stages if s["id"] == qualified["id"])["deal_count"],
            2,
        )
        self.assertEqual(
            Deal.objects.filter(workspace=workspace, stage="Qualified").count(),
            2,
        )
        self.assertEqual(
            AuditLog.objects.filter(
                workspace=workspace,
                object_type="deal",
                action=AuditLog.ACTION_UPDATE,
                field_name="stage",
                previous_value="Proposal",
                new_value="Qualified",
                actor_user=owner,
            ).count(),
            2,
        )
        self.assertTrue(
            AuditLog.objects.filter(
                workspace=workspace,
                object_type="pipeline_stage",
                object_id=str(proposal["id"]),
                action=AuditLog.ACTION_DELETE,
                previous_value="Proposal",
                new_value="Qualified",
            ).exists()
        )

        # Empty stage removes cleanly until only 1 stage remains
        for stage in updated_stages:
            if stage["name"] in ("New", "Won", "Lost"):
                r = owner_client.delete(
                    f"/api/v1/workspaces/{slug}/pipelines/stages/{stage['id']}/"
                )
                self.assertEqual(r.status_code, 200)

        # Last remaining stage (Qualified) cannot be removed
        last_res = owner_client.delete(
            f"/api/v1/workspaces/{slug}/pipelines/stages/{qualified['id']}/"
        )
        self.assertEqual(last_res.status_code, 400)

    def test_deal_stage_move_preserves_win_prob_and_board_per_stage_isolation(self):
        owner_client, workspace, owner = self._create_verified_workspace()
        staff_client, _staff = self._invite_and_accept_staff(owner_client, workspace)
        slug = workspace.slug

        # Create 4 deals in Won and 2 deals in New
        for idx in range(4):
            res = owner_client.post(
                f"/api/v1/workspaces/{slug}/records/deal/",
                data=json.dumps(
                    {
                        "title": f"Won Deal {idx + 1}",
                        "amount": "10000.00",
                        "stage": "Won",
                    }
                ),
                content_type="application/json",
            )
            self.assertEqual(res.status_code, 201)

        new_deal_ids = []
        for idx in range(2):
            res = owner_client.post(
                f"/api/v1/workspaces/{slug}/records/deal/",
                data=json.dumps(
                    {
                        "title": f"New Deal {idx + 1}",
                        "amount": "5000.00",
                        "stage": "New",
                        "win_probability": 35,
                    }
                ),
                content_type="application/json",
            )
            self.assertEqual(res.status_code, 201)
            new_deal_ids.append(res.json()["record"]["id"])

        # Moving a deal to Won updates only stage, leaving win_probability unchanged (35)
        move_res = owner_client.patch(
            f"/api/v1/workspaces/{slug}/records/deal/{new_deal_ids[0]}/",
            data=json.dumps({"stage": "Won"}),
            content_type="application/json",
        )
        self.assertEqual(move_res.status_code, 200)
        moved = move_res.json()["record"]
        self.assertEqual(moved["stage"], "Won")
        self.assertEqual(moved["win_probability"], 35)

        self.assertTrue(
            AuditLog.objects.filter(
                workspace=workspace,
                object_type="deal",
                object_id=str(new_deal_ids[0]),
                action=AuditLog.ACTION_UPDATE,
                field_name="stage",
                previous_value="New",
                new_value="Won",
                actor_kind=AuditLog.ACTOR_KIND_PERSON,
                actor_user=owner,
            ).exists()
        )

        # Invalid stage is refused
        bad_stage = owner_client.patch(
            f"/api/v1/workspaces/{slug}/records/deal/{new_deal_ids[1]}/",
            data=json.dumps({"stage": "NonExistent"}),
            content_type="application/json",
        )
        self.assertEqual(bad_stage.status_code, 400)

        # Per-stage limit=2 shows Won capped at 2 (deal_count=5, has_more=True) without starving New (1 deal)
        board_res = owner_client.get(
            f"/api/v1/workspaces/{slug}/deals/board/?per_stage_limit=2"
        )
        self.assertEqual(board_res.status_code, 200)
        cols_by_name = {col["name"]: col for col in board_res.json()["stages"]}
        self.assertEqual(cols_by_name["Won"]["deal_count"], 5)
        self.assertEqual(len(cols_by_name["Won"]["deals"]), 2)
        self.assertTrue(cols_by_name["Won"]["has_more"])
        self.assertEqual(cols_by_name["New"]["deal_count"], 1)
        self.assertEqual(len(cols_by_name["New"]["deals"]), 1)
        self.assertFalse(cols_by_name["New"]["has_more"])

        # Search query filters board columns
        search_res = owner_client.get(
            f"/api/v1/workspaces/{slug}/deals/board/?q=New%20Deal%202"
        )
        self.assertEqual(search_res.status_code, 200)
        self.assertEqual(search_res.json()["total_count"], 1)

        # Staff lacking deal.update gets 403 when moving a deal stage
        staff_role = Role.objects.get(workspace=workspace, is_default_staff=True)
        owner_client.patch(
            f"/api/v1/workspaces/{slug}/roles/{staff_role.id}/",
            data=json.dumps({"object_rights": {"deal": {"update": False}}}),
            content_type="application/json",
        )
        self.assertEqual(
            staff_client.patch(
                f"/api/v1/workspaces/{slug}/records/deal/{new_deal_ids[1]}/",
                data=json.dumps({"stage": "Qualified"}),
                content_type="application/json",
            ).status_code,
            403,
        )

    def test_lead_conversion_uses_dynamic_pipeline_stages(self):
        owner_client, workspace, _owner = self._create_verified_workspace()
        slug = workspace.slug

        lead_res = owner_client.post(
            f"/api/v1/workspaces/{slug}/records/lead/",
            data=json.dumps(
                {
                    "name": "Abena Boateng",
                    "company": "Volta Solar Ltd",
                    "email": "abena@voltasolar.example",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(lead_res.status_code, 201)
        lead_id = lead_res.json()["record"]["id"]

        stages = owner_client.get(f"/api/v1/workspaces/{slug}/pipelines/").json()["stages"]
        qualified_id = next(s["id"] for s in stages if s["name"] == "Qualified")

        # Rename Qualified -> Discovery
        self.assertEqual(
            owner_client.patch(
                f"/api/v1/workspaces/{slug}/pipelines/stages/{qualified_id}/",
                data=json.dumps({"name": "Discovery"}),
                content_type="application/json",
            ).status_code,
            200,
        )

        preview = owner_client.get(
            f"/api/v1/workspaces/{slug}/records/lead/{lead_id}/conversion-preview/"
        ).json()
        # Renaming Qualified removes the exact 'Qualified' stage, so default falls back to the first active stage ("New")
        self.assertEqual(preview["defaults"]["deal_stage"], "New")
        self.assertIn("Discovery", [s["name"] for s in preview["deal_stages"]])

        # Converting without explicit deal_stage uses the first active stage ("New")
        convert_res = owner_client.post(
            f"/api/v1/workspaces/{slug}/records/lead/{lead_id}/convert/",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(convert_res.status_code, 200)
        self.assertEqual(convert_res.json()["deal"]["stage"], "New")

        # Removing the renamed stage still falls back cleanly to the first active stage
        self.assertEqual(
            owner_client.delete(
                f"/api/v1/workspaces/{slug}/pipelines/stages/{qualified_id}/",
                data=json.dumps({}),
                content_type="application/json",
            ).status_code,
            200,
        )
        lead2_id = owner_client.post(
            f"/api/v1/workspaces/{slug}/records/lead/",
            data=json.dumps({"name": "Yaw Boateng"}),
            content_type="application/json",
        ).json()["record"]["id"]
        preview2 = owner_client.get(
            f"/api/v1/workspaces/{slug}/records/lead/{lead2_id}/conversion-preview/"
        ).json()
        self.assertEqual(preview2["defaults"]["deal_stage"], "New")

    def test_healthcare_has_no_pipeline_or_kanban_and_enabling_deal_seeds_pipeline(self):
        hc_client, hc_ws, _hc_owner = self._create_verified_workspace(
            workspace_name="Korle Care Clinic",
            business_type="healthcare_wellness",
            email="director@korlecare.example",
        )
        slug = hc_ws.slug

        self.assertFalse(Pipeline.objects.filter(workspace=hc_ws).exists())
        self.assertEqual(
            hc_client.get(f"/api/v1/workspaces/{slug}/pipelines/").status_code,
            404,
        )
        self.assertEqual(
            hc_client.post(
                f"/api/v1/workspaces/{slug}/pipelines/stages/",
                data=json.dumps({"name": "Custom"}),
                content_type="application/json",
            ).status_code,
            404,
        )
        self.assertEqual(
            hc_client.get(f"/api/v1/workspaces/{slug}/deals/board/").status_code,
            404,
        )
        self.assertEqual(
            hc_client.get(f"/api/v1/workspaces/{slug}/records/deal/").status_code,
            404,
        )

        opts = hc_client.get(f"/api/v1/workspaces/{slug}/crm/options/").json()
        self.assertEqual(opts["constants"]["deal_stages"], [])
        dash = hc_client.get(f"/api/v1/workspaces/{slug}/dashboard/").json()
        self.assertIsNone(dash["deal_summary"])

        # Enabling Deal table provisions the 5 default stages
        enable_res = hc_client.patch(
            f"/api/v1/workspaces/{slug}/tables/deal/",
            data=json.dumps({"is_enabled": True}),
            content_type="application/json",
        )
        self.assertEqual(enable_res.status_code, 200)
        pipe_res = hc_client.get(f"/api/v1/workspaces/{slug}/pipelines/")
        self.assertEqual(pipe_res.status_code, 200)
        self.assertEqual(
            [s["name"] for s in pipe_res.json()["stages"]],
            ["New", "Qualified", "Proposal", "Won", "Lost"],
        )

    def test_cross_workspace_pipeline_isolation(self):
        client_a, ws_a, _owner_a = self._create_verified_workspace(
            workspace_name="Alpha Services",
            email="alpha@services.example",
        )
        client_b, ws_b, _owner_b = self._create_verified_workspace(
            workspace_name="Beta Services",
            email="beta@services.example",
        )

        stages_a = client_a.get(f"/api/v1/workspaces/{ws_a.slug}/pipelines/").json()["stages"]
        stages_b = client_b.get(f"/api/v1/workspaces/{ws_b.slug}/pipelines/").json()["stages"]

        self.assertEqual(
            client_b.get(f"/api/v1/workspaces/{ws_a.slug}/pipelines/").status_code,
            404,
        )
        self.assertEqual(
            client_b.get(f"/api/v1/workspaces/{ws_a.slug}/deals/board/").status_code,
            404,
        )
        self.assertEqual(
            client_b.patch(
                f"/api/v1/workspaces/{ws_b.slug}/pipelines/stages/{stages_a[0]['id']}/",
                data=json.dumps({"name": "Hijack"}),
                content_type="application/json",
            ).status_code,
            404,
        )
        self.assertEqual(
            client_b.put(
                f"/api/v1/workspaces/{ws_b.slug}/pipelines/stages/reorder/",
                data=json.dumps({"stage_ids": [s["id"] for s in stages_a]}),
                content_type="application/json",
            ).status_code,
            400,
        )

        # Moving Workspace B stage deals into Workspace A's stage ID is refused
        client_b.post(
            f"/api/v1/workspaces/{ws_b.slug}/records/deal/",
            data=json.dumps({"title": "Beta Deal", "stage": "Proposal"}),
            content_type="application/json",
        )
        prop_b = next(s for s in stages_b if s["name"] == "Proposal")
        self.assertEqual(
            client_b.delete(
                f"/api/v1/workspaces/{ws_b.slug}/pipelines/stages/{prop_b['id']}/",
                data=json.dumps({"move_to_stage_id": stages_a[0]["id"]}),
                content_type="application/json",
            ).status_code,
            400,
        )
        self.assertTrue(PipelineStage.objects.filter(id=prop_b["id"]).exists())

    def test_expanded_tones_legacy_sync_pipeline_name_and_audit_scoping(self):
        owner_client, workspace, owner = self._create_verified_workspace(
            workspace_name="Meridian Trade",
            email="owner@meridiantrade.example",
        )
        staff_client, _staff = self._invite_and_accept_staff(owner_client, workspace)
        slug = workspace.slug

        # Rename pipeline and verify list summary + crm/options both expose pipeline.name
        rename_pipe = owner_client.patch(
            f"/api/v1/workspaces/{slug}/pipelines/",
            data=json.dumps({"name": "Enterprise Sales"}),
            content_type="application/json",
        )
        self.assertEqual(rename_pipe.status_code, 200)

        list_summary = owner_client.get(f"/api/v1/workspaces/{slug}/records/deal/").json()["summary"]
        self.assertEqual(list_summary["pipeline"]["name"], "Enterprise Sales")
        opts_constants = owner_client.get(f"/api/v1/workspaces/{slug}/crm/options/").json()["constants"]
        self.assertEqual(opts_constants["pipeline"]["name"], "Enterprise Sales")

        # Add a stage using expanded tone tokens
        for tone in ("indigo", "violet", "teal", "orange", "pink"):
            add_res = owner_client.post(
                f"/api/v1/workspaces/{slug}/pipelines/stages/",
                data=json.dumps({"name": f"Stage {tone}", "tone": tone}),
                content_type="application/json",
            )
            self.assertEqual(add_res.status_code, 201)
            self.assertEqual(add_res.json()["stage"]["tone"], tone)

        # Legacy unlinked Deal with pipeline_stage=None still updates on stage rename
        legacy_deal = Deal.objects.create(
            workspace=workspace,
            owner=owner,
            created_by=owner,
            title="Legacy Unlinked Deal",
            stage="Proposal",
            pipeline=None,
            pipeline_stage=None,
        )
        proposal_stage = PipelineStage.objects.get(workspace=workspace, name="Proposal")
        rename_stage_res = owner_client.patch(
            f"/api/v1/workspaces/{slug}/pipelines/stages/{proposal_stage.id}/",
            data=json.dumps({"name": "Pricing Review"}),
            content_type="application/json",
        )
        self.assertEqual(rename_stage_res.status_code, 200)
        legacy_deal.refresh_from_db()
        self.assertEqual(legacy_deal.stage, "Pricing Review")
        self.assertEqual(legacy_deal.pipeline_stage_id, proposal_stage.id)

        # Unlinked Deal patched with unchanged stage name repairs pipeline_stage FK without spurious audit
        unlinked_won = Deal.objects.create(
            workspace=workspace,
            owner=owner,
            created_by=owner,
            title="Legacy Won Deal",
            stage="Won",
            pipeline=None,
            pipeline_stage=None,
        )
        won_stage = PipelineStage.objects.get(workspace=workspace, name="Won")
        patch_res = owner_client.patch(
            f"/api/v1/workspaces/{slug}/records/deal/{unlinked_won.id}/",
            data=json.dumps({"stage": "Won"}),
            content_type="application/json",
        )
        self.assertEqual(patch_res.status_code, 200)
        self.assertFalse(patch_res.json()["changed"])
        unlinked_won.refresh_from_db()
        self.assertEqual(unlinked_won.pipeline_stage_id, won_stage.id)

        # Audit log scoping: revoking deal.read on Staff hides deal/pipeline/pipeline_stage audit entries
        staff_role = Role.objects.get(workspace=workspace, is_default_staff=True)
        owner_client.post(
            f"/api/v1/workspaces/{slug}/records/account/",
            data=json.dumps({"name": "Visible Audit Account"}),
            content_type="application/json",
        )
        self.assertEqual(
            owner_client.patch(
                f"/api/v1/workspaces/{slug}/roles/{staff_role.id}/",
                data=json.dumps({"object_rights": {"deal": {"read": False}}}),
                content_type="application/json",
            ).status_code,
            200,
        )
        staff_audit = staff_client.get(f"/api/v1/workspaces/{slug}/audit/?page_size=50").json()["results"]
        audit_types = {entry["object_type"] for entry in staff_audit}
        self.assertIn("account", audit_types)
        self.assertNotIn("deal", audit_types)
        self.assertNotIn("pipeline", audit_types)
        self.assertNotIn("pipeline_stage", audit_types)
