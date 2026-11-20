# S2.5 Search and Audit

Implemented in `/home/demiz/Projects/jojo_crm_venv/jojoCRM` and `/home/demiz/Projects/jojoCRM-frontend`.

## Overview

S2.5 delivers registry-driven, pack- and permission-aware **Global Search** across all implemented workspace CRM objects, unified **server-backed searchable and paginated relation pickers** across CRM panes and `ConvertLeadModal`, **URL deep-linking (`?inspect=<id>`)** with browser refresh and Back/Forward history navigation, **strict S2.4-scoped workspace audit log filtering and pagination**, and **per-record Change History** inside the Record Inspector Drawer.

## Data Model and Migrations

Updated in [`mainapp/models/core.py`](../mainapp/models/core.py):

- **`AuditLog` visibility snapshot & composite index**:
  - Added optional snapshot fields `record_visibility`, `record_owner` (`SET_NULL`), and `record_shared_role` (`SET_NULL`) so deleted CRM records retain their final sharing visibility for strict post-deletion audit access control while remaining compatible with non-CRM and legacy audit entries.
  - Added composite index `audit_ws_type_act_idx` on `(workspace, object_type, action, -created_at)` to accelerate filtered audit queries.
- **Migration**:
  - [`mainapp/migrations/0016_auditlog_visibility_snapshot_and_action_idx.py`](../mainapp/migrations/0016_auditlog_visibility_snapshot_and_action_idx.py)
- **Audit snapshot propagation**:
  - `record_audit_event(...)` in [`mainapp/service/audit_service.py`](../mainapp/service/audit_service.py) accepts `record=` (or explicit snapshot kwargs) and is wired across [`mainapp/service/crm_service.py`](../mainapp/service/crm_service.py), [`mainapp/service/lead_conversion_service.py`](../mainapp/service/lead_conversion_service.py), [`mainapp/service/pipeline_service.py`](../mainapp/service/pipeline_service.py), and [`mainapp/service/role_service.py`](../mainapp/service/role_service.py).

## Backend Services and API Endpoints

### Global Search ([`mainapp/service/search_service.py`](../mainapp/service/search_service.py), [`mainapp/views/search_views.py`](../mainapp/views/search_views.py))
- **Endpoint**: `GET /api/v1/workspaces/<slug>/search/`
- **Query parameters**: `q`, `object_type` (optional), `page` (default `1`), `page_size` (default `20`, max `100`).
- **Registry-driven scope**: Covers all implemented CRM models enabled in `WorkspaceTable` and readable by the caller (`account`, `contact`, `lead`, `deal`, `job`, `appointment`, `ticket`, and `activity`), adapting `contact` labels to `workspace.contact_label` / `contact_plural_label` (e.g. `Patient` on Healthcare).
- **Pack & permission gating**:
  - Strict tenant isolation (`workspace=workspace`).
  - Disabled tables (such as `deal` on Healthcare) are omitted from `searchable_objects` and return `404` when requested explicitly via `object_type`.
  - Unreadable objects are omitted and return `403` on explicit `object_type`.
  - Enforces record sharing visibility (`build_record_visibility_q`), including parent-record visibility for `activity` hits (which expose `inspect_object_type`, `inspect_pane_id`, and `inspect_record_id` pointing to the parent CRM record).
  - Matches caller-visible text fields (including notes/description/summary and visible linked names) plus exact record codes (`ACC-7`, `DL-42`, `ACT-4`, etc.), never matching hidden fields and omitting hidden fields from serialized results.
- **Pagination**: Uses SQL `COUNT(*)` per enabled readable object (`counts_by_object`, `all_objects_total_count`) and SQL `UNION ALL` ordered by `(-updated_at, -id)` with `LIMIT / OFFSET` and batched ORM hydration.

### Scoped Workspace & Per-Record Audit History ([`mainapp/service/audit_service.py`](../mainapp/service/audit_service.py), [`mainapp/views/workspace_views.py`](../mainapp/views/workspace_views.py), [`mainapp/views/crm_views.py`](../mainapp/views/crm_views.py))
- **Endpoints**:
  - `GET /api/v1/workspaces/<slug>/audit/` — supports `page`, `page_size` (default `20`, max `100`), `actor_kind`, `object_type`, `action` (`create`, `update`, `delete`), and `q`. Also returns `available_object_types` adapted to workspace packs and caller read rights, plus per-entry `can_inspect` and `inspect_pane_id` for live readable CRM records.
  - `GET /api/v1/workspaces/<slug>/records/<str:object_key>/<int:record_id>/audit/` — returns paginated audit change history (`page`, `page_size` default `10`, max `50`) for one readable, visible CRM record (including S2.3 Deal stage changes with person, timestamp, previous stage, and new stage).
