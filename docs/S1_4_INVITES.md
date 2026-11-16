# S1.4 — Invites

Story **S1.4** adds workspace member invitations, single-workspace enforcement across signup and invites, and location-free login for all existing workspace accounts.

## 1. Core Guarantees

1. **Creator / authorized invite management**: Only the workspace creator (`workspace.created_by`) — or a future role holding the `users_and_roles` switch from S1.5 — can send invites, view pending/past invites, resend invites, or revoke pending invites. All verified members in the workspace can view the read-only member directory under **Settings -> People**.
2. **Fail-closed transactional email**: Sending or resending an invite verifies `ensure_email_provider_configured()` before sending. If email configuration is missing, the API returns HTTP `503` without saving a `WorkspaceInvite` row.
3. **No location check on invites**: Neither previewing (`GET /api/v1/invites/preview/`) nor accepting (`POST /api/v1/invites/accept/`) runs the Ghana connection-location check. Connections outside Ghana can accept an invite and join that workspace immediately.
4. **One workspace per account**:
   - Inviting an email that already belongs to the same workspace returns HTTP `400`.
   - Inviting an email that already belongs to another workspace returns HTTP `409`.
   - Accepting an invite when another workspace invite with the same email was already accepted returns HTTP `409`.
   - Authenticated users are blocked (`403`) from `POST /api/v1/signup/` (after the connection-location check) and see an already-signed-in guard on `/signup`.
5. **No location check on later logins**: Password, Google, Microsoft, and passkey logins do not check connection location for either workspace founders or invited members.

## 2. Environment Configuration

Configured in `.env` (documented in `.env.example`):

| Key | Description |
| --- | --- |
| `INVITE_TTL_HOURS` | Invite token lifetime in hours before expiry (defaults to `24`). |
| `SERVER_EMAIL`, `SERVER_EMAIL_PASSWORD`, `EMAIL_HOST`, `EMAIL_PORT` | Transactional SMTP settings shared with S1.2 verification and password-reset emails. Missing credentials fail closed with HTTP `503`. |

## 3. Data Model (`mainapp/models.py`)

**`WorkspaceInvite`** (`mainapp/migrations/0004_workspaceinvite.py`):
- `workspace`: ForeignKey to `Workspace` (`CASCADE`)
- `invited_by`: Nullable ForeignKey to `User` (`SET_NULL`)
- `email`: Lowercased email address
- `token_hash`: Unique SHA-256 hex digest of the single-use invite token (raw token is emailed, never stored in plaintext)
- `expires_at`: Expiration timestamp (`INVITE_TTL_HOURS`, default 24 hours)
- `last_sent_at`: Timestamp updated on send/resend for 60-second resend cooldown enforcement
- `accepted_at`: Nullable timestamp set upon acceptance
- `revoked_at`: Nullable timestamp set upon manual revoke
- `created_at`: Creation timestamp
- Partial `UniqueConstraint`: `(workspace, email)` unique where `accepted_at IS NULL AND revoked_at IS NULL`

## 4. API Endpoints (`mainapp/urls.py`, `mainapp/views/invite_views.py`)

All endpoints live under `/api/v1/`:

- `GET /api/v1/auth/session/`: Returns `{authenticated: false}` for anonymous callers, or `{authenticated: true, workspace, user, permissions}` for an active session (`permissions: {is_owner, can_manage_invites}`).
- `GET /api/v1/workspaces/<slug>/people/` (verified workspace member):
  - Returns `workspace`, `can_manage_invites`, sorted `members`, and `invites` (populated only when `can_manage_invites` is `true`, otherwise `[]`).
- `POST /api/v1/workspaces/<slug>/invites/` (owner / `can_manage_invites`, verified):
  - Validates `{email}`, enforces single-workspace rules, enforces a 60-second resend cooldown on pending invites, rotates `token_hash` and refreshes `expires_at` / `last_sent_at` in-place for existing unrevoked/unaccepted rows, and emails `/accept-invite?token=...`.
- `POST /api/v1/workspaces/<slug>/invites/<invite_id>/revoke/` (owner / `can_manage_invites`, verified):
  - Sets `revoked_at` on a pending invite inside `transaction.atomic()`.
- `GET /api/v1/invites/preview/?token=...` (public, not location-checked):
  - Validates raw token and returns `{email, workspace: {name, slug}, invited_by_email, expires_at}` if valid, `400` if revoked/expired/used/invalid, or `409` if that email already joined a workspace.
- `POST /api/v1/invites/accept/` (public, not location-checked):
  - Accepts `{token, password, timezone}`, validates password strength and IANA timezone (`zoneinfo.available_timezones()`), atomically locks and consumes the invite (`accepted_at`), creates the verified `User` (`email_verified=True`), replaces any prior session, logs the new user in, and returns `{workspace, user, permissions, mailbox_connection}`.

## 5. Frontend Screens & Components (`jojoCRM-frontend`)

- `/accept-invite?token=...` (`src/screens/AcceptInviteScreen.jsx`): Previews invitation details, collects password + confirmation + timezone (pre-filled from browser timezone via reusable custom `Select` in `src/components/Select.jsx`), and signs the new member into their workspace.
- `/signup` (`src/screens/SignupScreen.jsx`): Checks `GET /api/v1/auth/session/` and shows an already-signed-in notice with **Return to workspace** and **Sign out** actions when an active session is detected.
- `/{slug}` -> **Settings** -> **People** (`src/screens/SettingsPane.jsx`, `src/components/SettingsPeopleSection.jsx`, `src/components/Modal.jsx`):
  - All verified members see the workspace members table.
  - Authorized invite managers additionally see the send-invite form, invites status table, **Resend** action, and **Modal**-confirmed **Revoke** action.
