# S1.1 — Ghana Workspace Registration

Story **S1.1** implements the signup connection-location check, workspace and user creation, and routing to `/{slug}` on the single application domain.

## 1. Connection-Location Check Rule (IPinfo)

During `POST /api/v1/signup/`, the backend resolves the caller's IP address on the server and queries **IPinfo**:

- **Request**: `GET https://ipinfo.io/{ip}/json` with header `Authorization: Bearer <IPINFO_TOKEN>`.
- **Clearly Ghana (`GH`)**: Registration proceeds only when all of the following hold:
  1. The HTTP lookup succeeds (`200 OK`) within `GEO_LOOKUP_TIMEOUT` seconds and returns a JSON object.
  2. `country` is `"GH"`.
  3. `privacy` is a JSON object where `vpn`, `proxy`, `tor`, `relay`, and `hosting` are all present and strictly `false`.

Client-supplied `country` or `ip` values in the request body or untrusted headers are ignored. `REMOTE_ADDR` is used unless `REMOTE_ADDR` is listed in `TRUSTED_PROXY_IPS`, in which case the client address from `X-Forwarded-For` is used.

## 2. Environment Keys

Configured in `.env` (documented with empty defaults in `.env.example`):

| Key | Description |
| --- | --- |
| `IPINFO_TOKEN` | Bearer token for the IPinfo API (`https://ipinfo.io/{ip}/json`). Never logged. |
| `GEO_LOOKUP_IP` | Optional IP override used **only** when `DEBUG=True` so a developer can test a Ghana or non-Ghana address locally. Ignored when `DEBUG=False`. |
| `GEO_LOOKUP_TIMEOUT` | Timeout in seconds for the IPinfo HTTP call (defaults to `3.0`). |
| `TRUSTED_PROXY_IPS` | Comma-separated proxy IPs allowed to supply `X-Forwarded-For` (defaults to empty / untrusted). |
| `BYPASS_GEO_CHECK` | Optional boolean (`True` / `False`, defaults to `False`). When set to `True` in `.env`, bypasses the IPinfo connection-location check during local development when an IPinfo privacy subscription is not active. |

## 3. Refusal Behavior

The connection-location check runs **before** input validation or database writes. When a lookup fails, times out, returns a malformed or incomplete payload, reports a country other than `GH`, or sets any of `privacy.{vpn,proxy,tor,relay,hosting}` to `true`:

- The server responds with HTTP `403` and the single JSON message:
  ```json
  {
    "error": "This connection-location check could not confirm a connection in Ghana, so registration stops here."
  }
  ```
- No `Workspace` or `User` row is inserted.

When the connection-location check passes, the server validates `workspace_name`, `slug` (optional on `POST /api/v1/signup/`, derived from `workspace_name` if omitted, editable on `SignupScreen.jsx` with a live `/{slug}` URL preview; rejects duplicates, invalid characters, and reserved slugs `api`, `admin`, `static`, `assets`, `signup`, `login`, `verify-email`, `forgot-password`, `reset-password`, `accept-invite`, `invite`), `email` (unique), and `password` (minimum 8 characters plus Django's configured `AUTH_PASSWORD_VALIDATORS`, hashed with Django's password hashers). On valid input, the workspace and user (`timezone="Africa/Accra"`, `workspace.created_by = user`) are created in one atomic transaction.

## 4. Workspace URL Shape

- Single domain with path `/{slug}` (local web origin `http://localhost:5173/{slug}`).
- API endpoints stay under `/api/v1/` on the same origin via the Vite `/api` proxy:
  - `GET /api/v1/csrf/`
  - `POST /api/v1/signup/`
  - `GET /api/v1/workspaces/<slug>/`

## 5. How a Reviewer Verifies Pass vs. Refusal

1. **Automated test suite (mocked provider)**:
   ```bash
   python manage.py test
   ```
2. **In the browser (`http://localhost:5173`)**:
   - **Refusal**: With `IPINFO_TOKEN` unset (or `DEBUG=True` and `GEO_LOOKUP_IP=8.8.8.8`), submit the signup form at `http://localhost:5173/`. The UI displays `"This connection-location check could not confirm a connection in Ghana, so registration stops here."` and `Workspace.objects.count()` remains `0`.
   - **Pass**: With `IPINFO_TOKEN` set to a valid IPinfo token that includes privacy detection and `DEBUG=True` with `GEO_LOOKUP_IP` set to a clear Ghana residential/ISP address (e.g. `154.160.1.1`), submit the form. The server creates the workspace and user in one transaction, starts the session, and the browser navigates to `http://localhost:5173/{slug}` showing the empty workspace shell (`"This workspace has no records yet."`).