- **Strict S2.4 enforcement**:
  - Only the permanent Workspace Owner bypasses CRM object/field/sharing restrictions. Non-Owner roles (including roles holding `users_and_roles`) strictly obey their own CRM object read rights, disabled tables, hidden fields, and sharing visibility.
  - Hidden fields are excluded from audit rows; hidden primary label fields (`name`/`title`/`summary`) redact `record_label` and `record` create/delete values to the record code and are excluded from search matching so hidden values cannot be probed via `q`.
  - Live CRM records use live `build_record_visibility_q` (and parent record visibility for `activity` entries); deleted CRM records evaluate their full history against their final visibility snapshot at deletion time.
  - Audit search `q` matches across `actor_label`, visible `record_label`, `field_name`, `object_type`, `object_id`, and cast `previous_value` / `new_value` text.

### Relation Picker Compatibility Filter ([`mainapp/service/crm_service.py`](../mainapp/service/crm_service.py))
- Added `compatible_account_id` query parameter to `list_crm_records(..., 'contact', ...)` matching `Q(account_id=acc_id) | Q(account_id__isnull=True)` so server-backed Contact pickers on Deals, Jobs, and Tickets paginate accurately across Account-linked and standalone Contacts beyond 250 records.

## Frontend Implementation (`jojoCRM-frontend`)

- **API client ([`src/service/crmService.js`](file:///home/demiz/Projects/jojoCRM-frontend/src/service/crmService.js))**: Added `searchWorkspaceRecords`, `fetchWorkspaceAudit`, and `fetchRecordAudit`.
- **URL Deep-Linking ([`src/utils/catalog.js`](file:///home/demiz/Projects/jojoCRM-frontend/src/utils/catalog.js), [`src/App.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/App.jsx), [`src/screens/WorkspaceShell.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/screens/WorkspaceShell.jsx), [`src/hooks/useCrmRecordsPane.js`](file:///home/demiz/Projects/jojoCRM-frontend/src/hooks/useCrmRecordsPane.js))**:
  - Opening/closing a Record Inspector pushes `/{slug}/{pane}[/{subtab}]?inspect=<id>` via `history.pushState` (`replaceState` on record delete), preserving active pane, subtab, and inspected record across refreshes, shareable links, and Back/Forward history navigation.
  - Opening a Deal from Global Search, Audit Log, or Linked Records preserves the current Deals subtab or defaults to `kanban`.
- **Global Search ([`src/components/GlobalSearchBar.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/GlobalSearchBar.jsx), [`src/index.css`](file:///home/demiz/Projects/jojoCRM-frontend/src/index.css))**:
  - Centered in `.floating-topbar` with `Ctrl/Cmd+K` shortcut, keyboard navigation, frosted portaled popover (`#globalSearchResults`), pack- and permission-aware object filter pills (Healthcare never offers `Deal`), and paginated results opening directly into the target record inspector.
- **Unified Relation Picker ([`src/components/crm/RecordRelationPicker.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/crm/RecordRelationPicker.jsx))**:
  - Server-backed debounced search, pagination (`Prev` / `Next`), clear truncation notices (`Showing X-Y of N ... Search or paginate to view more`), optional pinned Suggested Matches group, and persistent selected-record labels beyond page 1 / 250 entries.
  - Wired across [`ContactsPane.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/screens/ContactsPane.jsx), [`DealsPane.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/screens/DealsPane.jsx), [`JobsPane.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/screens/JobsPane.jsx), [`AppointmentsPane.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/screens/AppointmentsPane.jsx), [`TicketsPane.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/screens/TicketsPane.jsx), and [`ConvertLeadModal.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/crm/ConvertLeadModal.jsx) (Lead, reuse Account, and reuse Contact pickers).
- **Workspace & Per-Record Audit UI ([`src/components/AuditLogDrawer.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/AuditLogDrawer.jsx), [`src/components/crm/RecordAuditHistory.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/crm/RecordAuditHistory.jsx), [`src/components/crm/RecordInspectorDrawer.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/crm/RecordInspectorDrawer.jsx))**:
  - Workspace Audit Log drawer filters by Actor kind, Object type, Action (`create`, `update`, `delete`), and Search query with Clear filters, pagination, live `permissionsRevision` refresh, and Inspect links to live CRM records.
  - Record Inspector drawer displays a paginated **Change History** section below `ActivityTimeline`.

## Automated Tests

- Backend tests live in [`mainapp/tests/test_search_and_audit.py`](../mainapp/tests/test_search_and_audit.py), covering global search across enabled objects, Activity parent navigation, exact record codes, cross-workspace isolation, Healthcare excluding `Deal`, S2.4 object/field/sharing enforcement in search, SQL `UNION ALL` pagination, relation picker pagination beyond 250 records with `compatible_account_id`, Deal stage-change audit entries in per-record and filtered workspace audit endpoints, audit multi-page pagination, and strict S2.4 audit enforcement on unreadable objects, hidden fields, redacted labels, live private/role records, deleted private vs. public records, and non-Owner roles with `users_and_roles=True`.
