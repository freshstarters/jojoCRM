# S2.3 Pipeline

Implemented in `/home/demiz/Projects/jojo_crm_venv/jojoCRM` and `/home/demiz/Projects/jojoCRM-frontend`.

## Overview

S2.3 introduces editable sales pipelines and ordered stages for workspaces with the `Deal` table enabled (such as `general_services`), **Settings -> Pipelines** management controls for authorized roles, an interactive drag-and-drop and keyboard-accessible **Deals & Pipeline** Kanban board, dynamic stage validation across Deal CRUD and Lead conversion, and strict pack gating so packs without `Deal` (such as `healthcare_wellness`) have no pipeline or Kanban board.

## Data Model and Migrations

Added in [`mainapp/models/crm.py`](../mainapp/models/crm.py) (re-exported in [`mainapp/models/__init__.py`](../mainapp/models/__init__.py)):

- **`Pipeline`**:
  - Workspace-scoped sales pipeline (`workspace`, `name` defaulting to `'Sales Pipeline'`, `is_default`, timestamps).
  - Enforces `unique_default_pipeline_per_workspace` and case-insensitive `unique_pipeline_name_ci_per_workspace`.
- **`PipelineStage`**:
  - Ordered stages (`pipeline`, `workspace`, `name`, 1-based `position`, semantic `tone` token from 10 choices: `neutral`, `info`, `indigo`, `violet`, `teal`, `success`, `warn`, `orange`, `danger`, `pink`).
  - Default starter seed on workspaces with `Deal` enabled: `New` (`neutral`), `Qualified` (`info`), `Proposal` (`warn`), `Won` (`success`), `Lost` (`danger`).
  - Enforces case-insensitive `unique_stage_name_ci_per_pipeline` and indexes `(pipeline, position)`.
- **`Deal` extensions**:
  - `pipeline` and `pipeline_stage` (`SET_NULL`, nullable) alongside `stage`, which stays synchronized with `pipeline_stage.name`.
- **Migrations**:
  - [`mainapp/migrations/0012_pipeline_deal_pipeline_pipelinestage_and_more.py`](../mainapp/migrations/0012_pipeline_deal_pipeline_pipelinestage_and_more.py): Schema migration creating `Pipeline`, `PipelineStage`, and `Deal` foreign keys.
  - [`mainapp/migrations/0013_backfill_workspace_pipelines.py`](../mainapp/migrations/0013_backfill_workspace_pipelines.py): Data migration seeding the default `Sales Pipeline` and 5 starter stages for existing `Deal`-enabled workspaces and linking existing `Deal` rows.
  - [`mainapp/migrations/0014_alter_pipelinestage_tone.py`](../mainapp/migrations/0014_alter_pipelinestage_tone.py): Expands `PipelineStage.tone` choices to all 10 stage tones.

## Permissions, Services, and API Endpoints

- **Sales Settings Permission (`user_can_manage_sales_settings` in [`mainapp/service/role_service.py`](../mainapp/service/role_service.py))**:
  - Granted to verified active workspace **Owner** or roles holding the `theme_and_layouts` or `users_and_roles` switch; serialized as `permissions.can_manage_sales_settings` in [`mainapp/serializers.py`](../mainapp/serializers.py).
- **Pipeline Service ([`mainapp/service/pipeline_service.py`](../mainapp/service/pipeline_service.py))**:
  - `ensure_workspace_pipeline`: Requires `Deal` enabled (`404` otherwise); seeds the default pipeline and 5 starter stages on signup (`auth_views.py`), when `Deal` is turned on (`catalog_service.py`), or on first pipeline access.
  - `rename_workspace_pipeline`, `add_pipeline_stage`, `update_pipeline_stage`, `reorder_pipeline_stages`, `remove_pipeline_stage`: Enforce `user_can_manage_sales_settings` (`403` otherwise) and record `AuditLog` entries for `pipeline`, `pipeline_stage`, and affected `deal` records.
  - Renaming a stage bulk-updates `Deal.stage` (matching both `pipeline_stage` and legacy unlinked `stage__iexact=old_name` rows) and writes per-deal `stage` `AuditLog` entries alongside the `pipeline_stage.name` audit entry.
  - Removing a stage refuses removing the final stage (`400`), requires `move_to_stage_id` pointing to another stage in the same workspace pipeline whenever deals exist on the stage (`400` if missing/invalid), moves all deals to the chosen stage, writes per-deal `stage` `AuditLog` entries, and re-sequences remaining stage positions `1..N`.
