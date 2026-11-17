# S2.1 Core and Pack Objects

Implemented in `/home/demiz/Projects/jojo_crm_venv/jojoCRM` and `/home/demiz/Projects/jojoCRM-frontend`.

## Overview

S2.1 delivers persistent data models, workspace-scoped CRUD APIs, role CRUD enforcement, pack/table gating, audit logging, and workspace CRM UI panes for the eight Sprint 2 starter objects:

- **Core objects**: `Account`, `Contact` (rendered with workspace-specific labels such as `Patient` on Healthcare or `Student` on Training), and `Activity` (`note`, `task`, `call`, `meeting`).
- **Pack objects**: `Lead`, `Deal`, `Job`, `Appointment`, and `Ticket`.

Out-of-scope Sprint 2 stories remain intentionally separate: Lead conversion (`S2.2`), editable pipeline stage management and Kanban drag-and-drop (`S2.3`), role cloning / field permissions / record sharing (`S2.4`), global search (`S2.5`), and Healthcare `Visit` (`S2.6`).

## Data Model and Migrations

`mainapp/models.py` was refactored into the `mainapp/models/` package (re-exporting all models from `mainapp/models/__init__.py`):

- [`mainapp/models/core.py`](../mainapp/models/core.py): Workspace, Role, User, WorkspaceInvite, WorkspacePack, WorkspaceTable, Auth/Session, and AuditLog models.
- [`mainapp/models/crm.py`](../mainapp/models/crm.py): Abstract `WorkspaceRecordBase` plus `Account`, `Contact`, `Lead`, `Deal`, `Job`, `Appointment`, `Ticket`, and `Activity`.

Key domain rules enforced across service validation and database constraints:

- **Workspace isolation**: Every CRM record and attached `Activity` belongs to a single `Workspace`. Cross-workspace queries and cross-workspace foreign key references return `404` / `400`.
- **Contact labels & account linking**: `Contact` can sit on an `Account` (`account` nullable) or remain standalone. `Lead` remains separate until `S2.2` conversion.
- **Linked record rules**:
  - `Job` requires at least an `Account` or a `Contact` (or both), plus `status`, optional `assignee`, optional `due_date`, and `notes`.
  - `Appointment` requires a `Contact`, `scheduled_at` (parsed in caller timezone when naive and stored in UTC), `status`, optional `assignee`, and `notes`.
  - On `Deal`, `Job`, and `Ticket`, specifying `contact_id` without `account_id` automatically links the contact's `Account` if present; passing a mismatched `account_id` and `contact.account_id` raises `400`.
- **Referential deletion guard**: Deleting an `Account` or `Contact` returns `409 Conflict` if cascading/clearing the link would orphan an `Appointment` without a `Contact` or a `Job` without any `Account` or `Contact`.
- **Activities**: Sprint 2 activity creation/updates accept `note`, `task`, `call`, and `meeting` attached to any enabled CRM object target inside the same workspace (`target_object_type`, `target_object_id`).
- **Migrations**:
  - `0009_account_activity_contact_appointment_deal_job_lead_and_more.py`: Creates all eight CRM tables and indexes.
  - `0010_backfill_role_object_rights.py`: Backfills `Role.permissions_json['object_rights']` on existing roles (`Staff` defaults to `create=True`, `read=True`, `update=True`, `delete=False`; `Owner` always resolves to full CRUD).

All eight CRM object keys are registered in [`mainapp/service/catalog_service.py`](../mainapp/service/catalog_service.py) (`OBJECT_MODEL_NAMES`) so `Settings -> Packs` shows live row counts (batched in `get_workspace_table_record_counts()` via a single annotated `Workspace` query) and blocks disabling non-empty pack tables (`409`).

## Role Object Rights & Pack Gating

- [`mainapp/service/role_service.py`](../mainapp/service/role_service.py) defines `build_default_object_rights`, `normalize_role_object_rights`, and `user_has_object_right(user, workspace, object_key, action)`.
- **Owner** always holds full `create`, `read`, `update`, and `delete` rights across all objects.
- **Staff** and future cloned roles evaluate their stored `object_rights[object_key][action]`.
- **Table & Pack Gating**: Requests against disabled tables (for example `Deal` on Food and Beverage, which does not include the `Deal` table) return `404` and hide the table from sidebar navigation.
- Every CRM create, update, and delete records field-level changes to `AuditLog` via [`mainapp/service/audit_service.py`](../mainapp/service/audit_service.py).

## API Endpoints

Registered in [`mainapp/urls.py`](../mainapp/urls.py):

- `GET/PATCH /api/v1/workspaces/<slug>/roles/<role_id>/` — View or update `object_rights` on non-Owner roles (requires Owner or `users_and_roles` switch; blocks Owner modification with `403`).
- `GET /api/v1/workspaces/<slug>/dashboard/` — Returns permission- and table-filtered dashboard aggregates (`counts_by_object`, `open_counts`, `deal_summary`, `recent_records`, `total_records`).
- `GET /api/v1/workspaces/<slug>/crm/options/` — Returns active workspace assignees, readable Accounts and Contacts dropdown summaries (250-item slice plus `accounts_total_count`, `accounts_truncated`, `contacts_total_count`, `contacts_truncated`), and starter choice constants (`deal_stages`, `job_statuses`, `appointment_statuses`, `ticket_statuses`, `ticket_priorities`, `activity_types`).
- `GET/POST /api/v1/workspaces/<slug>/records/<object_key>/` — Search, filter, paginate, inspect summary counts (including `city_counts` on `account`, `priority_counts` on `ticket`, and stage totals on `deal`), and create records for `account`, `contact`, `lead`, `deal`, `activity`, `job`, `appointment`, and `ticket`.
- `GET/PATCH/DELETE /api/v1/workspaces/<slug>/records/<object_key>/<record_id>/` — Retrieve full detail (including up to 100 attached activities, `activities_total_count`, `has_more_activities`, and cross-object linked records), edit, or delete a record.
- `GET/POST /api/v1/workspaces/<slug>/records/<object_key>/<record_id>/activities/` — List or create a `note`, `task`, `call`, or `meeting` attached directly to a CRM record.

