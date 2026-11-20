# S2.4 Roles and Sharing

Implemented in `/home/demiz/Projects/jojo_crm_venv/jojoCRM` and `/home/demiz/Projects/jojoCRM-frontend`.

## Overview

S2.4 delivers full role cloning from **Staff**, object-level CRUD rights, field-level permissions (`hidden`, `read_only`, `editable`) across core and pack objects (including `Job`, `Appointment`, and `Visit`), all seven capability switches, role assignment on workspace invites and existing non-Owner members, immediate live permission synchronization across open client sessions, three-tier record sharing visibility (`owner`, `role`, `workspace`), initial password setup for passwordless verified accounts, and passkey label renaming in Settings.

## Data Model and Migrations

- **Invite Role Link ([`mainapp/models/core.py`](../mainapp/models/core.py))**:
  - Added `WorkspaceInvite.role` (`ForeignKey` to `mainapp.Role`, `null=True`, `blank=True`, `on_delete=SET_NULL`, `related_name='invites'`).
- **Existing Role & Record Fields**:
  - `Role` stores `object_rights`, `field_rights`, and the seven switches (`allow_import`, `allow_export`, `automations`, `scripts`, `users_and_roles`, `theme_and_layouts`, `unattended_agent`).
  - `WorkspaceRecordBase` stores `owner`, `visibility` (`workspace`, `role`, `owner`), and `shared_role`.
- **Migration**:
  - [`mainapp/migrations/0015_workspaceinvite_role.py`](../mainapp/migrations/0015_workspaceinvite_role.py): Adds `role` to `WorkspaceInvite`.

## Permissions, Sharing & Account Security (Backend)

- **Role & Field Rights Service ([`mainapp/service/role_service.py`](../mainapp/service/role_service.py))**:
  - `OBJECT_FIELD_SCHEMAS` defines configurable fields for `account`, `contact`, `activity`, `lead`, `deal`, `job`, `appointment`, `ticket`, and `visit`.
  - `clone_role_from_source` clones `Staff` (or another non-Owner role), deep-copying `object_rights`, `field_rights`, and all seven switches while keeping `is_owner=False`, `is_default_staff=False`, `can_manage_seat_plan=False`, and `can_manage_sms_wallet=False`.
  - `normalize_role_field_rights`, `get_user_field_rights`, and `get_field_right` resolve per-field `hidden` / `read_only` / `editable` access (always `editable` for the workspace Owner).
  - `build_record_visibility_q`, `user_can_see_record`, and `user_can_manage_record_sharing` enforce sharing:
    - Workspace **Owner** always sees every workspace record and every hidden field.
    - Non-Owner members see records where `visibility='workspace'`, `owner=user`, or `visibility='role'` with `shared_role=user.role`.
    - Only the record's `owner`, the workspace `Owner`, or a role with `users_and_roles` can alter an existing record's `visibility` or `shared_role`.
  - `compute_user_permissions_version` derives a live token from `(is_owner, role_id, role.updated_at, session_version)`.
  - `fallback_role_shared_records_to_owner` switches any CRM records shared with a deleted custom role back to `visibility='owner'` and records audit entries.
- **Live Session Sync Header ([`mainapp/middleware.py`](../mainapp/middleware.py))**:
  - `ActiveSessionEnforcementMiddleware` attaches `X-Permissions-Version` on authenticated responses so active browser tabs detect role reassignments and permission changes immediately without logout/login.
- **Roles, Members & Invites Endpoints ([`mainapp/views/workspace_access_views.py`](../mainapp/views/workspace_access_views.py), [`mainapp/views/invite_views.py`](../mainapp/views/invite_views.py))**:
  - `GET / POST /api/v1/workspaces/<slug>/roles/` — List roles with `field_schemas` and `shared_record_count`, or clone `Staff` into a new custom role.
  - `GET / PATCH / DELETE /api/v1/workspaces/<slug>/roles/<role_id>/` — Update `name`, `description`, `switches`, `object_rights`, and `field_rights` on non-Owner roles (blocking Owner edits with `403`, blocking non-Owner managers from removing `users_and_roles` on their own active role with `400`), or delete unused custom roles (blocking if members or pending invites remain with `409`, and switching shared records to `owner` visibility).
  - `PATCH / DELETE /api/v1/workspaces/<slug>/members/<member_id>/` — Reassign a non-Owner member to another non-Owner role in Settings (blocking Owner reassignment with `403` and non-Owner self-reassignment with `400`, with audit entries).
  - `GET /api/v1/workspaces/<slug>/people/` & `POST /api/v1/workspaces/<slug>/invites/` & `GET /api/v1/invites/preview/` & `POST /api/v1/invites/accept/` — Expose `assignable_roles`, allow selecting any non-Owner role on an invite (defaulting to `Staff`, refusing Owner with `403`), preview the invited role, and assign that role upon acceptance.
