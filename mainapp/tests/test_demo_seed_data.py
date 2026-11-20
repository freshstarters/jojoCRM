import io
import json
import os
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings

from mainapp.catalog import BUSINESS_TYPES, BUSINESS_TYPES_BY_KEY
from mainapp.models import (
    Account,
    Activity,
    Appointment,
    AuditLog,
    Contact,
    Deal,
    Job,
    Lead,
    PipelineStage,
    Role,
    Ticket,
    User,
    Visit,
    Workspace,
    WorkspaceRecordBase,
    WorkspaceTable,
)
from mainapp.service.catalog_service import initialize_workspace_catalog
from mainapp.service.demo_seed_service import (
    DEFAULT_DEV_DEMO_PASSWORD,
    DEMO_EMAIL_DOMAIN,
    DEMO_PASSWORD_ENV_VAR,
    DEMO_PROFILES_BY_KEY,
    register_demo_seed_extension,
    unregister_demo_seed_extension,
)
from mainapp.service.role_service import (
    FIELD_RIGHT_HIDDEN,
    FIELD_RIGHT_READ_ONLY,
    ensure_workspace_default_roles,
)
from mainapp.tests.helpers import CLEAR_GH_PAYLOAD, make_mock_urlopen


@override_settings(
    DEBUG=True,
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
)
class DemoSeedCommandTests(TestCase):
    def _run_seed(self, *args, **kwargs):
        out = io.StringIO()
        err = io.StringIO()
        call_command('seed_demo_data', *args, stdout=out, stderr=err, **kwargs)
        return out.getvalue(), err.getvalue()

    @override_settings(DEBUG=False)
    def test_refused_when_debug_false_without_force(self):
        with self.assertRaises(CommandError) as ctx:
            self._run_seed()

        self.assertIn('DEBUG=False', str(ctx.exception))
        self.assertEqual(Workspace.objects.count(), 0)
        self.assertEqual(User.objects.count(), 0)

    @override_settings(DEBUG=False)
    def test_debug_false_with_force_requires_explicit_password(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(DEMO_PASSWORD_ENV_VAR, None)
            with self.assertRaises(CommandError) as ctx:
                self._run_seed('--force', '--only', 'general_services')

        self.assertIn('password is required', str(ctx.exception).lower())
        self.assertEqual(Workspace.objects.count(), 0)

    @override_settings(DEBUG=False)
    def test_debug_false_with_force_and_password_succeeds(self):
        output, _ = self._run_seed(
            '--force',
            '--only',
            'general_services',
            '--password',
            'StagingQaPass99!',
        )
        self.assertIn('demo-general-services', output)
        owner = User.objects.get(email=f'owner.general{DEMO_EMAIL_DOMAIN}')
        self.assertTrue(owner.check_password('StagingQaPass99!'))
        self.assertFalse(owner.check_password(DEFAULT_DEV_DEMO_PASSWORD))

    def test_seed_all_20_business_types_and_idempotency(self):
        output1, _ = self._run_seed()
        self.assertIn('Seeded 20 demo workspace(s):', output1)
        self.assertEqual(Workspace.objects.count(), len(BUSINESS_TYPES))

        # Verify every business type has a workspace, verified Owner & Staff, enabled tables only,
        # and Deals across all 5 starter stages whenever Deal is enabled.
        expected_stages = {name for name, _ in PipelineStage.STARTER_STAGE_SPECS}
        for bt in BUSINESS_TYPES:
            profile = DEMO_PROFILES_BY_KEY[bt.key]
            ws = Workspace.objects.get(slug=profile.slug)
            self.assertEqual(ws.business_type, bt.key)
            self.assertEqual(ws.contact_label, bt.default_contact_label)

            owner = User.objects.get(email=f'owner.{profile.short_code}{DEMO_EMAIL_DOMAIN}')
            staff = User.objects.get(email=f'staff.{profile.short_code}{DEMO_EMAIL_DOMAIN}')
            self.assertEqual(owner.workspace_id, ws.id)
            self.assertEqual(staff.workspace_id, ws.id)
            self.assertTrue(owner.email_verified)
            self.assertTrue(staff.email_verified)
            self.assertTrue(owner.is_active)
            self.assertTrue(staff.is_active)
            self.assertTrue(owner.role.is_owner)
            self.assertTrue(staff.role.is_default_staff)

            enabled_keys = set(
                WorkspaceTable.objects.filter(workspace=ws, is_enabled=True).values_list(
                    'object_key', flat=True
                )
            )
            self.assertGreaterEqual(Account.objects.filter(workspace=ws).count(), 3)
            self.assertGreaterEqual(Contact.objects.filter(workspace=ws).count(), 4)
            self.assertGreaterEqual(Activity.objects.filter(workspace=ws).count(), 2)
            self.assertGreaterEqual(AuditLog.objects.filter(workspace=ws).count(), 1)

            if 'deal' in enabled_keys:
                deal_stages = set(
                    Deal.objects.filter(workspace=ws).values_list('stage', flat=True)
                )
                self.assertEqual(deal_stages, expected_stages)
            else:
                self.assertEqual(Deal.objects.filter(workspace=ws).count(), 0)

            if 'visit' in enabled_keys:
                self.assertGreaterEqual(Visit.objects.filter(workspace=ws).count(), 1)
            else:
                self.assertEqual(Visit.objects.filter(workspace=ws).count(), 0)

            if 'appointment' in enabled_keys:
                self.assertGreaterEqual(Appointment.objects.filter(workspace=ws).count(), 1)
            else:
                self.assertEqual(Appointment.objects.filter(workspace=ws).count(), 0)

            if 'ticket' in enabled_keys:
                self.assertGreaterEqual(Ticket.objects.filter(workspace=ws).count(), 1)
            else:
                self.assertEqual(Ticket.objects.filter(workspace=ws).count(), 0)

            if 'lead' in enabled_keys:
                self.assertTrue(
                    Lead.objects.filter(workspace=ws, status=Lead.STATUS_OPEN).exists()
                )
                if 'deal' in enabled_keys:
                    self.assertTrue(
                        Lead.objects.filter(workspace=ws, status=Lead.STATUS_CONVERTED).exists()
                    )
                else:
                    self.assertFalse(
                        Lead.objects.filter(workspace=ws, status=Lead.STATUS_CONVERTED).exists()
                    )
            else:
                self.assertEqual(Lead.objects.filter(workspace=ws).count(), 0)

            for job in Job.objects.filter(workspace=ws):
                self.assertTrue(
                    job.account_id is not None or job.contact_id is not None,
                    f"Seeded job {job.title} in {ws.slug} lacks Account and Contact link.",
                )

            if 'visit' in enabled_keys:
                for visit in Visit.objects.filter(workspace=ws):
                    blob = f"{visit.reason} {visit.notes} {visit.follow_up}".lower()
                    self.assertNotIn('hba1c', blob)
                    self.assertNotIn('mmhg', blob)
                    self.assertNotIn('medication titration', blob)

        # Snapshot counts before second run
        snapshot_before = self._db_counts_snapshot()

        # Mutate a seeded deal stage and verify plain re-run preserves QA edits and adds zero duplicates
        gen_ws = Workspace.objects.get(slug='demo-general-services')
        edited_deal = Deal.objects.filter(workspace=gen_ws).order_by('id').first()
        original_stage = edited_deal.stage
        edited_deal.notes = 'Manual QA note preserved on plain re-run'
        edited_deal.save(update_fields=['notes'])

        self._run_seed()

        snapshot_after = self._db_counts_snapshot()
        self.assertEqual(snapshot_before, snapshot_after)

        edited_deal.refresh_from_db()
        self.assertEqual(edited_deal.notes, 'Manual QA note preserved on plain re-run')
        self.assertEqual(edited_deal.stage, original_stage)

    def test_headline_qa_edge_cases_and_healthcare_patients(self):
        self._run_seed('--only', 'general_services,healthcare_wellness')
        self.assertEqual(Workspace.objects.count(), 2)

        # General services: cloned custom role with hidden margin + visibility diversity + duplicate-match lead
        gen_ws = Workspace.objects.get(slug='demo-general-services')
        coord_role = Role.objects.get(workspace=gen_ws, name='Sales Coordinator')
        self.assertEqual(coord_role.field_rights['deal']['estimated_margin'], FIELD_RIGHT_HIDDEN)
        self.assertEqual(coord_role.field_rights['deal']['win_probability'], FIELD_RIGHT_READ_ONLY)
        coord_user = User.objects.get(email=f'coord.general{DEMO_EMAIL_DOMAIN}')
        self.assertEqual(coord_user.role_id, coord_role.id)

        visibilities = set(Deal.objects.filter(workspace=gen_ws).values_list('visibility', flat=True))
        self.assertIn(WorkspaceRecordBase.VISIBILITY_WORKSPACE, visibilities)
        self.assertIn(WorkspaceRecordBase.VISIBILITY_ROLE, visibilities)
        self.assertIn(WorkspaceRecordBase.VISIBILITY_OWNER, visibilities)

        account_names = set(Account.objects.filter(workspace=gen_ws).values_list('name', flat=True))
        open_lead_companies = set(
            Lead.objects.filter(workspace=gen_ws, status=Lead.STATUS_OPEN).values_list(
                'company', flat=True
            )
        )
        self.assertTrue(account_names & open_lead_companies)

        # Healthcare: Contact labeled Patient, Visits with reason/notes/follow_up, no Deals
        hc_ws = Workspace.objects.get(slug='demo-healthcare-wellness')
        self.assertEqual(hc_ws.contact_label, 'Patient')
        self.assertEqual(Deal.objects.filter(workspace=hc_ws).count(), 0)
        visits = list(Visit.objects.filter(workspace=hc_ws))
        self.assertGreaterEqual(len(visits), 3)
        self.assertTrue(all(v.contact_id and v.reason and v.notes and v.follow_up for v in visits))
        hc_role = Role.objects.get(workspace=hc_ws, name='Front Desk Coordinator')
        self.assertEqual(hc_role.field_rights['visit']['notes'], FIELD_RIGHT_HIDDEN)

    def test_only_flag_accepts_repeatable_and_rejects_unknown(self):
        self._run_seed('--only', 'retail_trading', '--only', 'financial_services')
        slugs = set(Workspace.objects.values_list('slug', flat=True))
        self.assertEqual(slugs, {'demo-retail-trading', 'demo-financial-services'})

        with self.assertRaises(CommandError) as ctx:
            self._run_seed('--only', 'not_a_real_pack')
        self.assertIn('Unknown pack key', str(ctx.exception))

    def test_reset_refreshes_demo_without_touching_non_seeded_workspaces(self):
        # Create a real non-seeded customer workspace
        real_ws = Workspace.objects.create(
            name='Accra Real Customer Ltd',
            slug='accra-real-customer',
            business_type='general_services',
            business_type_label='General services',
            contact_label='Contact',
        )
        owner_role, _ = ensure_workspace_default_roles(real_ws)
        real_owner = User.objects.create_user(
            email='ceo@realcustomer.gh',
            workspace=real_ws,
            password='RealPassword123!',
            role=owner_role,
            email_verified=True,
        )
        real_ws.created_by = real_owner
        real_ws.save(update_fields=['created_by'])
        initialize_workspace_catalog(real_ws, 'general_services', actor_user=real_owner)
        real_account = Account.objects.create(
            workspace=real_ws,
            name='Real Production Client',
            owner=real_owner,
            created_by=real_owner,
        )

        # Seed demo workspace, mutate it, then reset
        self._run_seed('--only', 'general_services')
        demo_ws = Workspace.objects.get(slug='demo-general-services')
        demo_account = Account.objects.filter(workspace=demo_ws).first()
        demo_account.notes = 'Temporary QA scratch edit'
        demo_account.save(update_fields=['notes'])
        Account.objects.create(
            workspace=demo_ws,
            name='Extra Adhoc QA Account',
            created_by=demo_ws.created_by,
        )

        self._run_seed('--only', 'general_services', '--reset')

        # Real workspace and its records are completely untouched
        self.assertTrue(Workspace.objects.filter(pk=real_ws.pk).exists())
        self.assertTrue(User.objects.filter(pk=real_owner.pk).exists())
        self.assertTrue(Account.objects.filter(pk=real_account.pk).exists())

        # Demo workspace was recreated cleanly without the adhoc account or scratch edit
        refreshed_demo_ws = Workspace.objects.get(slug='demo-general-services')
        self.assertNotEqual(refreshed_demo_ws.id, demo_ws.id)
        self.assertFalse(
            Account.objects.filter(workspace=refreshed_demo_ws, name='Extra Adhoc QA Account').exists()
        )
        refreshed_account = Account.objects.get(
            workspace=refreshed_demo_ws,
            name=demo_account.name,
        )
        self.assertNotEqual(refreshed_account.notes, 'Temporary QA scratch edit')

    def test_reset_refuses_to_delete_colliding_non_demo_workspace(self):
        # A real user manually registered a workspace with a demo-* slug
        colliding_ws = Workspace.objects.create(
            name='Real Company Using Demo Slug',
            slug='demo-general-services',
            business_type='general_services',
            business_type_label='General services',
            contact_label='Contact',
        )
        owner_role, _ = ensure_workspace_default_roles(colliding_ws)
        real_owner = User.objects.create_user(
            email='founder@realcompany.gh',
            workspace=colliding_ws,
            password='RealPassword123!',
            role=owner_role,
            email_verified=True,
        )
        colliding_ws.created_by = real_owner
        colliding_ws.save(update_fields=['created_by'])

        with self.assertRaises(CommandError) as ctx:
            self._run_seed('--only', 'general_services', '--reset')

        self.assertIn('Refusing to modify or reset', str(ctx.exception))
        self.assertTrue(Workspace.objects.filter(pk=colliding_ws.pk).exists())
        self.assertTrue(User.objects.filter(pk=real_owner.pk).exists())

    @override_settings(
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        DEFAULT_FROM_EMAIL='noreply@jojocrm.example',
        EMAIL_HOST_USER='noreply@jojocrm.example',
        IPINFO_TOKEN='test-token',
        GEO_LOOKUP_IP='',
        TRUSTED_PROXY_IPS=[],
        BYPASS_GEO_CHECK=False,
    )
    def test_self_serve_signup_still_creates_empty_workspace(self):
        self._run_seed('--only', 'general_services')
        client = Client()
        with patch(
            'mainapp.integrations.geolocation.urllib.request.urlopen',
            return_value=make_mock_urlopen(json.dumps(CLEAR_GH_PAYLOAD).encode('utf-8')),
        ):
            response = client.post(
                '/api/v1/signup/',
                data=json.dumps(
                    {
                        'workspace_name': 'Fresh Ghana Startup',
                        'slug': 'fresh-ghana-startup',
                        'business_type': 'general_services',
                        'email': 'owner@freshghana.gh',
                        'password': 'StrongPassword123!',
                    }
                ),
                content_type='application/json',
                REMOTE_ADDR='154.160.1.1',
            )
        self.assertEqual(response.status_code, 201)
        fresh_ws = Workspace.objects.get(slug='fresh-ghana-startup')
        self.assertEqual(Account.objects.filter(workspace=fresh_ws).count(), 0)
        self.assertEqual(Contact.objects.filter(workspace=fresh_ws).count(), 0)
        self.assertEqual(Lead.objects.filter(workspace=fresh_ws).count(), 0)
        self.assertEqual(Deal.objects.filter(workspace=fresh_ws).count(), 0)
        self.assertEqual(Job.objects.filter(workspace=fresh_ws).count(), 0)
        self.assertEqual(Appointment.objects.filter(workspace=fresh_ws).count(), 0)
        self.assertEqual(Ticket.objects.filter(workspace=fresh_ws).count(), 0)
        self.assertEqual(Activity.objects.filter(workspace=fresh_ws).count(), 0)

    def test_seeded_accounts_can_sign_in_and_load_populated_dashboard_and_options(self):
        self._run_seed('--only', 'general_services')
        client = Client()

        login_resp = client.post(
            '/api/v1/login/',
            data=json.dumps(
                {
                    'email': f'owner.general{DEMO_EMAIL_DOMAIN}',
                    'password': DEFAULT_DEV_DEMO_PASSWORD,
                }
            ),
            content_type='application/json',
        )
        self.assertEqual(login_resp.status_code, 200)

        dash_resp = client.get('/api/v1/workspaces/demo-general-services/dashboard/')
        self.assertEqual(dash_resp.status_code, 200)
        dash_data = dash_resp.json()
        self.assertGreater(dash_data['total_records'], 10)
        self.assertEqual(len(dash_data['deal_summary']['stage_counts']), 5)
        self.assertTrue(all(v >= 1 for v in dash_data['deal_summary']['stage_counts'].values()))
        self.assertIn('task_summary', dash_data)
        self.assertIn('active_deals', dash_data)
        self.assertIn('ghana_hubs', dash_data)
        self.assertIn('velocity_series', dash_data)
        self.assertGreaterEqual(dash_data['ghana_hubs']['total_accounts_with_city'], 3)
        self.assertEqual(
            AuditLog.objects.filter(
                workspace__slug='demo-general-services',
                object_type='account',
                action=AuditLog.ACTION_CREATE,
                field_name='record',
            ).count(),
            1,
        )
        conv_lead = Lead.objects.get(
            workspace__slug='demo-general-services',
            status=Lead.STATUS_CONVERTED,
        )
        self.assertIsNotNone(conv_lead.converted_account)
        self.assertEqual(conv_lead.converted_account.name, conv_lead.company)
        self.assertIsNotNone(conv_lead.converted_contact)
        self.assertEqual(conv_lead.converted_contact.name, conv_lead.name)

        opts_resp = client.get('/api/v1/workspaces/demo-general-services/crm/options/')
        self.assertEqual(opts_resp.status_code, 200)
        opts_data = opts_resp.json()
        self.assertGreaterEqual(len(opts_data['accounts']), 3)
        self.assertGreaterEqual(len(opts_data['contacts']), 4)

    def test_extension_hook_executes_for_future_sprint_seeders(self):
        called_packs: list[str] = []

        def sample_extension(ctx):
            called_packs.append(ctx.business_type.key)
            return {'invoices': 2}

        register_demo_seed_extension('test_s3_invoices', sample_extension)
        try:
            self._run_seed('--only', 'retail_trading,healthcare_wellness')
        finally:
            unregister_demo_seed_extension('test_s3_invoices')

        self.assertEqual(called_packs, ['retail_trading', 'healthcare_wellness'])

    def _db_counts_snapshot(self) -> dict[str, int]:
        return {
            'workspaces': Workspace.objects.count(),
            'users': User.objects.count(),
            'roles': Role.objects.count(),
            'accounts': Account.objects.count(),
            'contacts': Contact.objects.count(),
            'leads': Lead.objects.count(),
            'deals': Deal.objects.count(),
            'jobs': Job.objects.count(),
            'appointments': Appointment.objects.count(),
            'visits': Visit.objects.count(),
            'tickets': Ticket.objects.count(),
            'activities': Activity.objects.count(),
            'audit_logs': AuditLog.objects.count(),
        }
