# S1.2 — Email and Password Authentication

Story **S1.2** implements email verification, the workspace verification gate, email and password sign-in (with wrong-password refusal), and password reset via single-use transactional email tokens.

## 1. Transactional Email Provider & Fail-Closed Configuration

Configured in `.env` (documented in `.env.example`):

| Key | Description |
| --- | --- |
| `SERVER_EMAIL` | Sender email / SMTP username (e.g. Gmail address). |
| `SERVER_EMAIL_PASSWORD` | SMTP password / App Password. Never logged. |
| `SERVER_EMAIL_HOST` | SMTP server host (defaults to `smtp.gmail.com`). |
| `SERVER_EMAIL_PORT` | SMTP port (defaults to `465`). |
| `SERVER_EMAIL_USE_TLS` | Use STARTTLS (`False` by default when SSL on 465 is used). |
| `SERVER_EMAIL_USE_SSL` | Use implicit SSL (`True` by default on port 465). |
| `APP_BASE_URL` | Base web URL used in verification and reset links (defaults to `http://localhost:5173`). |

### Fail-Closed Behavior

`mainapp/service/email_service.py` checks `ensure_email_provider_configured()` before issuing or sending any transactional email. When SMTP credentials are missing or an SMTP send fails:

- During `POST /api/v1/signup/`: the database transaction rolls back atomically so no `Workspace` or `User` record is left behind, and the endpoint returns HTTP `503`.
- During `POST /api/v1/resend-verification/` or `POST /api/v1/password-reset/request/`: the endpoint returns HTTP `503`.

## 2. Data Model & Token Lifecycle

1. **`User` fields** (`mainapp/models.py`):
   - `email_verified` (`BooleanField`, default `False`)
   - `email_verified_at` (`DateTimeField`, nullable)
2. **`EmailActionToken`** (`mainapp/models.py`):
   - `user`: ForeignKey to `User`
   - `purpose`: `verify_email` (1-hour TTL) or `reset_password` (15-minute TTL)
   - `token_hash`: SHA-256 hex digest of the raw URL-safe token (`secrets.token_urlsafe(32)`)
   - `used_at`: timestamp set when the token is consumed or superseded
   - `expires_at`: expiration timestamp (`created_at + TTL`)
   - **Single-use**: Consuming a token sets `used_at = timezone.now()`. Any replay of a used or expired token is rejected (`400 Bad Request`).
   - **Superseding**: Issuing a new token for the same `(user, purpose)` marks any previous unused token of that purpose as used.
   - **Cooldown**: A 60-second cooldown per `(user, purpose)` prevents repeated dispatch spam (`429 Too Many Requests`).

## 3. API Endpoints

All endpoints live under `/api/v1/`:

- `POST /api/v1/signup/`
  - Runs the S1.1 Ghana connection-location check first.
  - Atomically creates the `Workspace` and `User` (`email_verified=False`) and sends a verification email containing `{APP_BASE_URL}/verify-email?token=<raw_token>`.
  - Does not start an authenticated workspace session; returns `201 Created` with `"verification_required": true`.
- `POST /api/v1/verify-email/`
  - Consumes `{ "token": "<raw_token>" }`, marks `user.email_verified = True`, starts a Django session (`login()`), and returns `200 OK` with `workspace` and `user`.
- `POST /api/v1/resend-verification/`
  - Accepts `{ "email": "..." }`. Enforces the 60-second cooldown and sends a fresh verification link if the account exists and is still unverified.
- `POST /api/v1/login/`
  - Accepts `{ "email": "...", "password": "..." }`.
  - Wrong email or wrong password returns `401 Unauthorized` (`{"error": "Invalid email or password."}`).
  - Valid password for an unverified account returns `403 Forbidden` (`{"error": "Verify your email address from the verification message before opening this workspace.", "verification_required": true}`).
  - Valid password for a verified account starts a Django session and returns `200 OK`.
- `POST /api/v1/logout/`
  - Ends the current Django session (`200 OK`).
- `POST /api/v1/password-reset/request/`
  - Accepts `{ "email": "..." }`, enforces the 60-second cooldown, and sends a reset email containing `{APP_BASE_URL}/reset-password?token=<raw_token>`.
- `POST /api/v1/password-reset/confirm/`
  - Accepts `{ "token": "<raw_token>", "password": "<new_password>" }`.
  - Validates the token and password rules, sets the new password, marks `email_verified = True`, invalidates any remaining unused tokens for that user, logs the user in, and returns `200 OK`.
- `GET /api/v1/workspaces/<slug>/`
  - Returns `401` when unauthenticated and `403` (`verification_required: true`) when `user.email_verified` is `False`.

## 4. Frontend Screens & Routes

All standalone auth and closed-workspace cards share `src/components/AuthCardShell.jsx` (with synchronized light/dark theme switching via `ThemeToggleButton` and `CrmTooltipLayer`):

- `/signup` (`SignupScreen.jsx`): Creates workspace (with editable `/{slug}` Workspace URL and password-policy guidance) and transitions to the "Verify your email address" screen with a resend action.
- `/verify-email?token=...` (`VerifyEmailScreen.jsx`): Automatically submits the single-use token on load, starts the session, and navigates to `/{slug}`.
- `/login` (`LoginScreen.jsx`): Email and password sign-in, wrong-password error banner, unverified-account resend prompt, and navigation to `/forgot-password` and `/signup`.
- `/forgot-password` (`ForgotPasswordScreen.jsx`): Requests a password reset email.
- `/reset-password?token=...` (`ResetPasswordScreen.jsx`): Sets a new password from the reset link and opens `/{slug}`.
- `/{slug}` (`App.jsx` + `WorkspaceShell.jsx`): Renders a closed-workspace gate when unauthenticated (`401`) or unverified (`403`), and opens the workspace shell defaulting to `Dashboard` with topbar breadcrumb & theme toggle, collapsed sidebar user badge, and `Sign out` when verified.