- **CRM Field Rights & Sharing Enforcement ([`mainapp/service/crm_service.py`](../mainapp/service/crm_service.py), [`mainapp/service/lead_conversion_service.py`](../mainapp/service/lead_conversion_service.py), [`mainapp/views/crm_views.py`](../mainapp/views/crm_views.py))**:
  - Enforces `build_record_visibility_q` across list, detail, update, delete, options, dashboard summary, Kanban board, linked records, activities, and lead conversion.
  - Strips `hidden` fields from serialized list, detail, board, options, and linked-record payloads; falls back `display_label` to `record_code` when a primary label field (`name`, `title`, `summary`) is hidden; excludes hidden fields from `q` search, field filters, and summary aggregates; rejects `POST` / `PATCH` writes to `hidden` or `read_only` fields with `403`.
- **Settings Account Security ([`mainapp/views/account_views.py`](../mainapp/views/account_views.py), [`mainapp/views/passkey_views.py`](../mainapp/views/passkey_views.py), [`mainapp/urls.py`](../mainapp/urls.py))**:
  - `POST /api/v1/account/password/set/` — Lets a signed-in verified user without a password set an initial password while remaining signed in (`400` if a password is already set), retiring unused reset tokens, invalidating other sessions, and writing an audit log.
  - `PATCH /api/v1/auth/passkey/<passkey_id>/` — Renames a registered passkey label for the signed-in user and writes an audit log.

## Frontend Implementation (`jojoCRM-frontend`)

- **Live Permission Sync ([`src/utils/api.js`](file:///home/demiz/Projects/jojoCRM-frontend/src/utils/api.js), [`src/App.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/App.jsx), [`src/screens/WorkspaceShell.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/screens/WorkspaceShell.jsx), [`src/hooks/useCrmRecordsPane.js`](file:///home/demiz/Projects/jojoCRM-frontend/src/hooks/useCrmRecordsPane.js))**:
  - Subscribes to `X-Permissions-Version` headers on API responses, plus `focus` / `visibilitychange` checks and a 12-second visible-tab sync, updating `permissions`, incrementing `permissionsRevision` to reload open CRM panes/inspectors, and showing a rounded `brand-soft` toast when permissions change.
- **Settings UI ([`src/components/SettingsRolesSection.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/SettingsRolesSection.jsx), [`src/components/SettingsPeopleSection.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/SettingsPeopleSection.jsx), [`src/screens/SettingsPane.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/screens/SettingsPane.jsx), [`src/screens/AcceptInviteScreen.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/screens/AcceptInviteScreen.jsx))**:
  - **Roles**: **Clone from Staff** modal, interactive 7 capability switches, Object Rights CRUD table (with active pack objects including `Job`, `Appointment`, and `Visit`), per-object Field Rights selector (`Editable`, `Read-only`, `Hidden`), and **Delete role** confirmation modal warning when shared CRM records will switch to `Owner only`.
  - **People**: Role `<Select>` on invite creation, Role column in Invites table, and inline Role `<Select>` reassignment on non-Owner members.
  - **Security**: Initial **Set password** form when `!has_password` and inline **Rename** flow for registered passkeys.
- **CRM Field Rights & Sharing UI ([`src/components/crm/RecordSharingControl.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/crm/RecordSharingControl.jsx), [`src/components/crm/FieldLabel.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/crm/FieldLabel.jsx), [`src/components/crm/RecordInspectorDrawer.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/crm/RecordInspectorDrawer.jsx), [`src/components/crm/DealKanbanBoard.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/crm/DealKanbanBoard.jsx), [`src/components/crm/ActivityTimeline.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/crm/ActivityTimeline.jsx), and all CRM panes)**:
  - Sharing visibility selector (`Whole workspace`, `One role`, `Owner only`), list Sharing badges and filters, and automatic omission / read-only locking of restricted fields across tables, Kanban cards, create modals, and inspector drawers.

## Automated Tests

Backend test coverage in [`mainapp/tests/test_roles_and_sharing.py`](../mainapp/tests/test_roles_and_sharing.py):

- Cloning `Staff` copies object rights, field rights, and switches; rejects duplicate CI names and Owner role mutations; edits switches and field rights (including `Job`, `Appointment`, and `Visit`); writes audit logs.
- Enforces `hidden`, `read_only`, and `editable` fields (`403` on writing hidden/read-only fields, `display_label` fallback to `record_code` when primary label is hidden, Owner still sees hidden fields).
- Invite with a cloned role, invite preview, acceptance into that role, member role reassignment in Settings, and live `X-Permissions-Version` rotation with immediate API enforcement.
- Record sharing across `owner`, `role`, and `workspace` visibility and sharing-change authorization.
- Cloned role deletion switching shared records to `owner` visibility and self-lockout protection for non-Owner managers.
- Initial password setup for passwordless verified accounts and passkey renaming in Settings.
