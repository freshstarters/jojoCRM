# S2.2 Lead Conversion

Implemented in `/home/demiz/Projects/jojo_crm_venv/jojoCRM` and `/home/demiz/Projects/jojoCRM-frontend`.

## Overview

S2.2 delivers Lead-to-Deal conversion on workspaces whose enabled tables include `Deal` (such as General services), while keeping conversion completely unavailable on packs without `Deal` (such as Healthcare and wellness).

Converting an open `Lead` atomically creates or links an `Account`, a `Contact`, and a `Deal`, keeps the `Lead` on record marked `converted`, records conversion note activities and audit history, and refuses any second conversion attempt with `409 Conflict`.

## Data Model and Migration

Migration `mainapp/migrations/0011_account_source_lead_contact_source_lead_and_more.py` adds bidirectional `SET_NULL` conversion links in [`mainapp/models/crm.py`](../mainapp/models/crm.py):

- `Account.source_lead`, `Contact.source_lead`, and `Deal.source_lead` (`related_name='converted_accounts'`, `'converted_contacts'`, `'converted_deals'`) point newly created records back to their originating `Lead`.
- `Lead.converted_at`, `Lead.converted_by`, `Lead.converted_account`, `Lead.converted_contact`, and `Lead.converted_deal` (`related_name='source_leads'`) record conversion metadata and point to the converted/reused records.

Using `SET_NULL` preserves the converted `Lead` history even if an `Account`, `Contact`, or `Deal` is later removed or merged in `S7.2`.

## Service Layer & Business Rules

Implemented across [`mainapp/service/lead_conversion_service.py`](../mainapp/service/lead_conversion_service.py) and [`mainapp/service/crm_service.py`](../mainapp/service/crm_service.py):

- **Pack / Table Gating**: `is_lead_conversion_available(workspace)` checks that `Account`, `Contact`, `Lead`, and `Deal` are all enabled in `WorkspaceTable`. Workspaces without `Deal` (such as Healthcare and wellness) report `conversion_available=False` and return `404` from conversion endpoints.
- **Role Permissions**: `get_lead_conversion_capability(user, workspace)` and `convert_lead_record` enforce `lead.update`, `deal.create`, `account.create` (or `account.read` when reusing an existing Account), and `contact.create` (or `contact.read` plus `contact.update` when linking a standalone Contact). Missing rights return `403`.
- **Match Detection & Reuse (`conversion-preview`)**:
  - `get_lead_conversion_preview` scans the workspace for matching `Account` rows (case-insensitive `name`, `email`, or normalized `phone`) and `Contact` rows (case-insensitive `email`, normalized `phone`, or `name`), returning `match_reasons`, default field values (`account_name` falling back to `lead.company or lead.name`, `deal_stage='Qualified'`, `deal_amount='0.00'`), and role capabilities.
  - Choosing to reuse an existing Account or Contact keeps its existing fields unchanged. Reusing a standalone Contact links it to the resolved Account (audited on `contact.account`), and pairing a Contact that already belongs to a different Account raises `400`.
- **Idempotency & Post-Conversion Protections**:
  - `convert_lead_record` locks the `Lead` inside `transaction.atomic()` with `select_for_update(of=('self',))` and rejects a second conversion attempt with `409 Conflict`.
  - Converted leads cannot be deleted (`409 Conflict`) and lock their identity fields (`name`, `company`, `email`, `phone` return `400` if changed via `PATCH`), while `notes`, `sms_consent`, and attached `Activity` records remain editable.
- **Activities & Audit**:
  - Pre-conversion activities remain on the `Lead`. When `activity` is enabled, conversion logs a conversion `Note` on the `Lead`, `Account`, `Contact`, and `Deal`.
  - `AuditLog` records `create` events for newly created records, `update` for any standalone Contact linked to an Account, and `update` on `lead.status` (`open` -> `converted`) and `lead.converted_deal`.

## API Endpoints

Registered in [`mainapp/urls.py`](../mainapp/urls.py) and implemented in [`mainapp/views/crm_views.py`](../mainapp/views/crm_views.py):

