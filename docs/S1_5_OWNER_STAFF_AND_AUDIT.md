# S1.5 — Owner, Staff, and Audit

Story S1.5 completes Sprint 1 tenancy, identity governance, self-service account security settings, and workspace audit logging across `jojoCRM` (Django backend) and `jojoCRM-frontend` (React + Vite UI).

---

## 1. Data Model & Migrations

Added in `mainapp/models.py` and applied via `0005_user_session_version_role_user_role_auditlog_and_more.py` and `0006_backfill_workspace_roles.py`:

### `Role`
Workspace-scoped role definitions anchored to `Workspace`:
- **Identity & Protection**:
  - `is_owner`: Unique partial constraint (`uniq_owner_role_per_workspace`) ensuring at most one `Owner` role per workspace. `Owner` cannot be deactivated or removed.
  - `is_default_staff`: Unique partial constraint (`uniq_staff_role_per_workspace`) ensuring at most one default `Staff` template role per workspace (ready for cloning in S2.4).
  - Case-insensitive name uniqueness per workspace (`uniq_role_workspace_ci_name`).
- **Owner-Exclusive Billing & Wallet Authorities** (enforced at DB level via `CheckConstraint` so only `is_owner=True` roles may hold them):
  - `can_manage_seat_plan`
  - `can_manage_sms_wallet`
- **Seven Stored PRD Capability Switches** (serialized cleanly under `switches`):
  - `import` (`allow_import`), `export` (`allow_export`), `automations`, `scripts`, `users_and_roles`, `theme_and_layouts`, `unattended_agent`.

### `User` Extensions
- `role`: Nullable `ForeignKey(Role, on_delete=models.SET_NULL, related_name='users')` so future workspace deletion does not hit `ProtectedError`; role deletion protections for `Owner` and default `Staff` live in the service and API layers.
- `session_version`: `PositiveIntegerField(default=1)` incremented whenever a member's active sessions must be terminated immediately.

### `AuditLog`
Immutable change log table indexed by `(workspace, -created_at)` and `(workspace, object_type, -created_at)`:
- `workspace`, `actor_kind` (`person`, `script`, `automation`, `agent`), `actor_user` (`SET_NULL`), `actor_label`, `object_type`, `object_id`, `record_label`, `action` (`create`, `update`, `delete`), `field_name`, `previous_value` (JSON), `new_value` (JSON), `created_at`.
- Sprint 1 proves the `person` actor flow across workspace, verification, invite, OAuth, passkey, profile, password, and deactivation lifecycle mutations, while storing and filtering the full `actor_kind` taxonomy (`person`, `script`, `automation`, `agent`) for Sprint 3+.

---

## 2. Backend Services & Session Enforcement

- **`mainapp/service/role_service.py`**:
  - `ensure_workspace_default_roles(workspace)`: Idempotently ensures permanent `Owner` and default `Staff` roles exist for a workspace and repairs `role=None` legacy members.
  - `is_workspace_owner(user, workspace)`: Checks `workspace.created_by_id` or `user.role.is_owner`.
  - `user_can_manage_users_and_roles(user, workspace)`: Grants member & invite management only to an active verified Owner or a role holding `users_and_roles=True`.
  - `count_active_seats(workspace)`: Counts verified, active users (`is_active=True, email_verified=True`). Deactivated members are excluded immediately and re-included upon reactivation.
- **`mainapp/service/session_service.py` & `mainapp/signals.py`**:
  - `sync_session_version(request, user)`: Connected to Django's `user_logged_in` signal so every newly authenticated session binds the user's current `session_version`.
  - `invalidate_user_sessions(user, *, exclude_session_key=None)`: Increments `user.session_version` atomically, purges expired `django_session` rows, and deletes active `django_session` rows matching `_auth_user_id` (bounded at `MAX_SESSION_SCAN = 200` ordered by `-expire_date`), optionally preserving `exclude_session_key`.
- **`mainapp/middleware.py` (`ActiveSessionEnforcementMiddleware`)**:
  - Installed right after `AuthenticationMiddleware` in `jojoCRM/settings.py`. Even if a cached or pre-existing session cookie presents an inactive user (`not user.is_active`), stale `session_version`, or missing `SESSION_VERSION_KEY` (defaulting to version `1`), Django immediately calls `logout(request)` before reaching any view.
- **`mainapp/service/audit_service.py`**:
  - `record_audit_event(...)` and `serialize_audit_entry(...)`.

---

## 3. API Endpoints (`mainapp/urls.py` under `/api/v1/`)

