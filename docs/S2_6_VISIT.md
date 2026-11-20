# S2.6 Visit

Implemented in `/home/demiz/Projects/jojo_crm_venv/jojoCRM` and `/home/demiz/Projects/jojoCRM-frontend`.

## Overview

S2.6 implements the **Visit** starter catalog object for **Healthcare and wellness** workspaces (and any workspace where the `visit` table is enabled) while keeping Visit excluded from **General services** workspaces unless explicitly turned on. A Visit records a timezone-aware **date**, **reason**, **notes**, and **follow-up** linked to a **Patient** (`Contact`), plus an optional **assignee** aligned with S2.4 field permissions. Visits intentionally exclude clinical EHR fields—there is no diagnosis code, chart, lab result, or prescription field in the database, API, or UI, and clinical/EHR keys sent to the Visit API are explicitly refused with `400 Bad Request`.

## Data Model and Migrations

Added in [`mainapp/models/crm.py`](../mainapp/models/crm.py) and exported from [`mainapp/models/__init__.py`](../mainapp/models/__init__.py) and [`mainapp/admin.py`](../mainapp/admin.py):

- **`Visit(WorkspaceRecordBase)`** (`OBJECT_KEY = 'visit'`):
  - `contact` (`ForeignKey(Contact, null=True, blank=True, on_delete=SET_NULL, related_name='visits')`) — required at the service layer for every Visit.
  - `date` (`DateTimeField()`) — timezone-aware visit timestamp stored in UTC and parsed/displayed in the caller's timezone.
  - `reason` (`CharField(max_length=255)`) — required visit reason and primary record label (`VIS-<id>` fallback when hidden).
  - `notes` (`TextField(blank=True, default='')`) — optional multi-line visit notes.
  - `follow_up` (`TextField(blank=True, default='')`) — optional free-text follow-up plan, instruction, or target date.
  - `assignee` (`ForeignKey(User, null=True, blank=True, on_delete=SET_NULL, related_name='assigned_visits')`) — optional staff assignee aligned with S2.4 `OBJECT_FIELD_SCHEMAS['visit']`.
  - Ordering: `['-date', '-id']`; indexes on `(workspace, -date)` (`vis_ws_date_idx`) and `(workspace, contact)` (`vis_ws_contact_idx`).
- **`Activity.VALID_TARGET_OBJECTS`**: Extended with `'visit'` so `note`, `task`, `call`, and `meeting` activities attach to Visit records and clean up when a Visit is deleted.
- **Migration**: [`mainapp/migrations/0017_visit.py`](../mainapp/migrations/0017_visit.py).

## Backend Services and API Endpoints

### Visit CRUD & Domain Rules ([`mainapp/service/crm_service.py`](../mainapp/service/crm_service.py))
- **Endpoints** (served via existing S2.1 generic CRM views in [`mainapp/views/crm_views.py`](../mainapp/views/crm_views.py)):
  - `GET /api/v1/workspaces/<slug>/records/visit/` (or `/records/visits/`) — paginated list sorted by `-date, -id`, supporting `q` (visible `reason`, `notes`, `follow_up`, `contact__name`, `assignee__email`), `contact_id`, `assignee_id`, `has_follow_up` (`yes`/`no`, `true`/`false`), `visibility`, and `shared_role_id`. Returns `summary.with_follow_up_count` when `follow_up` is visible.
  - `POST /api/v1/workspaces/<slug>/records/visit/` — creates a Visit linked to a visible Patient (`Contact`) in the same workspace. Requires `contact_id`, `date` (or `visited_at` alias), and `reason`; `notes`, `follow_up`, and `assignee_id` are optional.
  - `GET/PATCH/DELETE /api/v1/workspaces/<slug>/records/visit/<id>/` — retrieves detail (including `linked_records.contact` and activities), updates fields with per-field audit logging, or deletes the Visit with activity cleanup and deletion audit snapshot.
  - `GET/POST /api/v1/workspaces/<slug>/records/visit/<id>/activities/` and `/audit/` — attaches activities and returns per-record S2.4-scoped audit history.
- **Pack gating**: General services workspaces do not include `visit` and return `404` on Visit endpoints, while Healthcare and wellness enables `visit` automatically.
- **Clinical/EHR boundary guardrail**: Payloads containing clinical/EHR keys (`diagnosis`, `diagnosis_code`, `icd_code`, `chart`, `medical_chart`, `lab`, `labs`, `lab_result`, `lab_results`, `prescription`, `prescriptions`, `medication`, `medications`) are explicitly refused with `400 Bad Request`.
- **Relational & table guards**:
  - Deleting a `Contact`/`Patient` with linked Visits returns `409 Conflict` using `workspace.contact_label`.
  - [`mainapp/service/catalog_service.py`](../mainapp/service/catalog_service.py) registers `'visit': 'Visit'` in `OBJECT_MODEL_NAMES`, so disabling `visit` in `Settings -> Packs` is refused (`409`) once Visit records exist.
