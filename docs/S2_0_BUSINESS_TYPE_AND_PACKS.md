# S2.0 — Business Type and Packs

Story **S2.0** implements the 20-type workspace catalog, starter pack provisioning (`5` core objects + `12` pack objects = `17` tables), specialized Contact screen labels, secondary pack activation, empty-table disable protection, label customization, and persistent URL pane/subtab navigation across `jojoCRM` (Django backend) and `jojoCRM-frontend` (React + Vite UI).

---

## 1. Starter Catalog (`mainapp/catalog.py` & `mainapp/service/catalog_service.py`)

### Core Objects (5 — always enabled)
- `account` (`Account`), `contact` (`Contact`), `activity` (`Activity`), `file` (`File`), `tag` (`Tag`)

### Catalog Objects (25)
- `lead`, `deal`, `product`, `line_item`, `invoice`, `payment`, `inventory`, `campaign`, `contract`, `ticket`, `customer_subscription`, `job`, `appointment`, `supplier`, `purchase`, `shipment`, `vehicle`, `property`, `booking`, `program`, `enrollment`, `visit`, `policy`, `membership`, `asset`

### Business Types (20) & Contact Screen Labels
- Each of the 20 business types defined in `docs/PRD.md` maps to exactly `12` catalog objects (`17` starter tables in total with the `5` core objects).
- Primary business-type Contact screen labels:
  - `healthcare_wellness`: **Patient** (`Patients`)
  - `education`: **Student or Guardian** (`Students or Guardians`)
  - `nonprofit_faith_membership`: **Donor or Member** (`Donors or Members`)
  - All other 17 business types: **Contact** (`Contacts`)
- Adding a second pack preserves `Workspace.contact_label`; authorized users can also customize or reset `contact_label` under **Settings -> Workspace**.

### Record-Count Protection Registry
- `mainapp/service/catalog_service.py` exposes `OBJECT_RECORD_COUNT_RESOLVERS`, `register_record_count_resolver()`, and `get_table_record_count(workspace, object_key)` so S2.1+ starter models (and automated test resolvers) report per-table row counts without requiring S2.1 CRUD models in S2.0.
- Disabling a core table (`is_core=True`) is refused with HTTP `400`.
- Disabling a pack table with `record_count > 0` is refused with HTTP `409`.
- Turning on another pack only adds/enables the new pack's objects and leaves all existing tables and saved records intact.

---

## 2. Data Model & Migrations (`mainapp/models.py`)

Added via `0007_workspace_business_type_and_more.py` and backfilled via `0008_backfill_workspace_packs_and_tables.py`:

- **`Workspace` extensions**:
  - `business_type` (`CharField(max_length=64, default='general_services')`)
  - `business_type_label` (`CharField(max_length=150, default='General services')`)
  - `contact_label` (`CharField(max_length=80, default='Contact')`)
- **`WorkspacePack`**:
  - `workspace`, `pack_key`, `is_primary`, `enabled_by`, `created_at`
  - Constraints: `unique_pack_per_workspace` (`workspace`, `pack_key`) and `unique_primary_pack_per_workspace` (`workspace` where `is_primary=True`).
- **`WorkspaceTable`**:
  - `workspace`, `object_key`, `is_core`, `is_enabled`, `source_pack_key`, `created_at`, `updated_at`
  - Constraints: `unique_object_per_workspace` (`workspace`, `object_key`) and `core_workspace_table_always_enabled` (`is_core=False OR is_enabled=True`).

---

## 3. Permissions & API Endpoints (`mainapp/urls.py` under `/api/v1/`)

- **Catalog Management Permission (`user_can_manage_workspace_catalog` in `mainapp/service/role_service.py`)**:
  - Granted to an active, verified workspace **Owner** or a role holding either the `users_and_roles` or `theme_and_layouts` switch.
  - Serialized under `permissions.can_manage_packs` and `permissions.can_manage_workspace_profile`.
- **Public Business Type Catalog**:
  - `GET /api/v1/catalog/business-types/` — Returns the `5` core objects, `25` catalog objects, `contact_label_presets`, and `20` business types with their `12` pack objects.
- **Workspace Creation (`POST /api/v1/signup/`)**:
  - Runs the fail-closed IPinfo Ghana connection-location check first.
  - Requires a valid `business_type` from the 20 catalog types (`400` if missing/invalid).
  - Creates the `Workspace`, primary `WorkspacePack`, `17` `WorkspaceTable` rows, default `Owner`/`Staff` roles, Owner user, and `AuditLog` entries inside `transaction.atomic()`.
- **Workspace Profile (`GET` / `PATCH /api/v1/workspaces/<slug>/`)**:
  - `GET` returns workspace metadata including `business_type`, `business_type_label`, `contact_label`, `contact_plural_label`, `enabled_packs`, `enabled_object_keys`, `active_table_count`, `core_table_count`, and `pack_table_count`.
  - `PATCH` updates `business_type_label`, `contact_label`, or `reset_contact_label` and records `AuditLog` entries.
- **Workspace Packs & Tables**:
  - `GET /api/v1/workspaces/<slug>/packs/` — Lists all `30` objects with `is_enabled`, `record_count`, `can_disable`, `source_pack_label`, and all `20` packs.
  - `POST /api/v1/workspaces/<slug>/packs/` — Turns on another `pack_key` additively and records a `workspace_pack` audit event.
  - `PATCH /api/v1/workspaces/<slug>/tables/<object_key>/` — Toggles `is_enabled` (`true`/`false`), enforcing core (`400`) and non-empty (`409`) protections and recording a `workspace_table` audit event.

---

## 4. Frontend Implementation (`jojoCRM-frontend`)

- **`src/screens/SignupScreen.jsx`**: Fetches `GET /api/v1/catalog/business-types/`, requires selecting one of the 20 business types via `Select`, and renders a live 17-table pack preview (including specialized Contact labels).
- **`src/utils/catalog.js` & `src/screens/WorkspaceShell.jsx`**: Dynamically builds sidebar navigation groups from `enabled_object_keys`, labels Contact with `contact_plural_label` (e.g., `Patients`), hides disabled pack tables (such as `Deals & Pipeline` on Healthcare), supports subtabs under multi-table panes (`Deals & Pipeline`, `Invoicing`, `Settings`), and surfaces unavailable/disabled states cleanly.
- **`src/App.jsx` & `src/screens/SettingsPane.jsx`**: Encodes workspace pane and subtab navigation in clean paths (`/{slug}`, `/{slug}/{pane}`, `/{slug}/{pane}/{subtab}`) via `window.history.pushState` and `popstate` so browser refresh and Back/Forward return to the exact active pane and subtab.
- **`src/components/SettingsWorkspaceSection.jsx` & `src/components/SettingsPacksSection.jsx`**: Adds **Workspace** and **Packs** Settings subtabs for renaming `business_type_label`, customizing/resetting `contact_label`, inspecting all `5` core + `12+` pack tables with record counts, turning on additional packs, and turning off empty pack tables.
- **`src/components/AuditLogDrawer.jsx`**: Maps `workspace_pack` (`Pack`) and `workspace_table` (`Table`) object types in the audit drawer.