### Session Recovery & Account Self-Service
- `GET /api/v1/auth/session/` — Returns `{ "authenticated": false }` when signed out or deactivated; returns `verification_required: true` with `email` and `workspace` for unverified sessions; and returns `workspace`, `user`, `active_seat_count`, `role`, `permissions`, and `mailbox_connection` when authenticated and verified.
- `POST /api/v1/signup/` (`signup_view`) — Rejects unverified authenticated sessions with HTTP `403` and `verification_required: true` (including `email` and `workspace`) while logging out inactive users, so pending verification users are routed cleanly to the verification/sign-out state instead of attempting duplicate workspace signup.
- `PATCH` / `POST /api/v1/account/profile/` — Validates an IANA `timezone` (`zoneinfo.available_timezones()`), updates the signed-in verified user's timezone, and writes a `person` audit entry (`timezone`: old -> new).
- `POST /api/v1/account/password/change/` — Verifies `current_password`, enforces Django password validators and non-reuse (`400` if unchanged), updates password hash, marks unused `PURPOSE_RESET_PASSWORD` tokens as used, calls `invalidate_user_sessions(locked_user, exclude_session_key=request.session.session_key)` so all other sessions are revoked while keeping the caller's session open (`update_session_auth_hash` + `sync_session_version`), and records an audit entry (`password`: `Configured` -> `Updated`).
- `POST /api/v1/password-reset/confirm/` (`password_reset_confirm_view`) — Consumes the reset token, sets the new password, calls `invalidate_user_sessions(user)` to revoke all prior sessions, and then calls `login(request, user)` (which binds the updated `session_version`).
- `DELETE` / `POST /api/v1/auth/providers/<provider>/` — Unlinks a connected `google` or `microsoft` `ExternalIdentity` without modifying email/password login or the current session, and records an audit entry (`<provider>_sign_in`: `Linked` -> `Unlinked`). Refuses unlinking the last remaining sign-in method (`400`) when no usable password, passkey, or other OAuth identity exists.

### Workspace Roles, Member Lifecycle & Audit Log
- `GET /api/v1/workspaces/<slug>/roles/` — Lists `Owner` and `Staff` roles with member counts, `is_removable`, `can_manage_seat_plan`, `can_manage_sms_wallet`, and the 7 capability switches.
- `POST /api/v1/workspaces/<slug>/members/<member_id>/deactivate/` — Owner or `users_and_roles` manager deactivates a non-Owner member (`403` for Owner, `400` for self), terminates active sessions immediately, blocks sign-in across password/OAuth/passkey/email tokens, excludes the member from `active_seat_count`, and records an audit entry (`status`: `Active` -> `Deactivated`).
- `POST /api/v1/workspaces/<slug>/members/<member_id>/reactivate/` — Restores a deactivated non-Owner member's access and paid seat count, and writes an audit entry (`status`: `Deactivated` -> `Active`).
- `DELETE /api/v1/workspaces/<slug>/members/<member_id>/` — Refuses Owner removal (`403`) and refuses hard member deletion (`400`) so audit history stays intact.
- `GET /api/v1/workspaces/<slug>/audit/` — Returns paginated audit entries (`page`, `page_size`) filterable by `actor_kind`. Owner and `users_and_roles` managers see workspace-wide entries; plain Staff see their own actions and general record events while excluding invites and other users' account/security mutations.

All login, passkey, OAuth, verification, and password reset endpoints reject deactivated users and return uniform `404` responses across workspaces (`OwnerStaffAndAuditTests`).

---

## 4. Frontend Implementation (`jojoCRM-frontend`)

- **Direct Session Recovery & Unverified Signup Guard (`src/App.jsx`, `src/screens/SignupScreen.jsx`, `src/components/Button.jsx`, `src/screens/LoginScreen.jsx`, `src/screens/AcceptInviteScreen.jsx`)**: Visiting `/`, `/login`, or `/signup` checks `GET /api/v1/auth/session/` before flashing auth screens. Verified active sessions replace history directly to `/{slug}` via `window.history.replaceState`, while unverified sessions redirect to `/{slug}/verify` (or surface the pending-verification/sign-out screen cleanly on `/signup` without HTTP `403` loops). Handles live timezone updates (`onUserUpdated`) and `401` session revocation (`onSessionExpired`), fixes logout URL (`/api/v1/logout/`) in `SignupScreen.jsx`, adds a `ghost` variant to `Button.jsx`, and replaces raw inline action buttons in `LoginScreen.jsx` and `AcceptInviteScreen.jsx` with reusable `Button` components.
- **Reusable Drawer, Blurry Dropdowns & Icon-Only Controls (`src/components/Drawer.jsx`, `src/components/AuditLogDrawer.jsx`, `src/screens/WorkspaceShell.jsx`, `src/components/Select.jsx`, `src/index.css`)**: Frosted slide-over drawer (`36rem` width for **Workspace Audit Log**, styled with `border-left: 1px solid var(--border-strong)`) triggered from the workspace topbar icon-only **Audit Log** button (`variant="icon"`, `.icon-plain`, `data-tooltip`), alongside an icon-only **Sign out** button (which opens a confirmation modal before logging out) and theme toggle. All action buttons in the Audit Log drawer (**Close**, **Refresh**, **Previous**, and **Next**) are icon-only controls with tooltips and `aria-label` attributes. All dropdown portals (`.custom-select-portal`) use `var(--bg-surface)` with `blur(18px)` frosted glass for consistent translucent blurry backgrounds matching the UI template.
- **Settings (`src/screens/SettingsPane.jsx`)**: Reorganized into **Account & Security**, **People**, and **Roles** subtabs:
  - **Account & Security**: Profile & IANA timezone selector (shared via `src/utils/timezones.js` with `AcceptInviteScreen.jsx`), verified Password change form, Google & Microsoft unlink confirmation modal alongside active Email/Password, Mailbox connection notice, and Passkeys.
  - **People (`src/components/SettingsPeopleSection.jsx`)**: Active paid seat badge, Owner/Staff role badges, Active/Deactivated status, Deactivate confirmation modal, Reactivate action, permanent Owner guard, and invite `last_sent_at`/`expires_at` timestamps formatted in the current user's configured `timezone` (`Africa/Accra` fallback).
  - **Roles (`src/components/SettingsRolesSection.jsx`)**: Read-only comparison of Owner vs default Staff, Owner-only Seat Plan & SMS Wallet, and the 7 PRD switches (role cloning deferred to S2.4).