## Frontend Screens & Components

Implemented in `/home/demiz/Projects/jojoCRM-frontend`:

- **Service & Hook Layer**:
  - `src/utils/api.js` & `src/service/crmService.js` — Request helpers with in-flight deduplication for `fetchAuthSession` and `fetchBusinessTypesCatalog` (plus in-memory caching for static business types), plus `fetchDashboardSummary` and typed CRUD, options, activities, and role permission helpers.
  - `src/hooks/useCrmRecordsPane.js` — Shared search, filters, pagination (clamping out-of-range pages and stepping back when deleting the last item on a page `> 1`), record inspector loading, save/delete, and deep-link inspection (`initialInspectRequest` + `onInspectRequestConsumed`).
  - `src/utils/formatters.js` & `src/utils/crmFormOptions.js` — Tabular GHS currency formatting, timezone-aware date/time formatting, and Contact-to-Account link synchronization.
- **Reusable CRM Components (`src/components/crm/`)**:
  - `CrmListPage.jsx` — Shared toolbar, stat cards, `statsAddon`, `extraContent`, `keepToolbarWhenTableHidden` for Board views, search/filter row, responsive data table, out-of-range page recovery, read-restriction banner, and create button gating.
  - `SummaryMiniCharts.jsx` — Shared `SummaryChartCard`, `HorizontalBarChart`, and `MiniDonutChart` mini visualizations for CRM list headers and dashboard panels.
  - `PipelineStageDistribution.jsx` — Proportional GHS pipeline value bar and stage breakdown legend for `DealsPane`.
  - `PaginationBar.jsx` — Shared table pagination bar.
  - `RecordInspectorDrawer.jsx` — Frosted side drawer with read-only indicator when `update` permission is missing, delete confirmation modal when `delete` permission is granted, linked record navigation, workspace `contactPluralLabel` forwarding, and embedded `ActivityTimeline`.
  - `RecordLinksSection.jsx` — Displays connected parent/child records with dynamic `contactPluralLabel` headings and navigates directly to linked panes/records.
  - `ActivityTimeline.jsx` — Inline quick-composer and inline edit/delete controls for `note`, `task`, `call`, and `meeting` items, including workspace `assignee` selection and 100-item overflow notices (`activitiesTotalCount` / `hasMoreActivities`).
- **Workspace Panes (`src/screens/`)**:
  - `DashboardPane.jsx` — Wired live workspace dashboard rendering KPI stats, Pipeline Stage Mix donut, clickable record counts, Recent CRM Records, and Core/Active table quick links.
  - `AccountsPane.jsx` — Includes `Companies by city` horizontal bar chart.
  - `ContactsPane.jsx` — Adapts dynamically to `workspace.contact_label` / `workspace.contact_plural_label` (e.g., `Patient` / `Patients`) and includes `SMS consent` donut chart.
  - `LeadsPane.jsx` — Includes `Open and converted` donut chart.
  - `DealsPane.jsx` — Includes Table CRUD view, read-only Stage-grouped Board subtab preview (keeping search/stage filter and pagination visible), and `PipelineStageDistribution` (stage customization and drag-and-drop remain deferred to S2.3).
  - `JobsPane.jsx`
  - `AppointmentsPane.jsx`
  - `TicketsPane.jsx` — Uses the `"Support Tickets"` header and includes `Priority` horizontal bar chart.
- **Settings, Controls & Audit**:
  - `src/components/SettingsRolesSection.jsx` — Interactive Object Rights CRUD matrix (`Create`, `Read`, `Update`, `Delete`) per object for non-Owner roles, checking `workspace.enabled_object_keys` for live active status and refreshing active session permissions immediately after saving.
  - `src/components/AuditLogDrawer.jsx` — Maps `account`, `contact`, `lead`, `deal`, `activity`, `job`, `appointment`, and `ticket` object types to readable labels using workspace `contactLabel`.
  - `src/index.css` — Renders `.custom-select-portal` on `--bg-modal` so portal dropdown menus remain legible in both light and dark modes.

## Automated Tests

Backend coverage lives in [`mainapp/tests/test_core_and_pack_objects.py`](../mainapp/tests/test_core_and_pack_objects.py), covering:

- Full CRUD across all eight objects and all four activity types (`note`, `task`, `call`, `meeting`) on a General services workspace, plus `Job` and `Appointment` link persistence and deletion guards (`409`).
- Dashboard aggregate endpoint (`/dashboard/`) gating by enabled tables and role permissions, `city_counts` / `priority_counts` list summaries, `activities_total_count` / `has_more_activities`, and `/crm/options/` truncation metadata (`test_dashboard_summary_and_chart_summaries_and_metadata`).
- Role permission enforcement (`PATCH` on Staff `object_rights` blocking/unblocking `update`/`delete`/`create`/`read`, protecting Owner from restriction with `403`, and verifying Activity update/delete rights).
- Cross-workspace isolation across list, detail, update, delete, foreign-key linking, and activity attachment.
- Food & Beverage pack gating (`404` on `deal` API endpoints; Healthcare `Patient` + `Appointment` support).
- `CatalogService` empty-table protection (`409` when disabling populated `job`/`deal` tables vs allowing empty tables) and Sprint 1 regression suite continuity.
