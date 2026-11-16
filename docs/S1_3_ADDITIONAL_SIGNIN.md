# S1.3 — Additional Sign-In (Google, Microsoft, and Passkeys)

Story **S1.3** adds Google OIDC sign-in, Microsoft OIDC sign-in, and optional WebAuthn passkeys as additional sign-in methods into an existing jojoCRM account identified by email.

## 1. Core Guarantees

1. **Single account per email**: Google, Microsoft, and passkeys open the existing `User` account matching that email. They never create a second `User` or a new `Workspace`.
2. **Passkeys are optional**: Registering one or more passkeys does not disable or block email and password sign-in, and a passkey is not required as a second factor for password, Google, or Microsoft sign-in.
3. **Sign-in only (mailbox stays unset)**: Google and Microsoft OAuth request only OIDC identity scopes (`openid email profile`). No access/refresh tokens or Gmail/Graph mail permissions are requested or stored, and `mailbox_connection` remains unset (`connected: false`, `provider: null`).
4. **Fail-closed configuration**: Missing OAuth client credentials return HTTP `503` without bypassing verification.

## 2. Environment Configuration

Configured in `.env` (documented in `.env.example`):

| Key | Description |
| --- | --- |
| `GOOGLE_OAUTH_CLIENT_ID` | Google Cloud OAuth 2.0 Web Client ID. |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Google Cloud OAuth 2.0 Client Secret. Never logged. |
| `GOOGLE_OAUTH_REDIRECT_URI` | Optional override; defaults to `{APP_BASE_URL}/api/v1/auth/google/callback/`. |
| `MICROSOFT_OAUTH_CLIENT_ID` | Microsoft Entra Application (client) ID. |
| `MICROSOFT_OAUTH_CLIENT_SECRET` | Microsoft Entra Client Secret **Value**. Never logged. |
| `MICROSOFT_OAUTH_TENANT_ID` | Defaults to `common` (supports both Entra work/school and personal Microsoft accounts). |
| `MICROSOFT_OAUTH_REDIRECT_URI` | Optional override; defaults to `{APP_BASE_URL}/api/v1/auth/microsoft/callback/`. |
| `WEBAUTHN_RP_ID` | WebAuthn Relying Party ID (defaults to hostname of `APP_BASE_URL`, e.g. `localhost`). |
| `WEBAUTHN_RP_NAME` | WebAuthn Relying Party display name (defaults to `jojoCRM`). |
| `WEBAUTHN_ORIGIN` | Allowed WebAuthn origin(s), comma-separated (defaults to `APP_BASE_URL`, e.g. `http://localhost:5173`). |

### Authorized Redirect URIs
Register these exact URIs in your Google Cloud Console and Azure App Registration:
- Google: `http://localhost:5173/api/v1/auth/google/callback/`
- Microsoft: `http://localhost:5173/api/v1/auth/microsoft/callback/`

## 3. Data Model (`mainapp/models.py`)

1. **`ExternalIdentity`** (`mainapp/migrations/0003_passkeycredential_externalidentity.py`):
   - `user`: ForeignKey to `User`
   - `provider`: `google` or `microsoft`
   - `subject`: OIDC `sub` claim (pinned per `(user, provider)` and unique per `(provider, subject)`)
   - `email_at_link`: normalized verified email returned by the provider
   - `created_at`, `last_used_at`
2. **`PasskeyCredential`**:
   - `user`: ForeignKey to `User`
   - `credential_id`: unique base64url WebAuthn credential ID
   - `public_key`: base64url COSE public key
   - `sign_count`, `transports`, `name`, `aaguid`, `backed_up`, `created_at`, `last_used_at`

## 4. Verification Rules & API Endpoints

All endpoints live under `/api/v1/`:

- `GET /api/v1/auth/providers/`: Reports configuration status (`google`, `microsoft`, `passkey`).
- `GET /api/v1/auth/google/start/` and `GET /api/v1/auth/microsoft/start/`:
  - Fail closed with `503` if client credentials are missing.
  - Store PKCE `code_verifier`, `state`, and `nonce` in the Django session and redirect (`302`) — or return `{"authorization_url": ...}` when `?response=json` is supplied — with `scope=openid email profile`.
- `GET /api/v1/auth/google/callback/` and `GET /api/v1/auth/microsoft/callback/`:
  - Validate single-use `state`, exchange `code` + PKCE `code_verifier`, verify OIDC `id_token` signature against provider JWKS, `aud`, `iss`, `exp`, and `nonce`.
  - Google requires `email_verified: true`.
  - Microsoft requires `email` (rejecting if `xms_edov` is false/`0`), falling back to `preferred_username` only when `email` is absent and `xms_edov` is verified (`true`/`1`).
  - Matches an existing active `User` by normalized email (marking `email_verified = True` if previously unverified), enforces `(user, provider)` subject pinning, starts a Django session, and redirects to `/{slug}` (or `/login?auth_error=...` on failure).
- `GET /api/v1/auth/methods/` (authenticated, verified):
  - Returns `has_password`, linked providers (`google`, `microsoft`), registered passkeys, and `mailbox_connection: {"connected": false, "provider": null}`.
- `POST /api/v1/auth/passkey/register/options/` and `POST /api/v1/auth/passkey/register/verify/` (authenticated, verified):
  - Registers a WebAuthn passkey for the current user without altering their password.
- `DELETE /api/v1/auth/passkey/<id>/` (authenticated, verified):
  - Removes a passkey belonging to the current user.
- `POST /api/v1/auth/passkey/login/options/` and `POST /api/v1/auth/passkey/login/verify/`:
  - Supports both email-scoped (`allowCredentials` when `email` is provided) and discoverable (`allowCredentials: []` when `email` is omitted) WebAuthn sign-in.

## 5. Frontend Screens & Components (`jojoCRM-frontend`)

- `/login` (`src/screens/LoginScreen.jsx`): Email and password sign-in alongside **Google**, **Microsoft**, and **Sign in with passkey** actions, plus `?auth_error=` display.
- `/{slug}` → **Settings** (`src/screens/WorkspaceShell.jsx`, `src/screens/SettingsPane.jsx`, `src/components/Subtabs.jsx`): **Sign-in & Security** tab showing password status, Google/Microsoft link status, **Mailbox connection: Not connected**, and passkey registration/removal.
- WebAuthn browser helper: `src/utils/webauthn.js` (use `http://localhost:5173` in the browser, as WebAuthn requires a domain origin rather than an IP literal).