- `GET /api/v1/workspaces/<slug>/records/lead/<record_id>/conversion-preview/` (and `/leads/<record_id>/conversion-preview/`) — Returns lead details, `already_converted`, `account_matches`, `contact_matches`, defaults, and role capabilities (`404` when `Deal` or `Lead` is disabled).
- `POST /api/v1/workspaces/<slug>/records/lead/<record_id>/convert/` (and `/leads/<record_id>/convert/`) — Converts an open `Lead` using optional overrides (`account_id` or `account_name`/`account_city`, `contact_id` or `contact_name`/`contact_title`, `deal_title`, `deal_amount`, `deal_stage`, `target_close_date`, `win_probability`) or empty payload defaults (`409` if already converted, `404` if `Deal` is disabled).

## Frontend Screens & Components

Implemented in `/home/demiz/Projects/jojoCRM-frontend`:

- [`src/service/crmService.js`](file:///home/demiz/Projects/jojoCRM-frontend/src/service/crmService.js): Adds `fetchLeadConversionPreview`, `convertLeadRecord`, and `canUserConvertLead`.
- [`src/components/crm/ConvertLeadModal.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/crm/ConvertLeadModal.jsx): Reusable conversion modal featuring a server-backed Lead search picker (labeling converted leads `(Already Converted)` so second-conversion attempts visibly display the `409` refusal), Account/Contact match banners with capability-aware **Create new** vs **Reuse existing** defaults (switching automatically to `Reuse existing` when `can_create_account` or `can_create_contact` is `false`, flagging standalone Contacts when `can_update_contact` is `false`, and enforcing Account–Contact locks), and full Account, Contact, and Deal creation fields.
- [`src/components/crm/ConversionResultBanner.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/crm/ConversionResultBanner.jsx): Reusable post-conversion confirmation banner with direct `Open` actions for the Account, Contact, Deal, and Lead.
- [`src/components/crm/RecordLinksSection.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/crm/RecordLinksSection.jsx): Renders `Converted from Lead` / `Converted from Leads` links on converted Accounts, Contacts, and Deals.
- [`src/screens/LeadsPane.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/screens/LeadsPane.jsx): Adds `Convert Lead` toolbar button, row `Convert` action (open leads only), and Inspector `Convert this lead` action gated on `Deal` table enablement (completely hidden on Healthcare), role-permission tooltips when `Deal` is enabled without convert rights, locked identity fields and hidden Delete on converted leads, and automatic list/chart/inspector refresh on conversion.
- [`src/screens/DealsPane.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/screens/DealsPane.jsx), [`src/screens/AccountsPane.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/screens/AccountsPane.jsx), and [`src/screens/ContactsPane.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/screens/ContactsPane.jsx): Add `Convert Lead` on the Deals toolbar (when `Lead` is enabled) and `From LD-...` source-lead indicators on converted records.
- [`src/App.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/App.jsx) and [`src/screens/WorkspaceShell.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/screens/WorkspaceShell.jsx): Code-split `WorkspaceShell`, secondary auth routes, and individual CRM/Settings panes via `React.lazy` and `Suspense` so entry chunks remain under Vite's 500 kB threshold.

## Automated Tests

Backend tests live in [`mainapp/tests/test_lead_conversion.py`](../mainapp/tests/test_lead_conversion.py), verifying:

- Full General services conversion creating Account, Contact, and Deal pointing at the Lead, keeping the Lead listed with `status == 'converted'`, preserving pre-conversion activities, adding conversion Notes, updating Dashboard counts, and recording audit events.
- Second conversion refusal (`409`), converted lead delete refusal (`409`), identity field lock (`400`), and `notes`/`sms_consent` edits (`200`).
- Blank `company` fallback to `lead.name` and empty override validation (`400`).
- Match detection (`Company name`, `Email`, `Phone`, `Name`), reusing existing Accounts and standalone Contacts, preserving reused fields, and refusing mismatched Account–Contact pairs (`400`).
- Healthcare pack gating (`404` while `Deal` is disabled; succeeds once `Deal` is enabled) and role permission + cross-workspace isolation (`403`/`404`).
