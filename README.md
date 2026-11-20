# jojoCRM
AI-enhanced Multi-tenant CRM

Product requirements: [docs/PRD.md](docs/PRD.md).

Project scope: [docs/SCOPE.md](docs/SCOPE.md), [docs/SOW.md](docs/SOW.md), and [docs/BACKLOG.md](docs/BACKLOG.md).

Story documentation:
- [docs/S1_1_GHANA_WORKSPACE.md](docs/S1_1_GHANA_WORKSPACE.md) — S1.1 Ghana workspace registration and connection-location check.
- [docs/S1_2_EMAIL_AND_PASSWORD.md](docs/S1_2_EMAIL_AND_PASSWORD.md) — S1.2 email verification, password sign-in, and password reset.
- [docs/S1_3_ADDITIONAL_SIGNIN.md](docs/S1_3_ADDITIONAL_SIGNIN.md) — S1.3 Google OIDC, Microsoft OIDC, and WebAuthn passkeys.
- [docs/S1_4_INVITES.md](docs/S1_4_INVITES.md) — S1.4 workspace invitations, single-workspace enforcement, and location-free acceptance/sign-in.
- [docs/S1_5_OWNER_STAFF_AND_AUDIT.md](docs/S1_5_OWNER_STAFF_AND_AUDIT.md) — S1.5 Owner and Staff roles, member deactivation and reactivation, session recovery, account settings, and audit logging.
- [docs/S2_0_BUSINESS_TYPE_AND_PACKS.md](docs/S2_0_BUSINESS_TYPE_AND_PACKS.md) — S2.0 20-type workspace catalog, starter packs, Contact labels, empty-table protection, and URL pane/subtab persistence.
- [docs/S2_1_CORE_AND_PACK_OBJECTS.md](docs/S2_1_CORE_AND_PACK_OBJECTS.md) — S2.1 Account, Contact, Lead, Deal, Activity, Job, Appointment, and Ticket models, CRUD APIs, role permissions matrix, and workspace panes.
- [docs/S2_2_LEAD_CONVERSION.md](docs/S2_2_LEAD_CONVERSION.md) — S2.2 Lead-to-Deal conversion, Account/Contact match detection and reuse, double-conversion protection, converted Lead locks, and pack gating.
- [docs/S2_3_PIPELINE.md](docs/S2_3_PIPELINE.md) — S2.3 Editable sales pipeline stages, stage reordering and removal with deal reassignment, interactive Deals Kanban board, and Healthcare pack gating.
- [docs/S2_4_ROLES_AND_SHARING.md](docs/S2_4_ROLES_AND_SHARING.md) — S2.4 Custom role cloning, field-level permissions, and record sharing.
- [docs/S2_5_SEARCH_AND_AUDIT.md](docs/S2_5_SEARCH_AND_AUDIT.md) — S2.5 Global workspace search, URL record deep-linking, and scoped audit history.
- [docs/S2_6_VISIT.md](docs/S2_6_VISIT.md) — S2.6 Healthcare Visit table, Patient-linked records, and follow-up tracking.
- [docs/S2_7_RESPONSE_AND_WORKSPACE_CACHING.md](docs/S2_7_RESPONSE_AND_WORKSPACE_CACHING.md) — S2.7 HTTP/Redis response & workspace caching with immediate write invalidation and frontend lookup deduplication.
- [docs/S2_8_DEMO_SEED_DATA.md](docs/S2_8_DEMO_SEED_DATA.md) — S2.8 Idempotent non-production demo seed management command (`manage.py seed_demo_data`) across all 20 business types.
