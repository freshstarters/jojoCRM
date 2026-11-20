import json

from django.core import mail
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from ..models import (
    Account,
    Activity,
    Appointment,
    AuditLog,
    Contact,
    Deal,
    Job,
    Lead,
    PasskeyCredential,
    Role,
    User,
    Workspace,
)
from ..service.catalog_service import initialize_workspace_catalog
from ..service.role_service import ensure_workspace_default_roles
from .helpers import extract_token_from_email_body


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    DEFAULT_FROM_EMAIL='noreply@jojocrm.local',
)
class S24RolesAndSharingTests(TestCase):
    def _create_workspace_and_users(self, *, business_type='general_services', slug='kora-s24'):
        ws = Workspace.objects.create(
            name='Kora Industrial S24',
            slug=slug,
            business_type=business_type,
        )
        initialize_workspace_catalog(ws, business_type)
        owner = User.objects.create_user(
            email=f'owner@{slug}.example.com',
            workspace=ws,
            password='OwnerPassword123!',
            email_verified=True,
            email_verified_at=timezone.now(),
        )
        ws.created_by = owner
        ws.save(update_fields=['created_by'])
        owner_role, staff_role = ensure_workspace_default_roles(ws)
        owner.refresh_from_db()

        staff_a = User.objects.create_user(
            email=f'staff-a@{slug}.example.com',
            workspace=ws,
            role=staff_role,
            password='StaffPassword123!',
            email_verified=True,
            email_verified_at=timezone.now(),
        )
        staff_b = User.objects.create_user(
            email=f'staff-b@{slug}.example.com',
            workspace=ws,
            role=staff_role,
            password='StaffPassword123!',
            email_verified=True,
            email_verified_at=timezone.now(),
        )
        return ws, owner, owner_role, staff_role, staff_a, staff_b

    def _logged_in_client(self, user: User) -> Client:
        client = Client()
        client.force_login(user)
        return client

    def test_clone_staff_copies_rights_and_switches_and_edits_field_rights_and_switches(self):
        ws, owner, owner_role, staff_role, _staff_a, _staff_b = self._create_workspace_and_users()
        owner_client = self._logged_in_client(owner)

        # Configure Staff with custom switch, object right, and field right before cloning.
        patch_staff = owner_client.patch(
            f'/api/v1/workspaces/{ws.slug}/roles/{staff_role.id}/',
            data=json.dumps({
                'switches': {'export': True},
                'object_rights': {'deal': {'delete': False, 'update': True}},
                'field_rights': {'deal': {'estimated_margin': 'hidden'}},
            }),
            content_type='application/json',
        )
        self.assertEqual(patch_staff.status_code, 200)

        # Clone from Staff
        clone_res = owner_client.post(
            f'/api/v1/workspaces/{ws.slug}/roles/',
            data=json.dumps({'name': 'Field Sales Rep', 'description': 'Regional rep role'}),
            content_type='application/json',
        )
        self.assertEqual(clone_res.status_code, 201)
        cloned = clone_res.json()['role']
        self.assertEqual(cloned['name'], 'Field Sales Rep')
        self.assertFalse(cloned['is_owner'])
        self.assertFalse(cloned['is_default_staff'])
        self.assertTrue(cloned['is_removable'])
        self.assertTrue(cloned['switches']['export'])
        self.assertFalse(cloned['switches']['users_and_roles'])
        self.assertEqual(cloned['field_rights']['deal']['estimated_margin'], 'hidden')
        self.assertEqual(cloned['object_rights']['deal']['delete'], False)
        self.assertEqual(cloned['object_rights']['job']['update'], True)
        self.assertEqual(cloned['object_rights']['appointment']['read'], True)
        self.assertEqual(cloned['object_rights']['visit']['read'], True)

        # Duplicate CI name rejected
        dup_res = owner_client.post(
            f'/api/v1/workspaces/{ws.slug}/roles/',
            data=json.dumps({'name': 'field sales rep'}),
            content_type='application/json',
        )
        self.assertEqual(dup_res.status_code, 400)

        # Owner role remains immutable
        owner_patch = owner_client.patch(
            f'/api/v1/workspaces/{ws.slug}/roles/{owner_role.id}/',
            data=json.dumps({'field_rights': {'deal': {'estimated_margin': 'hidden'}}}),
            content_type='application/json',
        )
        self.assertEqual(owner_patch.status_code, 403)

        # Update all 7 switches + field rights (including Job, Appointment, Visit) on cloned role
        update_clone = owner_client.patch(
            f'/api/v1/workspaces/{ws.slug}/roles/{cloned["id"]}/',
            data=json.dumps({
                'switches': {
                    'import': True,
                    'export': False,
                    'automations': True,
                    'scripts': True,
                    'users_and_roles': True,
                    'theme_and_layouts': True,
                    'unattended_agent': True,
                },
                'field_rights': {
                    'deal': {
                        'estimated_margin': 'hidden',
                        'amount': 'read_only',
                        'notes': 'editable',
                    },
                    'job': {'notes': 'read_only'},
                    'appointment': {'notes': 'hidden'},
                    'visit': {'notes': 'hidden', 'follow_up': 'read_only'},
                },
            }),
            content_type='application/json',
        )
        self.assertEqual(update_clone.status_code, 200)
        updated_role = update_clone.json()['role']
        self.assertTrue(updated_role['switches']['automations'])
        self.assertTrue(updated_role['switches']['unattended_agent'])
        self.assertFalse(updated_role['switches']['export'])
        self.assertEqual(updated_role['field_rights']['deal']['estimated_margin'], 'hidden')
        self.assertEqual(updated_role['field_rights']['deal']['amount'], 'read_only')
        self.assertEqual(updated_role['field_rights']['deal']['notes'], 'editable')
        self.assertEqual(updated_role['field_rights']['visit']['notes'], 'hidden')
        self.assertEqual(updated_role['field_rights']['visit']['follow_up'], 'read_only')

        # Audit entries recorded for clone creation and updates
        self.assertTrue(
            AuditLog.objects.filter(
                workspace=ws,
                object_type='role',
                object_id=str(cloned['id']),
                action=AuditLog.ACTION_CREATE,
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                workspace=ws,
                object_type='role',
                object_id=str(cloned['id']),
                field_name='field_rights.deal.amount',
                new_value='read_only',
            ).exists()
        )

    def test_field_rights_hidden_read_only_editable_and_owner_override(self):
        ws, owner, _owner_role, _staff_role, staff_a, _staff_b = self._create_workspace_and_users(
            slug='kora-fields'
        )
        owner_client = self._logged_in_client(owner)

        clone_res = owner_client.post(
            f'/api/v1/workspaces/{ws.slug}/roles/',
            data=json.dumps({'name': 'Junior Sales'}),
            content_type='application/json',
        )
        junior_id = clone_res.json()['role']['id']

        # Hide estimated_margin, freeze amount (read_only), keep notes editable.
        # Also test hiding primary title field on Job -> display_label fallback.
        owner_client.patch(
            f'/api/v1/workspaces/{ws.slug}/roles/{junior_id}/',
            data=json.dumps({
                'field_rights': {
                    'deal': {
                        'estimated_margin': 'hidden',
                        'amount': 'read_only',
                        'notes': 'editable',
                    },
                    'job': {'title': 'hidden', 'notes': 'read_only'},
                },
            }),
            content_type='application/json',
        )

        # Reassign staff_a to Junior Sales
        reassign_res = owner_client.patch(
            f'/api/v1/workspaces/{ws.slug}/members/{staff_a.id}/',
            data=json.dumps({'role_id': junior_id}),
            content_type='application/json',
        )
        self.assertEqual(reassign_res.status_code, 200)

        acc = Account.objects.create(workspace=ws, owner=owner, created_by=owner, name='Accra Solar')
        deal = Deal.objects.create(
            workspace=ws,
            owner=owner,
            created_by=owner,
            account=acc,
            title='Grid Battery Expansion',
            amount='25000.00',
            estimated_margin='7500.00',
            notes='Initial notes',
        )
        job = Job.objects.create(
            workspace=ws,
            owner=owner,
            created_by=owner,
            account=acc,
            title='Confidential Site Audit',
            notes='Audit notes',
        )

        # Owner still sees hidden fields
        owner_detail = owner_client.get(f'/api/v1/workspaces/{ws.slug}/records/deal/{deal.id}/')
        self.assertEqual(owner_detail.status_code, 200)
        self.assertEqual(owner_detail.json()['record']['estimated_margin'], '7500.00')

        # Junior Sales sees amount (read-only), notes (editable), and does NOT see estimated_margin
        staff_client = self._logged_in_client(staff_a)
        staff_list = staff_client.get(f'/api/v1/workspaces/{ws.slug}/records/deal/')
        self.assertEqual(staff_list.status_code, 200)
        row = staff_list.json()['results'][0]
        self.assertNotIn('estimated_margin', row)
        self.assertEqual(row['amount'], '25000.00')
        self.assertEqual(row['notes'], 'Initial notes')

        staff_detail = staff_client.get(f'/api/v1/workspaces/{ws.slug}/records/deal/{deal.id}/')
        self.assertEqual(staff_detail.status_code, 200)
        self.assertNotIn('estimated_margin', staff_detail.json()['record'])
        self.assertEqual(staff_detail.json()['record']['amount'], '25000.00')

        # Hidden primary label field on Job falls back display_label to JOB-<id>
        job_detail = staff_client.get(f'/api/v1/workspaces/{ws.slug}/records/job/{job.id}/')
        self.assertEqual(job_detail.status_code, 200)
        job_rec = job_detail.json()['record']
        self.assertNotIn('title', job_rec)
        self.assertEqual(job_rec['display_label'], f'JOB-{job.id}')

        # Saving hidden field fails closed (403)
        save_hidden = staff_client.patch(
            f'/api/v1/workspaces/{ws.slug}/records/deal/{deal.id}/',
            data=json.dumps({'estimated_margin': '100.00'}),
            content_type='application/json',
        )
        self.assertEqual(save_hidden.status_code, 403)

        # Saving read-only field fails closed (403)
        save_ro = staff_client.patch(
            f'/api/v1/workspaces/{ws.slug}/records/deal/{deal.id}/',
            data=json.dumps({'amount': '99999.00'}),
            content_type='application/json',
        )
        self.assertEqual(save_ro.status_code, 403)

        # Saving editable field succeeds (200)
        save_editable = staff_client.patch(
            f'/api/v1/workspaces/{ws.slug}/records/deal/{deal.id}/',
            data=json.dumps({'notes': 'Updated by junior rep'}),
            content_type='application/json',
        )
        self.assertEqual(save_editable.status_code, 200)
        self.assertEqual(save_editable.json()['record']['notes'], 'Updated by junior rep')
        self.assertNotIn('estimated_margin', save_editable.json()['record'])

    def test_invite_with_cloned_role_and_member_reassignment_and_live_version_header(self):
        ws, owner, owner_role, staff_role, staff_a, _staff_b = self._create_workspace_and_users(
            slug='kora-invites'
        )
        owner_client = self._logged_in_client(owner)

        clone_res = owner_client.post(
            f'/api/v1/workspaces/{ws.slug}/roles/',
            data=json.dumps({'name': 'Support Specialist'}),
            content_type='application/json',
        )
        support_role_id = clone_res.json()['role']['id']

        # Cannot invite with Owner role
        bad_invite = owner_client.post(
            f'/api/v1/workspaces/{ws.slug}/invites/',
            data=json.dumps({'email': 'nope@example.com', 'role_id': owner_role.id}),
            content_type='application/json',
        )
        self.assertEqual(bad_invite.status_code, 403)

        # Invite with cloned role
        mail.outbox.clear()
        invite_res = owner_client.post(
            f'/api/v1/workspaces/{ws.slug}/invites/',
            data=json.dumps({'email': 'newrep@example.com', 'role_id': support_role_id}),
            content_type='application/json',
        )
        self.assertEqual(invite_res.status_code, 201)
        self.assertEqual(invite_res.json()['invite']['role']['name'], 'Support Specialist')
        token = extract_token_from_email_body(mail.outbox[-1].body)

        # Preview shows invited role
        anon = Client()
        preview = anon.get(f'/api/v1/invites/preview/?token={token}')
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()['role']['name'], 'Support Specialist')

        # Accepting invite assigns Support Specialist
        accept = anon.post(
            '/api/v1/invites/accept/',
            data=json.dumps({'token': token, 'password': 'NewRepPassword123!'}),
            content_type='application/json',
        )
        self.assertEqual(accept.status_code, 200)
        self.assertEqual(accept.json()['role']['name'], 'Support Specialist')
        new_user = User.objects.get(email='newrep@example.com')
        self.assertEqual(new_user.role_id, support_role_id)

        # Live session permission version changes immediately when Owner reassigns staff_a or edits role
        staff_client = self._logged_in_client(staff_a)
        res1 = staff_client.get(f'/api/v1/workspaces/{ws.slug}/')
        self.assertEqual(res1.status_code, 200)
        v1 = res1.headers.get('X-Permissions-Version')
        self.assertTrue(v1)
        self.assertEqual(res1.json()['role']['name'], 'Staff')

        # Reassign staff_a to Support Specialist
        reassign = owner_client.patch(
            f'/api/v1/workspaces/{ws.slug}/members/{staff_a.id}/',
            data=json.dumps({'role_id': support_role_id}),
            content_type='application/json',
        )
        self.assertEqual(reassign.status_code, 200)
        self.assertTrue(
            AuditLog.objects.filter(
                workspace=ws,
                object_type='user',
                object_id=str(staff_a.id),
                field_name='role',
                previous_value='Staff',
                new_value='Support Specialist',
            ).exists()
        )

        # Without re-logging in, staff_a immediately gets updated role and new X-Permissions-Version
        res2 = staff_client.get(f'/api/v1/workspaces/{ws.slug}/')
        self.assertEqual(res2.status_code, 200)
        v2 = res2.headers.get('X-Permissions-Version')
        self.assertNotEqual(v1, v2)
        self.assertEqual(res2.json()['role']['name'], 'Support Specialist')

        # Updating Support Specialist object rights revokes deal.create immediately and rotates version
        owner_client.patch(
            f'/api/v1/workspaces/{ws.slug}/roles/{support_role_id}/',
            data=json.dumps({'object_rights': {'deal': {'create': False}}}),
            content_type='application/json',
        )
        res3 = staff_client.get(f'/api/v1/workspaces/{ws.slug}/')
        v3 = res3.headers.get('X-Permissions-Version')
        self.assertNotEqual(v2, v3)
        self.assertFalse(res3.json()['permissions']['object_rights']['deal']['create'])

        blocked_create = staff_client.post(
            f'/api/v1/workspaces/{ws.slug}/records/deal/',
            data=json.dumps({'title': 'Blocked Deal'}),
            content_type='application/json',
        )
        self.assertEqual(blocked_create.status_code, 403)

    def test_record_sharing_three_visibility_choices_and_sharing_authorization(self):
        ws, owner, _owner_role, staff_role, staff_a, staff_b = self._create_workspace_and_users(
            slug='kora-sharing'
        )
        owner_client = self._logged_in_client(owner)

        clone_res = owner_client.post(
            f'/api/v1/workspaces/{ws.slug}/roles/',
            data=json.dumps({'name': 'VIP Sales'}),
            content_type='application/json',
        )
        vip_role_id = clone_res.json()['role']['id']
        owner_client.patch(
            f'/api/v1/workspaces/{ws.slug}/members/{staff_a.id}/',
            data=json.dumps({'role_id': vip_role_id}),
            content_type='application/json',
        )

        staff_a_client = self._logged_in_client(staff_a)
        staff_b_client = self._logged_in_client(staff_b)

        # 1. Owner-only record created by staff_a
        create_res = staff_a_client.post(
            f'/api/v1/workspaces/{ws.slug}/records/account/',
            data=json.dumps({'name': 'Private Client Ltd', 'visibility': 'owner'}),
            content_type='application/json',
        )
        self.assertEqual(create_res.status_code, 201)
        acc_id = create_res.json()['record']['id']

        # Visible to record owner (staff_a) and workspace Owner; invisible to staff_b
        self.assertEqual(staff_a_client.get(f'/api/v1/workspaces/{ws.slug}/records/account/{acc_id}/').status_code, 200)
        self.assertEqual(owner_client.get(f'/api/v1/workspaces/{ws.slug}/records/account/{acc_id}/').status_code, 200)
        self.assertEqual(staff_b_client.get(f'/api/v1/workspaces/{ws.slug}/records/account/{acc_id}/').status_code, 404)
        self.assertEqual(staff_b_client.get(f'/api/v1/workspaces/{ws.slug}/records/account/').json()['total_count'], 0)

        # 2. Share with one role (Staff -> staff_b can see it)
        share_role = staff_a_client.patch(
            f'/api/v1/workspaces/{ws.slug}/records/account/{acc_id}/',
            data=json.dumps({'visibility': 'role', 'shared_role_id': staff_role.id}),
            content_type='application/json',
        )
        self.assertEqual(share_role.status_code, 200)
        self.assertEqual(staff_b_client.get(f'/api/v1/workspaces/{ws.slug}/records/account/{acc_id}/').status_code, 200)

        # Non-owner editor (staff_b) cannot change sharing settings (403)
        hijack_share = staff_b_client.patch(
            f'/api/v1/workspaces/{ws.slug}/records/account/{acc_id}/',
            data=json.dumps({'visibility': 'owner'}),
            content_type='application/json',
        )
        self.assertEqual(hijack_share.status_code, 403)

        # Share with VIP Sales role instead -> staff_b loses access again
        share_vip = staff_a_client.patch(
            f'/api/v1/workspaces/{ws.slug}/records/account/{acc_id}/',
            data=json.dumps({'visibility': 'role', 'shared_role_id': vip_role_id}),
            content_type='application/json',
        )
        self.assertEqual(share_vip.status_code, 200)
        self.assertEqual(staff_b_client.get(f'/api/v1/workspaces/{ws.slug}/records/account/{acc_id}/').status_code, 404)

        # 3. Share with whole workspace -> everyone can see it
        share_ws = staff_a_client.patch(
            f'/api/v1/workspaces/{ws.slug}/records/account/{acc_id}/',
            data=json.dumps({'visibility': 'workspace'}),
            content_type='application/json',
        )
        self.assertEqual(share_ws.status_code, 200)
        self.assertIsNone(share_ws.json()['record']['shared_role_id'])
        self.assertEqual(staff_b_client.get(f'/api/v1/workspaces/{ws.slug}/records/account/{acc_id}/').status_code, 200)

    def test_delete_cloned_role_falls_back_shared_records_to_owner_and_blocks_self_lockout(self):
        ws, owner, _owner_role, staff_role, staff_a, _staff_b = self._create_workspace_and_users(
            slug='kora-role-del'
        )
        owner_client = self._logged_in_client(owner)

        # Create manager role with users_and_roles=True and assign to staff_a
        mgr_id = owner_client.post(
            f'/api/v1/workspaces/{ws.slug}/roles/',
            data=json.dumps({'name': 'Ops Manager'}),
            content_type='application/json',
        ).json()['role']['id']
        owner_client.patch(
            f'/api/v1/workspaces/{ws.slug}/roles/{mgr_id}/',
            data=json.dumps({'switches': {'users_and_roles': True}}),
            content_type='application/json',
        )
        owner_client.patch(
            f'/api/v1/workspaces/{ws.slug}/members/{staff_a.id}/',
            data=json.dumps({'role_id': mgr_id}),
            content_type='application/json',
        )

        # Non-Owner manager cannot reassign self or disable users_and_roles on own role
        mgr_client = self._logged_in_client(staff_a)
        self_reassign = mgr_client.patch(
            f'/api/v1/workspaces/{ws.slug}/members/{staff_a.id}/',
            data=json.dumps({'role_id': staff_role.id}),
            content_type='application/json',
        )
        self.assertEqual(self_reassign.status_code, 400)
        self_lockout = mgr_client.patch(
            f'/api/v1/workspaces/{ws.slug}/roles/{mgr_id}/',
            data=json.dumps({'switches': {'users_and_roles': False}}),
            content_type='application/json',
        )
        self.assertEqual(self_lockout.status_code, 400)

        # Create a disposable role shared on a record
        temp_id = owner_client.post(
            f'/api/v1/workspaces/{ws.slug}/roles/',
            data=json.dumps({'name': 'Temp Reviewers'}),
            content_type='application/json',
        ).json()['role']['id']
        acc = Account.objects.create(
            workspace=ws,
            owner=owner,
            created_by=owner,
            name='Shared Acc',
            visibility='role',
            shared_role_id=temp_id,
        )

        # Roles listing exposes shared_record_count=1 for pre-delete warning
        roles_list = owner_client.get(f'/api/v1/workspaces/{ws.slug}/roles/').json()['roles']
        temp_role_data = next(r for r in roles_list if r['id'] == temp_id)
        self.assertEqual(temp_role_data['shared_record_count'], 1)

        # Deleting Temp Reviewers switches Shared Acc to visibility='owner' and audits it
        del_res = owner_client.delete(f'/api/v1/workspaces/{ws.slug}/roles/{temp_id}/')
        self.assertEqual(del_res.status_code, 200)
        self.assertEqual(del_res.json()['fallback_records_count'], 1)
        acc.refresh_from_db()
        self.assertEqual(acc.visibility, 'owner')
        self.assertIsNone(acc.shared_role_id)

    def test_set_initial_password_and_rename_passkey_in_settings(self):
        ws, owner, _owner_role, staff_role, _staff_a, _staff_b = self._create_workspace_and_users(
            slug='kora-security'
        )
        # User created via SSO with no usable password
        sso_user = User.objects.create_user(
            email='sso-user@kora-security.example.com',
            workspace=ws,
            role=staff_role,
            password=None,
            email_verified=True,
            email_verified_at=timezone.now(),
        )
        self.assertFalse(sso_user.has_usable_password())

        sso_client = self._logged_in_client(sso_user)
        methods_before = sso_client.get('/api/v1/auth/methods/').json()
        self.assertFalse(methods_before['has_password'])

        # Set initial password while signed in
        set_res = sso_client.post(
            '/api/v1/account/password/set/',
            data=json.dumps({'new_password': 'InitialStrongPassword123!'}),
            content_type='application/json',
        )
        self.assertEqual(set_res.status_code, 200)
        self.assertTrue(set_res.json()['has_password'])

        sso_user.refresh_from_db()
        self.assertTrue(sso_user.has_usable_password())
        self.assertTrue(sso_user.check_password('InitialStrongPassword123!'))

        # Session stays signed in and calling /password/set/ again is refused (400)
        second_set = sso_client.post(
            '/api/v1/account/password/set/',
            data=json.dumps({'new_password': 'AnotherStrongPassword123!'}),
            content_type='application/json',
        )
        self.assertEqual(second_set.status_code, 400)

        # Register a passkey record and rename it via PATCH
        pk = PasskeyCredential.objects.create(
            user=sso_user,
            credential_id='cred-test-rename-1',
            public_key='pubkey-placeholder',
            name='Default Key',
        )
        rename_res = sso_client.patch(
            f'/api/v1/auth/passkey/{pk.id}/',
            data=json.dumps({'name': 'Accra Work Laptop'}),
            content_type='application/json',
        )
        self.assertEqual(rename_res.status_code, 200)
        self.assertEqual(rename_res.json()['passkey']['name'], 'Accra Work Laptop')

        pk.refresh_from_db()
        self.assertEqual(pk.name, 'Accra Work Laptop')
        self.assertTrue(
            AuditLog.objects.filter(
                workspace=ws,
                object_type='passkey',
                object_id=str(pk.id),
                field_name='name',
                previous_value='Default Key',
                new_value='Accra Work Laptop',
            ).exists()
        )

        # Another user cannot rename sso_user's passkey (404)
        owner_client = self._logged_in_client(owner)
        cross_rename = owner_client.patch(
            f'/api/v1/auth/passkey/{pk.id}/',
            data=json.dumps({'name': 'Stolen Name'}),
            content_type='application/json',
        )
        self.assertEqual(cross_rename.status_code, 404)

    def test_lead_conversion_enforces_field_rights_on_preview_and_convert(self):
        ws, owner, _owner_role, staff_role, staff_user, _staff_b = self._create_workspace_and_users(
            slug='kora-s24-conv-fr'
        )
        owner_client = self._logged_in_client(owner)

        # Clone Staff and restrict fields across Account, Contact, and Deal
        clone_res = owner_client.post(
            f'/api/v1/workspaces/{ws.slug}/roles/',
            data=json.dumps({'name': 'Restricted Converter', 'source_role_id': staff_role.id}),
            content_type='application/json',
        )
        self.assertEqual(clone_res.status_code, 201)
        restricted_role_id = clone_res.json()['role']['id']

        patch_res = owner_client.patch(
            f'/api/v1/workspaces/{ws.slug}/roles/{restricted_role_id}/',
            data=json.dumps({
                'field_rights': {
                    'account': {'email': 'hidden', 'city': 'read_only'},
                    'contact': {'phone': 'hidden', 'title': 'read_only', 'account': 'read_only'},
                    'deal': {'amount': 'hidden', 'win_probability': 'read_only'},
                }
            }),
            content_type='application/json',
        )
        self.assertEqual(patch_res.status_code, 200)

        staff_user.role = Role.objects.get(id=restricted_role_id)
        staff_user.save(update_fields=['role'])

        existing_acc = Account.objects.create(
            workspace=ws,
            owner=owner,
            name='Volta Metals Ltd',
            city='Tema',
            email='secret@voltametals.gh',
            phone='+233200001111',
        )
        standalone_contact = Contact.objects.create(
            workspace=ws,
            owner=owner,
            name='Kwame Mensah',
            title='Director',
            email='kwame@voltametals.gh',
            phone='+233244998877',
        )
        lead = Lead.objects.create(
            workspace=ws,
            owner=staff_user,
            name='Kwame Mensah',
            company='Volta Metals Ltd',
            email='secret@voltametals.gh',
            phone='+233244998877',
        )

        staff_client = self._logged_in_client(staff_user)

        # Preview strips hidden Account/Contact fields and never matches on hidden fields
        prev_res = staff_client.get(
            f'/api/v1/workspaces/{ws.slug}/records/lead/{lead.id}/conversion-preview/'
        )
        self.assertEqual(prev_res.status_code, 200)
        prev_data = prev_res.json()

        self.assertEqual(prev_data['field_rights']['deal']['amount'], 'hidden')
        self.assertNotIn('deal_amount', prev_data['defaults'])
        self.assertFalse(prev_data['capabilities']['can_link_standalone_contact'])

        acc_matches = prev_data['account_matches']
        self.assertEqual(len(acc_matches), 1)
        self.assertEqual(acc_matches[0]['id'], existing_acc.id)
        self.assertNotIn('email', acc_matches[0])
        self.assertNotIn('Email', acc_matches[0]['match_reasons'])
        self.assertIn('Company name', acc_matches[0]['match_reasons'])

        con_matches = prev_data['contact_matches']
        self.assertEqual(len(con_matches), 1)
        self.assertEqual(con_matches[0]['id'], standalone_contact.id)
        self.assertNotIn('phone', con_matches[0])
        self.assertNotIn('Phone', con_matches[0]['match_reasons'])
        self.assertIn('Name', con_matches[0]['match_reasons'])

        # Passing hidden/read-only field overrides is rejected with 403
        blocked_amount = staff_client.post(
            f'/api/v1/workspaces/{ws.slug}/records/lead/{lead.id}/convert/',
            data=json.dumps({'deal_amount': '9999.00'}),
            content_type='application/json',
        )
        self.assertEqual(blocked_amount.status_code, 403)

        blocked_city = staff_client.post(
            f'/api/v1/workspaces/{ws.slug}/records/lead/{lead.id}/convert/',
            data=json.dumps({'account_city': 'Kumasi'}),
            content_type='application/json',
        )
        self.assertEqual(blocked_city.status_code, 403)

        # Linking a standalone Contact is blocked (403) when contact.account is read_only
        blocked_standalone_link = staff_client.post(
            f'/api/v1/workspaces/{ws.slug}/records/lead/{lead.id}/convert/',
            data=json.dumps({
                'account_id': existing_acc.id,
                'contact_id': standalone_contact.id,
            }),
            content_type='application/json',
        )
        self.assertEqual(blocked_standalone_link.status_code, 403)

        # Omitting non-writable fields succeeds using safe defaults and strips hidden fields on response
        ok_res = staff_client.post(
            f'/api/v1/workspaces/{ws.slug}/records/lead/{lead.id}/convert/',
            data=json.dumps({
                'account_name': 'Volta Metals New',
                'contact_name': 'Kwame Mensah',
                'deal_title': 'Volta Metals Retainer',
            }),
            content_type='application/json',
        )
        self.assertEqual(ok_res.status_code, 200)
        ok_body = ok_res.json()
        self.assertNotIn('amount', ok_body['deal'])
        self.assertNotIn('email', ok_body['account'])
        self.assertNotIn('phone', ok_body['contact'])

    def test_business_type_starter_roles_are_created_and_deletable(self):
        from ..catalog import BUSINESS_TYPES
        from ..service.catalog_service import initialize_workspace_catalog

        # Every business type defines 3-5 unique starter roles distinct from Owner and Staff
        for bt in BUSINESS_TYPES:
            self.assertGreaterEqual(len(bt.starter_roles), 3, bt.key)
            self.assertLessEqual(len(bt.starter_roles), 5, bt.key)
            names_lower = [sr.name.lower() for sr in bt.starter_roles]
            self.assertEqual(len(names_lower), len(set(names_lower)), bt.key)
            self.assertNotIn('owner', names_lower, bt.key)
            self.assertNotIn('staff', names_lower, bt.key)

        ws = Workspace.objects.create(
            name='Starter Roles Health Clinic',
            slug='starter-roles-health',
            business_type='healthcare_wellness',
        )
        initialize_workspace_catalog(ws, 'healthcare_wellness')
        owner = User.objects.create_user(
            email='owner@starter-roles-health.example.com',
            workspace=ws,
            password='OwnerPassword123!',
            email_verified=True,
            email_verified_at=timezone.now(),
        )
        ws.created_by = owner
        ws.save(update_fields=['created_by'])
        ensure_workspace_default_roles(ws)
        owner.refresh_from_db()

        roles_qs = Role.objects.filter(workspace=ws)
        self.assertEqual(roles_qs.count(), 6)  # Owner + Staff + 4 starter roles

        starter_role = roles_qs.filter(is_owner=False, is_default_staff=False).first()
        self.assertIsNotNone(starter_role)
        owner_client = self._logged_in_client(owner)

        del_res = owner_client.delete(
            f'/api/v1/workspaces/{ws.slug}/roles/{starter_role.id}/'
        )
        self.assertEqual(del_res.status_code, 200)

        # Calling GET /roles/ or ensure_workspace_default_roles must NOT recreate a deleted starter role
        roles_after = owner_client.get(f'/api/v1/workspaces/{ws.slug}/roles/')
        self.assertEqual(roles_after.status_code, 200)
        self.assertEqual(len(roles_after.json()['roles']), 5)
        self.assertFalse(
            Role.objects.filter(workspace=ws, id=starter_role.id).exists()
        )
