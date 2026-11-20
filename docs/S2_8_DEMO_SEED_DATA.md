# S2.8 Demo Seed Data for Manual Testing

Implemented in `/home/demiz/Projects/jojo_crm_venv/jojoCRM`.

## Overview

S2.8 provides a non-production, idempotent Django management command (`manage.py seed_demo_data`) backed by [`mainapp/service/demo_seed_service.py`](../mainapp/service/demo_seed_service.py) that populates verified Owner, Staff, and QA role accounts alongside realistic Ghanaian CRM data across **all 20 starter business types** (or any subset via `--only`).

Self-serve workspace signups (`POST /api/v1/signup/`) are completely unaffected and continue to start empty.

## Command Usage

```bash
# Seed all 20 business-type demo workspaces (DEBUG=True only; idempotent)
python manage.py seed_demo_data

# Seed only specific business types (repeatable or comma-separated)
python manage.py seed_demo_data --only general_services --only healthcare_wellness
python manage.py seed_demo_data --only retail_trading,financial_services

# Cleanly reset and recreate targeted demo workspaces without touching non-seeded workspaces
python manage.py seed_demo_data --reset
python manage.py seed_demo_data --only general_services --reset

# Override the dev password for newly created accounts
python manage.py seed_demo_data --password "CustomQaPass123!"

# Staging / non-debug override (requires --force AND explicit --password or DEMO_SEED_PASSWORD)
python manage.py seed_demo_data --force --password "StagingQaPass99!"
```

## Safety Guardrails

1. **Non-production restriction**: Running `seed_demo_data` with `DEBUG=False` without `--force` is immediately refused (`CommandError`) with zero database writes.
2. **Fail-closed non-debug password**:
   - When `DEBUG=True`, newly created demo users default to `DemoPass123!` unless `--password` or the `DEMO_SEED_PASSWORD` environment variable is supplied.
   - When `DEBUG=False` with `--force`, an explicit `--password` or `DEMO_SEED_PASSWORD` (at least 8 characters) is **required**; omitting it fails closed.
3. **Password Configuration & CLI Output**:
   - Newly created demo accounts resolve their password via `--password`, then `DEMO_SEED_PASSWORD`, then the `DEBUG=True` dev fallback `DemoPass123!`.
   - CLI summary output reports `Password for newly created accounts` matching that exact resolved password (never hardcoding `DemoPass123!` when an override is active).
4. **Strict demo-workspace ownership check on `--reset` and re-runs**:
   - Demo workspaces use deterministic `demo-<pack-key>` slugs (e.g., `demo-general-services`) and `@demo.jojocrm.gh` accounts.
   - Before modifying or deleting an existing workspace matching a demo slug, `_assert_safe_demo_workspace()` verifies that `workspace.created_by` and all workspace Owner users belong to `@demo.jojocrm.gh`. Any colliding non-demo workspace is refused and never modified or deleted.
5. **Non-destructive idempotency**:
   - Plain re-runs (`seed_demo_data` without `--reset`) use `get_or_create` keyed on stable natural keys per workspace, preserving manual QA edits while filling in any missing baseline records.
   - `--reset` deletes only verified targeted demo workspaces (cascading their child rows) and recreates the clean baseline, invalidating all workspace cache domains (`ALL_WORKSPACE_DOMAINS`).

## Seeded Tenants & QA Coverage

All 20 business types from [`mainapp/catalog.py`](../mainapp/catalog.py) are seeded by default, each respecting its exact enabled tables and default Contact label (`Contact`, `Patient`, `Student or Guardian`, or `Donor or Member`).

### Headline QA Workspaces

| Business Type (`pack_key`) | Workspace Name | Slug | Contact Label | Seeded Accounts |
| --- | --- | --- | --- | --- |
| `general_services` | Kora Industrial Services | `demo-general-services` | Contact | `owner.general@demo.jojocrm.gh`, `staff.general@demo.jojocrm.gh`, `coord.general@demo.jojocrm.gh` (`Sales Coordinator`) |
| `retail_trading` | Osu Market & Trading Co. | `demo-retail-trading` | Contact | `owner.retail@demo.jojocrm.gh`, `staff.retail@demo.jojocrm.gh`, `coord.retail@demo.jojocrm.gh` (`Sales Coordinator`) |
| `healthcare_wellness` | Cantonments Care Clinic | `demo-healthcare-wellness` | Patient | `owner.health@demo.jojocrm.gh`, `staff.health@demo.jojocrm.gh`, `coord.health@demo.jojocrm.gh` (`Front Desk Coordinator`) |
| `financial_services` | Accra Ridge Advisory | `demo-financial-services` | Contact | `owner.finance@demo.jojocrm.gh`, `staff.finance@demo.jojocrm.gh`, `coord.finance@demo.jojocrm.gh` (`Sales Coordinator`) |

The remaining 16 business types follow the same `demo-<pack-key-hyphenated>` slug convention and `owner.<short>@demo.jojocrm.gh` / `staff.<short>@demo.jojocrm.gh` verified accounts (listed by the CLI output upon execution).

### Pre-Seeded QA Scenarios

- **Deals & Pipeline charts**: Every Deal-enabled workspace seeds Deals spanning all 5 starter pipeline stages (`New`, `Qualified`, `Proposal`, `Won`, `Lost`) with GHS amounts, `target_close_date`, `win_probability`, and `estimated_margin`.
- **Lead conversion & duplicate detection**: Every Lead-enabled workspace includes open leads (including one matching an existing `Account` name for duplicate-reuse QA) plus one already-converted Lead aligned to its converted `Account`, `Contact`, and optional `Deal` (when `deal` is enabled).
- **Healthcare workflow**: `demo-healthcare-wellness` labels `Contact` as `Patient`, keeps `Deal` disabled, seeds `Job` records linked to `Account` records, and seeds `Appointment`s plus `Visit` records with `date`, `reason`, `notes`, and `follow_up`.
- **Roles, field permissions & sharing**: Headline workspaces include starter roles plus a cloned QA role with hidden/read-only fields (e.g., `estimated_margin` hidden on `Deal`, `notes` hidden on `Visit`) and records spanning `workspace`, `role`, and `owner` visibility.
- **Activities & Audit history**: Core `Activity` entries (`note`, `task` including overdue & upcoming, `call`, `meeting`) and `AuditLog` entries (`field_name='record'` / `'role'`, including Deal stage progressions) populate timelines and the Audit Log drawer immediately.

## Sprint 3–5 Extension Hook

Future stories (invoices, payments, stock, warehouses, custom dashboard widgets) can extend `seed_demo_data` without modifying the core seeder by registering callbacks in [`mainapp/service/demo_seed_service.py`](../mainapp/service/demo_seed_service.py):

```python
from mainapp.service.demo_seed_service import register_demo_seed_extension

def seed_sprint3_invoicing(ctx):
    if 'invoice' not in ctx.enabled_tables:
        return {}
    # Create idempotent invoices/payments/stock for ctx.workspace
    return {'invoices': 3}

register_demo_seed_extension('sprint3_invoicing', seed_sprint3_invoicing)
```

## Automated Tests

- Backend suite: [`mainapp/tests/test_demo_seed_data.py`](../mainapp/tests/test_demo_seed_data.py)