- **Dynamic Deal, Audit & Lead Conversion Integration ([`mainapp/service/crm_service.py`](../mainapp/service/crm_service.py), [`mainapp/service/lead_conversion_service.py`](../mainapp/service/lead_conversion_service.py), [`mainapp/views/workspace_views.py`](../mainapp/views/workspace_views.py))**:
  - `create_crm_record`, `update_crm_record`, and `convert_lead_record` validate stages against live `PipelineStage` rows (`resolve_deal_stage`, falling back cleanly to the first active stage `stages[0]` whenever `Qualified` has been renamed or removed), leaving `win_probability` untouched when moving a Deal across stages.
  - `get_deals_board` returns ordered columns with per-stage bounded Deal slices (default `100` deals per column) and exact SQL column counts/totals so busy closed columns never starve other stages.
  - `get_crm_options`, `_build_deal_stage_summary`, `get_workspace_dashboard_summary`, and `get_lead_conversion_preview` return live ordered workspace stages, tones, and `pipeline.name`.
  - `workspace_audit_view` excludes CRM/pipeline audit entries for objects where a non-Owner/non-admin caller lacks `read` permission.
- **Endpoints ([`mainapp/views/pipeline_views.py`](../mainapp/views/pipeline_views.py), [`mainapp/views/crm_views.py`](../mainapp/views/crm_views.py), [`mainapp/urls.py`](../mainapp/urls.py))**:
  - `GET / PATCH /api/v1/workspaces/<slug>/pipelines/`
  - `POST /api/v1/workspaces/<slug>/pipelines/stages/`
  - `PATCH / PUT / DELETE /api/v1/workspaces/<slug>/pipelines/stages/<stage_id>/`
  - `PUT / POST /api/v1/workspaces/<slug>/pipelines/stages/reorder/`
  - `GET /api/v1/workspaces/<slug>/deals/board/`

## Frontend Implementation (`jojoCRM-frontend`)

- **Service & Navigation**:
  - [`src/service/crmService.js`](file:///home/demiz/Projects/jojoCRM-frontend/src/service/crmService.js): Adds pipeline/board API helpers, `canUserManageSalesSettings`, `getStageBadgeClass`, and `getStageColor`.
  - [`src/utils/catalog.js`](file:///home/demiz/Projects/jojoCRM-frontend/src/utils/catalog.js): Adds `getSettingsTabs(workspace)` exposing the **Pipelines** subtab only when `deal` is enabled (hidden on Healthcare).
- **Stage Management ([`src/components/SettingsPipelinesSection.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/SettingsPipelinesSection.jsx), [`src/components/Select.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/Select.jsx), [`src/screens/SettingsPane.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/screens/SettingsPane.jsx))**:
  - Rename the workspace pipeline, add stages across all 10 semantic tones with color-swatch `<Select>` previews and live stage badge previews, inline-edit stage names/tones, reorder via drag-and-drop or Up/Down buttons with **Save order** / **Discard**, and remove stages with a required destination-stage picker modal whenever deals exist on the removed stage.
  - `.custom-select-portal` in [`src/index.css`](file:///home/demiz/Projects/jojoCRM-frontend/src/index.css) uses frosted translucent surfaces with `backdrop-filter: blur(18px)` in both light and dark modes, and pressing `Escape` inside `<Select>` isolates dismissal from parent `<Modal>` dialogs.
- **Interactive Kanban & Dynamic Charts**:
  - [`src/components/crm/DealKanbanBoard.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/crm/DealKanbanBoard.jsx) & [`src/screens/DealsPane.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/screens/DealsPane.jsx): Drag-and-drop across ordered stage columns (`.kanban-col.drag-over` in [`src/index.css`](file:///home/demiz/Projects/jojoCRM-frontend/src/index.css)), keyboard-accessible previous/next stage buttons on cards, optimistic updates with error rollback, debounced search, **Edit Stages** shortcut for authorized roles, accurate Board/Table counts and pipeline title, Table-only stage filtering, and per-stage overflow links into the Table view.
  - [`src/components/crm/PipelineStageDistribution.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/crm/PipelineStageDistribution.jsx), [`src/components/crm/ConvertLeadModal.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/crm/ConvertLeadModal.jsx), [`src/screens/DashboardPane.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/screens/DashboardPane.jsx), and [`src/components/AuditLogDrawer.jsx`](file:///home/demiz/Projects/jojoCRM-frontend/src/components/AuditLogDrawer.jsx): Drive stage options (`warn` aligned to Amber, matching badges and charts), badges, chart segments, `stages[0]` lead-conversion fallback, and `Pipeline` / `Pipeline stage` audit labels from live workspace pipeline data.

## Automated Tests

Backend tests live in [`mainapp/tests/test_pipeline.py`](../mainapp/tests/test_pipeline.py), covering default General services stages, role permission gating, pipeline renaming, expanded stage tones (`indigo`, `violet`, `teal`, `orange`, `pink`), stage addition/renaming (including legacy unlinked deals)/reordering, stage removal with mandatory deal reassignment and last-stage protection, Deal stage moves without mutating `win_probability`, per-stage Kanban board isolation, dynamic `stages[0]` Lead conversion fallback when `Qualified` is renamed or removed, `pipeline.name` in summaries/options, audit object-read scoping (`deal.read = False`), Healthcare `404` pack gating, and cross-workspace isolation.
