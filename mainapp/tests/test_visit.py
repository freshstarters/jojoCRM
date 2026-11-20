import json
from datetime import datetime, timezone as dt_timezone

from django.test import Client, TestCase
from django.utils import timezone

from mainapp.models import (
    Activity,
    Appointment,
    AuditLog,
    Contact,
    Role,
    User,
    Visit,
    Workspace,
    WorkspaceTable,
)
from mainapp.service.catalog_service import initialize_workspace_catalog
from mainapp.service.role_service import clone_role_from_source, ensure_workspace_default_roles


class S26VisitTests(TestCase):
    def _create_workspace_and_users(
        self,
        *,
        slug: str = 'accra-wellness',
        name: str = 'Accra Wellness Clinic',
        business_type: str = 'healthcare_wellness',
    ):
        ws = Workspace.objects.create(
            name=name,
            slug=slug,
            business_type=business_type,
        )
        bt = initialize_workspace_catalog(ws, business_type)
        ws.business_type_label = bt.label
        ws.contact_label = bt.default_contact_label
        ws.save(update_fields=['business_type_label', 'contact_label'])

        owner_role, staff_role = ensure_workspace_default_roles(ws)
        owner = User.objects.create_user(
            email=f'owner@{slug}.example',
            password='StrongPassword123!',
            workspace=ws,
            role=owner_role,
            email_verified=True,
            email_verified_at=timezone.now(),
            timezone='Africa/Accra',
        )
        ws.created_by = owner
        ws.save(update_fields=['created_by'])

        staff_a = User.objects.create_user(
            email=f'nurse@{slug}.example',
            password='StrongPassword123!',
            workspace=ws,
            role=staff_role,
            email_verified=True,
            email_verified_at=timezone.now(),
            timezone='Africa/Accra',
        )
        staff_b = User.objects.create_user(
            email=f'desk@{slug}.example',
            password='StrongPassword123!',
            workspace=ws,
            role=staff_role,
            email_verified=True,
            email_verified_at=timezone.now(),
            timezone='Africa/Accra',
        )
        return ws, owner, owner_role, staff_role, staff_a, staff_b

    def _logged_in_client(self, user: User) -> Client:
        client = Client()
        client.force_login(user)
        return client

    def test_healthcare_visit_crud_linked_to_patient_and_no_clinical_ehr_fields(self):
        ws, owner, _, _, staff_a, _ = self._create_workspace_and_users()
        client = self._logged_in_client(owner)

        # Healthcare labels Contact as Patient and enables Visit
        self.assertEqual(ws.contact_label, 'Patient')
        self.assertTrue(
            WorkspaceTable.objects.filter(
                workspace=ws, object_key='visit', is_enabled=True
            ).exists()
        )

        # Model schema check: no diagnosis code, chart, lab result, or prescription fields
        model_field_names = {field.name for field in Visit._meta.get_fields()}
        for forbidden in (
            'diagnosis',
            'diagnosis_code',
            'icd_code',
            'chart',
            'medical_chart',
            'lab',
            'lab_result',
            'lab_results',
            'prescription',
            'prescriptions',
            'medication',
        ):
            self.assertNotIn(forbidden, model_field_names)

        # Create a Patient
        patient_res = client.post(
            f'/api/v1/workspaces/{ws.slug}/records/contact/',
            data=json.dumps({'name': 'Abena Mensah', 'phone': '+233201234567'}),
            content_type='application/json',
        )
        self.assertEqual(patient_res.status_code, 201)
        patient_id = patient_res.json()['record']['id']

        # Create a Visit against that Patient with date, reason, notes, follow_up, and optional assignee
        create_res = client.post(
            f'/api/v1/workspaces/{ws.slug}/records/visit/',
            data=json.dumps({
                'contact_id': patient_id,
                'date': '2026-11-20T09:30',
                'reason': 'Annual wellness checkup',
                'notes': 'Reviewed blood pressure log and hydration habits.',
                'follow_up': 'Schedule follow-up appointment in 4 weeks.',
                'assignee_id': staff_a.id,
            }),
            content_type='application/json',
        )
        self.assertEqual(create_res.status_code, 201)
        record = create_res.json()['record']
        visit_id = record['id']
        self.assertEqual(record['object_key'], 'visit')
        self.assertEqual(record['record_code'], f'VIS-{visit_id}')
        self.assertEqual(record['contact_id'], patient_id)
        self.assertEqual(record['contact']['name'], 'Abena Mensah')
        self.assertEqual(record['reason'], 'Annual wellness checkup')
        self.assertEqual(record['notes'], 'Reviewed blood pressure log and hydration habits.')
        self.assertEqual(record['follow_up'], 'Schedule follow-up appointment in 4 weeks.')
        self.assertEqual(record['assignee_id'], staff_a.id)
        self.assertTrue(record['date'].startswith('2026-11-20T09:30:00'))
        self.assertEqual(record['visited_at'], record['date'])
        self.assertEqual(record['linked_records']['contact']['id'], patient_id)

        # Serialized response must not expose clinical EHR fields
        for forbidden in (
            'diagnosis',
            'diagnosis_code',
            'chart',
            'lab',
            'lab_result',
            'prescription',
        ):
            self.assertNotIn(forbidden, record)

        # Patient detail shows linked Visits
        patient_detail = client.get(
            f'/api/v1/workspaces/{ws.slug}/records/contact/{patient_id}/'
        )
        self.assertEqual(patient_detail.status_code, 200)
        linked_visits = patient_detail.json()['record']['linked_records']['visits']
        self.assertEqual(len(linked_visits), 1)
        self.assertEqual(linked_visits[0]['id'], visit_id)
        self.assertEqual(linked_visits[0]['reason'], 'Annual wellness checkup')
        self.assertEqual(
            linked_visits[0]['follow_up'],
            'Schedule follow-up appointment in 4 weeks.',
        )

        # Update Visit (also accepting visited_at alias)
        patch_res = client.patch(
            f'/api/v1/workspaces/{ws.slug}/records/visit/{visit_id}/',
            data=json.dumps({
                'visited_at': '2026-11-21T14:15:00Z',
                'reason': 'Annual wellness checkup (updated)',
                'notes': 'Updated wellness notes.',
                'follow_up': 'Call patient on 2026-12-18.',
            }),
            content_type='application/json',
        )
        self.assertEqual(patch_res.status_code, 200)
        updated = patch_res.json()['record']
        self.assertEqual(updated['reason'], 'Annual wellness checkup (updated)')
        self.assertEqual(updated['notes'], 'Updated wellness notes.')
        self.assertEqual(updated['follow_up'], 'Call patient on 2026-12-18.')
        self.assertTrue(updated['date'].startswith('2026-11-21T14:15:00'))

        # Activities attach cleanly to Visit records
        act_res = client.post(
            f'/api/v1/workspaces/{ws.slug}/records/visit/{visit_id}/activities/',
            data=json.dumps({
                'activity_type': 'task',
                'summary': 'Send follow-up reminder SMS',
            }),
            content_type='application/json',
        )
        self.assertEqual(act_res.status_code, 201)
        self.assertEqual(
            Activity.objects.filter(
                workspace=ws, target_object_type='visit', target_object_id=visit_id
            ).count(),
            1,
        )

        # Deleting Patient while Visit exists is refused with 409 naming Patient
        del_patient_blocked = client.delete(
            f'/api/v1/workspaces/{ws.slug}/records/contact/{patient_id}/'
        )
        self.assertEqual(del_patient_blocked.status_code, 409)
        self.assertIn('Patient', del_patient_blocked.json()['error'])
        self.assertIn('Visits', del_patient_blocked.json()['error'])

        # Turning off Visit table while Visit records exist is refused with 409
        disable_visit_blocked = client.patch(
            f'/api/v1/workspaces/{ws.slug}/tables/visit/',
            data=json.dumps({'is_enabled': False}),
            content_type='application/json',
        )
        self.assertEqual(disable_visit_blocked.status_code, 409)

        # Delete Visit cleans up its activities and allows Patient deletion & table toggle
        del_visit = client.delete(
            f'/api/v1/workspaces/{ws.slug}/records/visit/{visit_id}/'
        )
        self.assertEqual(del_visit.status_code, 200)
        self.assertFalse(Visit.objects.filter(id=visit_id).exists())
        self.assertEqual(
            Activity.objects.filter(
                workspace=ws, target_object_type='visit', target_object_id=visit_id
            ).count(),
            0,
        )

        del_patient_ok = client.delete(
            f'/api/v1/workspaces/{ws.slug}/records/contact/{patient_id}/'
        )
        self.assertEqual(del_patient_ok.status_code, 200)

    def test_clinical_ehr_fields_explicitly_refused_and_validation_rules(self):
        ws, owner, _, _, _, _ = self._create_workspace_and_users(slug='ehr-guard')
        client = self._logged_in_client(owner)
        patient = Contact.objects.create(workspace=ws, owner=owner, created_by=owner, name='Kwame Boateng')

        # Missing required fields rejected with 400
        missing_patient = client.post(
            f'/api/v1/workspaces/{ws.slug}/records/visit/',
            data=json.dumps({'date': '2026-11-20T10:00', 'reason': 'Checkup'}),
            content_type='application/json',
        )
        self.assertEqual(missing_patient.status_code, 400)
        self.assertIn('Patient', missing_patient.json()['error'])

        missing_date = client.post(
            f'/api/v1/workspaces/{ws.slug}/records/visit/',
            data=json.dumps({'contact_id': patient.id, 'reason': 'Checkup'}),
            content_type='application/json',
        )
        self.assertEqual(missing_date.status_code, 400)

        missing_reason = client.post(
            f'/api/v1/workspaces/{ws.slug}/records/visit/',
            data=json.dumps({'contact_id': patient.id, 'date': '2026-11-20T10:00'}),
            content_type='application/json',
        )
        self.assertEqual(missing_reason.status_code, 400)

        # Forbidden clinical/EHR keys refused on create
        for forbidden_key in (
            'diagnosis_code',
            'diagnosis',
            'chart',
            'lab_result',
            'lab',
            'prescription',
            'prescriptions',
        ):
            res = client.post(
                f'/api/v1/workspaces/{ws.slug}/records/visit/',
                data=json.dumps({
                    'contact_id': patient.id,
                    'date': '2026-11-20T10:00',
                    'reason': 'Consultation',
                    forbidden_key: 'I10 / sample',
                }),
                content_type='application/json',
            )
            self.assertEqual(res.status_code, 400, f'Expected 400 for {forbidden_key}')
            self.assertIn('diagnosis codes', res.json()['error'].lower())

        # Valid create (without assignee — assignee is optional)
        ok_res = client.post(
            f'/api/v1/workspaces/{ws.slug}/records/visit/',
            data=json.dumps({
                'contact_id': patient.id,
                'date': '2026-11-20T10:00',
                'reason': 'Wellness consultation',
                'notes': 'All good.',
                'follow_up': '',
            }),
            content_type='application/json',
        )
        self.assertEqual(ok_res.status_code, 201)
        visit_id = ok_res.json()['record']['id']
        self.assertIsNone(ok_res.json()['record']['assignee_id'])

        # Forbidden clinical/EHR keys refused on update as well
        patch_forbidden = client.patch(
            f'/api/v1/workspaces/{ws.slug}/records/visit/{visit_id}/',
            data=json.dumps({'prescription': 'Amoxicillin 500mg'}),
            content_type='application/json',
        )
        self.assertEqual(patch_forbidden.status_code, 400)
        self.assertIn('prescriptions', patch_forbidden.json()['error'].lower())

        # Clearing Patient on update is refused
        clear_patient = client.patch(
            f'/api/v1/workspaces/{ws.slug}/records/visit/{visit_id}/',
            data=json.dumps({'contact_id': None}),
            content_type='application/json',
        )
        self.assertEqual(clear_patient.status_code, 400)

    def test_general_services_does_not_include_visit_and_tenant_isolation(self):
        gs_ws, gs_owner, _, _, _, _ = self._create_workspace_and_users(
            slug='kora-general',
            name='Kora General Services',
            business_type='general_services',
        )
        hc_ws, hc_owner, _, _, _, _ = self._create_workspace_and_users(
            slug='korle-clinic',
            name='Korle Clinic',
            business_type='healthcare_wellness',
        )

        gs_client = self._logged_in_client(gs_owner)
        hc_client = self._logged_in_client(hc_owner)

        # General services does not have Visit enabled
        self.assertFalse(
            WorkspaceTable.objects.filter(
                workspace=gs_ws, object_key='visit', is_enabled=True
            ).exists()
        )
        self.assertEqual(
            gs_client.get(f'/api/v1/workspaces/{gs_ws.slug}/records/visit/').status_code,
            404,
        )
        self.assertEqual(
            gs_client.post(
                f'/api/v1/workspaces/{gs_ws.slug}/records/visit/',
                data=json.dumps({'date': '2026-11-20T10:00', 'reason': 'Test'}),
                content_type='application/json',
            ).status_code,
            404,
        )
        gs_search = gs_client.get(f'/api/v1/workspaces/{gs_ws.slug}/search/')
        self.assertEqual(gs_search.status_code, 200)
        gs_search_keys = {item['key'] for item in gs_search.json()['searchable_objects']}
        self.assertNotIn('visit', gs_search_keys)
        self.assertEqual(
            gs_client.get(f'/api/v1/workspaces/{gs_ws.slug}/search/?object_type=visit').status_code,
            404,
        )
        gs_dash = gs_client.get(f'/api/v1/workspaces/{gs_ws.slug}/dashboard/')
        self.assertEqual(gs_dash.status_code, 200)
        self.assertNotIn('visit', gs_dash.json()['readable_object_keys'])

        # Cross-workspace Patient rejected when creating a Visit on Healthcare
        gs_contact = Contact.objects.create(
            workspace=gs_ws, owner=gs_owner, created_by=gs_owner, name='Outside Contact'
        )
        cross_res = hc_client.post(
            f'/api/v1/workspaces/{hc_ws.slug}/records/visit/',
            data=json.dumps({
                'contact_id': gs_contact.id,
                'date': '2026-11-20T10:00',
                'reason': 'Cross-tenant attempt',
            }),
            content_type='application/json',
        )
        self.assertEqual(cross_res.status_code, 400)

    def test_visit_list_filters_sorting_dashboard_search_and_audit(self):
        ws, owner, _, _, staff_a, _ = self._create_workspace_and_users(slug='osu-clinic')
        client = self._logged_in_client(owner)

        p1 = Contact.objects.create(workspace=ws, owner=owner, created_by=owner, name='Ama Osei')
        p2 = Contact.objects.create(workspace=ws, owner=owner, created_by=owner, name='Kojo Appiah')

        v_older = Visit.objects.create(
            workspace=ws,
            owner=owner,
            created_by=owner,
            contact=p1,
            date=datetime(2026, 11, 10, 8, 0, tzinfo=dt_timezone.utc),
            reason='Initial intake consultation',
            notes='Baseline vitals discussed.',
            follow_up='Follow-up in 10 days',
            assignee=staff_a,
        )
        v_newer = Visit.objects.create(
            workspace=ws,
            owner=owner,
            created_by=owner,
            contact=p2,
            date=datetime(2026, 11, 19, 15, 0, tzinfo=dt_timezone.utc),
            reason='Nutrition review',
            notes='Discussed meal plan.',
            follow_up='',
        )

        # Default ordering is newest Visit date first (-date, -id)
        list_res = client.get(f'/api/v1/workspaces/{ws.slug}/records/visit/')
        self.assertEqual(list_res.status_code, 200)
        body = list_res.json()
        self.assertEqual([r['id'] for r in body['results']], [v_newer.id, v_older.id])
        self.assertEqual(body['summary']['with_follow_up_count'], 1)

        # Filter by Patient (contact_id)
        by_patient = client.get(
            f'/api/v1/workspaces/{ws.slug}/records/visit/?contact_id={p1.id}'
        )
        self.assertEqual([r['id'] for r in by_patient.json()['results']], [v_older.id])

        # Filter by Follow-up (has_follow_up=yes / no)
        with_fu = client.get(
            f'/api/v1/workspaces/{ws.slug}/records/visit/?has_follow_up=yes'
        )
        self.assertEqual([r['id'] for r in with_fu.json()['results']], [v_older.id])
        without_fu = client.get(
            f'/api/v1/workspaces/{ws.slug}/records/visit/?has_follow_up=no'
        )
        self.assertEqual([r['id'] for r in without_fu.json()['results']], [v_newer.id])

        # Filter by Assignee
        by_assignee = client.get(
            f'/api/v1/workspaces/{ws.slug}/records/visit/?assignee_id={staff_a.id}'
        )
        self.assertEqual([r['id'] for r in by_assignee.json()['results']], [v_older.id])

        # Dashboard summary includes Visit count and recent Visits
        dash_res = client.get(f'/api/v1/workspaces/{ws.slug}/dashboard/')
        self.assertEqual(dash_res.status_code, 200)
        dash = dash_res.json()
        self.assertEqual(dash['counts_by_object']['visit'], 2)
        self.assertEqual(dash['open_counts']['visit_with_follow_up'], 1)
        self.assertTrue(any(r['object_key'] == 'visit' for r in dash['recent_records']))

        # Global search finds Visit by reason, follow_up, patient name, and record code
        search_res = client.get(
            f'/api/v1/workspaces/{ws.slug}/search/?q=Nutrition'
        )
        self.assertEqual(search_res.status_code, 200)
        search_keys = {o['key'] for o in search_res.json()['searchable_objects']}
        self.assertIn('visit', search_keys)
        self.assertNotIn('deal', search_keys)
        self.assertEqual(len(search_res.json()['results']), 1)
        self.assertEqual(search_res.json()['results'][0]['object_type'], 'visit')
        self.assertEqual(search_res.json()['results'][0]['inspect_pane_id'], 'visits')

        code_search = client.get(
            f'/api/v1/workspaces/{ws.slug}/search/?q=VIS-{v_older.id}'
        )
        self.assertEqual(code_search.status_code, 200)
        self.assertEqual(code_search.json()['results'][0]['id'], v_older.id)

        # Create + update via API writes audit entries visible in workspace & per-record audit
        created = client.post(
            f'/api/v1/workspaces/{ws.slug}/records/visit/',
            data=json.dumps({
                'contact_id': p1.id,
                'date': '2026-11-20T11:00',
                'reason': 'Audited clinic visit',
                'follow_up': 'Check in next week',
            }),
            content_type='application/json',
        )
        self.assertEqual(created.status_code, 201)
        audited_id = created.json()['record']['id']

        ws_audit = client.get(
            f'/api/v1/workspaces/{ws.slug}/audit/?object_type=visit'
        )
        self.assertEqual(ws_audit.status_code, 200)
        self.assertTrue(
            any(e['object_id'] == str(audited_id) and e['can_inspect'] and e['inspect_pane_id'] == 'visits'
                for e in ws_audit.json()['results'])
        )
        self.assertTrue(
            any(o['key'] == 'visit' for o in ws_audit.json()['available_object_types'])
        )

        rec_audit = client.get(
            f'/api/v1/workspaces/{ws.slug}/records/visit/{audited_id}/audit/'
        )
        self.assertEqual(rec_audit.status_code, 200)
        self.assertGreaterEqual(rec_audit.json()['total_count'], 1)

    def test_visit_s24_role_field_rights_sharing_and_role_deletion_fallback(self):
        ws, owner, _, staff_role, staff_a, staff_b = self._create_workspace_and_users(
            slug='tema-health'
        )
        owner_client = self._logged_in_client(owner)

        # Clone a Nurse role: hide notes & date, freeze follow_up, leave reason editable
        nurse_role = clone_role_from_source(ws, name='Nurse', source_role=staff_role)
        nurse_role.field_rights['visit'] = {
            'date': 'hidden',
            'reason': 'editable',
            'contact': 'editable',
            'assignee': 'editable',
            'notes': 'hidden',
            'follow_up': 'read_only',
        }
        nurse_role.save(update_fields=['field_rights', 'updated_at'])
        staff_a.role = nurse_role
        staff_a.save(update_fields=['role'])

        nurse_client = self._logged_in_client(staff_a)
        desk_client = self._logged_in_client(staff_b)

        patient = Contact.objects.create(workspace=ws, owner=owner, created_by=owner, name='Efua Mensah')

        # Owner creates a workspace-visible Visit and an owner-only Visit and a role-shared Visit
        v_ws_res = owner_client.post(
            f'/api/v1/workspaces/{ws.slug}/records/visit/',
            data=json.dumps({
                'contact_id': patient.id,
                'date': '2026-11-20T08:00',
                'reason': 'General checkup',
                'notes': 'SecretSensitiveNoteXYZ',
                'follow_up': 'Return in 3 weeks',
                'visibility': 'workspace',
            }),
            content_type='application/json',
        )
        self.assertEqual(v_ws_res.status_code, 201)
        v_ws_id = v_ws_res.json()['record']['id']

        v_private_res = owner_client.post(
            f'/api/v1/workspaces/{ws.slug}/records/visit/',
            data=json.dumps({
                'contact_id': patient.id,
                'date': '2026-11-20T09:00',
                'reason': 'Owner-only consultation',
                'visibility': 'owner',
            }),
            content_type='application/json',
        )
        self.assertEqual(v_private_res.status_code, 201)
        v_private_id = v_private_res.json()['record']['id']

        v_role_res = owner_client.post(
            f'/api/v1/workspaces/{ws.slug}/records/visit/',
            data=json.dumps({
                'contact_id': patient.id,
                'date': '2026-11-20T10:00',
                'reason': 'Nurse team handoff',
                'visibility': 'role',
                'shared_role_id': nurse_role.id,
            }),
            content_type='application/json',
        )
        self.assertEqual(v_role_res.status_code, 201)
        v_role_id = v_role_res.json()['record']['id']

        # Nurse sees workspace + Nurse-role visits, NOT owner-only visit
        nurse_list = nurse_client.get(f'/api/v1/workspaces/{ws.slug}/records/visit/')
        self.assertEqual(nurse_list.status_code, 200)
        nurse_ids = {r['id'] for r in nurse_list.json()['results']}
        self.assertEqual(nurse_ids, {v_ws_id, v_role_id})

        # Desk (Staff role) sees only workspace visit, NOT Nurse-role or owner-only visits
        desk_list = desk_client.get(f'/api/v1/workspaces/{ws.slug}/records/visit/')
        self.assertEqual({r['id'] for r in desk_list.json()['results']}, {v_ws_id})

        # Hidden fields (notes, date, visited_at) are stripped for Nurse, but visible to Owner
        nurse_detail = nurse_client.get(
            f'/api/v1/workspaces/{ws.slug}/records/visit/{v_ws_id}/'
        ).json()['record']
        self.assertNotIn('notes', nurse_detail)
        self.assertNotIn('date', nurse_detail)
        self.assertNotIn('visited_at', nurse_detail)
        self.assertEqual(nurse_detail['follow_up'], 'Return in 3 weeks')

        owner_detail = owner_client.get(
            f'/api/v1/workspaces/{ws.slug}/records/visit/{v_ws_id}/'
        ).json()['record']
        self.assertEqual(owner_detail['notes'], 'SecretSensitiveNoteXYZ')
        self.assertIn('date', owner_detail)

        # Nurse cannot match hidden notes via search
        hidden_search = nurse_client.get(
            f'/api/v1/workspaces/{ws.slug}/search/?q=SecretSensitiveNoteXYZ'
        )
        self.assertEqual(hidden_search.json()['total_count'], 0)

        # Nurse cannot modify read-only follow_up (403), but can update editable reason (200)
        ro_patch = nurse_client.patch(
            f'/api/v1/workspaces/{ws.slug}/records/visit/{v_ws_id}/',
            data=json.dumps({'follow_up': 'Tampered'}),
            content_type='application/json',
        )
        self.assertEqual(ro_patch.status_code, 403)

        ok_patch = nurse_client.patch(
            f'/api/v1/workspaces/{ws.slug}/records/visit/{v_ws_id}/',
            data=json.dumps({'reason': 'General checkup updated by nurse'}),
            content_type='application/json',
        )
        self.assertEqual(ok_patch.status_code, 200)

        # Owner audit shows hidden note change, Nurse audit omits hidden notes changes and private visit
        owner_client.patch(
            f'/api/v1/workspaces/{ws.slug}/records/visit/{v_ws_id}/',
            data=json.dumps({'notes': 'UpdatedSecretNote'}),
            content_type='application/json',
        )
        nurse_audit = nurse_client.get(
            f'/api/v1/workspaces/{ws.slug}/audit/?object_type=visit'
        ).json()['results']
        self.assertFalse(any(e['field_name'] == 'notes' for e in nurse_audit))
        self.assertFalse(any(e['object_id'] == str(v_private_id) for e in nurse_audit))

        # Deleting Nurse role reassigns staff_a and falls back role-shared Visit to owner visibility
        staff_a.role = staff_role
        staff_a.save(update_fields=['role'])
        del_role = owner_client.delete(
            f'/api/v1/workspaces/{ws.slug}/roles/{nurse_role.id}/'
        )
        self.assertEqual(del_role.status_code, 200)
        v_role_refreshed = Visit.objects.get(id=v_role_id)
        self.assertEqual(v_role_refreshed.visibility, 'owner')
        self.assertIsNone(v_role_refreshed.shared_role_id)
        self.assertTrue(
            AuditLog.objects.filter(
                workspace=ws,
                object_type='visit',
                object_id=str(v_role_id),
                field_name='visibility',
                new_value='owner',
            ).exists()
        )