- **Linked records**: Opening a Patient (`Contact`) includes `linked_records.visits` (ordered by `-date, -id`) when `visit` is enabled and readable; opening a Visit includes `linked_records.contact`.
- **Dashboard summary**: `get_workspace_dashboard_summary` includes `visit` counts, `open_counts.visit_with_follow_up`, and recent Visit records.

### Roles, Sharing, Search, and Audit
- **Roles & Sharing ([`mainapp/service/role_service.py`](../mainapp/service/role_service.py))**:
  - Updated `OBJECT_FIELD_SCHEMAS['visit']` so hiding `date` strips both `date` and `visited_at`.
  - Registered `'visit': 'reason'` in `PRIMARY_LABEL_FIELD_BY_OBJECT` and `('visit', Visit)` in `_SHARED_RECORD_MODELS` (including role-delete fallback to `owner` visibility).
- **Global Search ([`mainapp/service/search_service.py`](../mainapp/service/search_service.py))**:
  - Registered `SearchObjectSpec('visit', 'visits', 'VIS')`, matching visible `reason`, `notes`, `follow_up`, `assignee__email`, visible `contact__name`, and exact `VIS-<id>` codes while respecting S2.4 field rights and sharing visibility. Absent on General services.
- **Audit History ([`mainapp/service/audit_service.py`](../mainapp/service/audit_service.py))**:
  - Added `'visit'` to `INSPECTABLE_CRM_PANE_BY_KEY`, `RECORD_CODE_PREFIX_BY_KEY` (`VIS`), `AUDIT_OBJECT_TYPE_ALIASES`, `_get_crm_models_by_key`, live visibility scoping, and `available_object_types`.

## Frontend Implementation (`jojoCRM-frontend`)

- **Visits screen ([`src/screens/VisitsPane.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/screens/VisitsPane.jsx))**:
  - Uses shared `CrmListPage`, `Modal`, `RecordInspectorDrawer`, `RecordRelationPicker`, `RecordSharingControl`, `SharingBadge`, `FieldLabel`, `Select`, `Button`, `FeedbackNotice`, and `useCrmRecordsPane`.
  - Toolbar filters for **Patient** (`RecordRelationPicker`), **Follow-up** (`All follow-ups` / `With follow-up` / `Without follow-up`), **Assignee**, and **Sharing**, sorted newest Visit date first.
  - Create modal and Record Inspector expose **Patient** (adapting to `workspace.contact_label`), **Visit date** (timezone-aware `datetime-local`), **Reason**, **Notes**, **Follow-up**, optional **Assignee**, and **Sharing**, with zero diagnosis code, chart, lab result, or prescription fields.
  - Enforces S2.4 CRUD and field rights (`hidden`, `read_only`, `editable`).
- **Healthcare workflow shortcuts**:
  - [`src/components/crm/RecordInspectorDrawer.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/crm/RecordInspectorDrawer.jsx) adds a reusable `headerActions` slot.
  - [`src/screens/ContactsPane.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/screens/ContactsPane.jsx) and [`src/screens/AppointmentsPane.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/screens/AppointmentsPane.jsx) show a **Record Visit** button when `visit` is enabled and creatable, navigating to `VisitsPane` with the create modal pre-filled with that Patient (and appointment timestamp/assignee when launched from an Appointment).
- **Workspace wiring**:
  - [`src/screens/WorkspaceShell.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/screens/WorkspaceShell.jsx) lazy-loads and mounts `VisitsPane` for the `visits` route.
  - [`src/utils/catalog.js`](file:///home/demiz/Projects/jojoCRM-frontend/src/utils/catalog.js) registers `visit` / `visits` in `INSPECTABLE_OBJECT_PANES` for `?inspect=` deep-linking.
  - [`src/components/crm/RecordLinksSection.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/crm/RecordLinksSection.jsx) displays linked Visits on Patient records.
  - [`src/components/GlobalSearchBar.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/GlobalSearchBar.jsx), [`src/components/AuditLogDrawer.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/AuditLogDrawer.jsx), and [`src/screens/DashboardPane.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/screens/DashboardPane.jsx) include Visit entries when enabled.

## Automated Tests

- Backend test suite: [`mainapp/tests/test_visit.py`](../mainapp/tests/test_visit.py), covering Healthcare Visit CRUD, timezone-aware dates and `visited_at` alias, absence and `400` refusal of clinical/EHR fields, General services exclusion, Patient delete guard (`409`), table disable protection (`409`), Visit activities, linked Patient/Visit records, list filters (`contact_id`, `has_follow_up`, `assignee_id`, `visibility`, `q`), S2.4 role/field/sharing enforcement and role-deletion fallback, and S2.5 global search and audit integration.
