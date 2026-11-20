"""Idempotent non-production demo seed service for manual QA across all 20 business types."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..catalog import BUSINESS_TYPES, BUSINESS_TYPES_BY_KEY, BusinessTypeDefinition
from ..models import (
    Account,
    Activity,
    Appointment,
    AuditLog,
    Contact,
    Deal,
    Job,
    Lead,
    Role,
    Ticket,
    User,
    Visit,
    Workspace,
    WorkspaceRecordBase,
    WorkspaceTable,
)
from .audit_service import record_audit_event
from .cache_service import ALL_WORKSPACE_DOMAINS, invalidate_workspace_cache
from .catalog_service import ensure_workspace_catalog, initialize_workspace_catalog
from .pipeline_service import ensure_workspace_pipeline, get_ordered_pipeline_stages
from .role_service import (
    FIELD_RIGHT_HIDDEN,
    FIELD_RIGHT_READ_ONLY,
    STAFF_ROLE_DEFAULTS,
    build_default_field_rights,
    build_default_object_rights,
    ensure_workspace_default_roles,
)

DEMO_EMAIL_DOMAIN = '@demo.jojocrm.gh'
DEFAULT_DEV_DEMO_PASSWORD = 'DemoPass123!'
DEMO_PASSWORD_ENV_VAR = 'DEMO_SEED_PASSWORD'

HEADLINE_QA_PACK_KEYS: tuple[str, ...] = (
    'general_services',
    'retail_trading',
    'healthcare_wellness',
    'financial_services',
)


class DemoSeedError(Exception):
    """Domain error raised when demo seeding is refused or misconfigured."""


@dataclass(frozen=True)
class DemoTenantProfile:
    """Identity and Ghanaian domain flavor for one business-type demo workspace."""

    pack_key: str
    slug: str
    workspace_name: str
    short_code: str
    city: str
    accounts: tuple[tuple[str, str, str, str, str], ...]
    contacts: tuple[tuple[str, str, str, str, bool, int | None], ...]
    open_leads: tuple[tuple[str, str, str, str, bool, str], ...]
    converted_lead: tuple[str, str, str, str, bool, str]
    deals: tuple[tuple[str, str, str, int, int, str, int, int | None], ...]
    jobs: tuple[tuple[str, str, int, int, int | None, str], ...]
    appointments: tuple[tuple[str, str, int, int, str], ...]
    visits: tuple[tuple[int, int, str, str, str], ...]
    tickets: tuple[tuple[str, str, str, int, int | None, str], ...]
    custom_role_name: str | None = None
    custom_role_hidden_fields: tuple[tuple[str, str], ...] = ()
    custom_role_readonly_fields: tuple[tuple[str, str], ...] = ()


@dataclass
class DemoSeedContext:
    """Context handed to Sprint 3-5 demo seed extensions for one seeded workspace."""

    workspace: Workspace
    business_type: BusinessTypeDefinition
    owner: User
    staff: User
    coordinator: User | None
    owner_role: Role
    staff_role: Role
    custom_role: Role | None
    enabled_tables: frozenset[str]
    accounts: list[Account]
    contacts: list[Contact]
    leads: list[Lead]
    deals: list[Deal]
    jobs: list[Job]
    appointments: list[Appointment]
    visits: list[Visit]
    tickets: list[Ticket]
    reset: bool


@dataclass
class SeededTenantSummary:
    """Summary returned per workspace after seeding."""

    pack_key: str
    business_type_label: str
    workspace_name: str
    workspace_slug: str
    contact_label: str
    created_workspace: bool
    reset_performed: bool
    owner_email: str
    staff_email: str
    coordinator_email: str | None
    counts: dict[str, int] = field(default_factory=dict)
    extension_counts: dict[str, dict[str, int]] = field(default_factory=dict)


_DEMO_SEED_EXTENSIONS: dict[str, Callable[[DemoSeedContext], dict[str, int] | None]] = {}


def register_demo_seed_extension(
    name: str,
    callback: Callable[[DemoSeedContext], dict[str, int] | None],
) -> None:
    """Register a Sprint 3-5 demo seed extension invoked once per seeded workspace."""
    cleaned = (name or '').strip()
    if not cleaned:
        raise ValueError('Extension name must be a non-empty string.')
    _DEMO_SEED_EXTENSIONS[cleaned] = callback


def unregister_demo_seed_extension(name: str) -> None:
    """Remove a previously registered demo seed extension if present."""
    _DEMO_SEED_EXTENSIONS.pop((name or '').strip(), None)


def list_demo_seed_extensions() -> tuple[str, ...]:
    """Return registered demo seed extension names in registration order."""
    return tuple(_DEMO_SEED_EXTENSIONS.keys())


def _demo_slug(pack_key: str) -> str:
    return f"demo-{pack_key.replace('_', '-')}"


def _build_generic_profile(
    *,
    pack_key: str,
    workspace_name: str,
    short_code: str,
    city: str,
    sector_noun: str,
    company_names: tuple[str, str, str],
    person_names: tuple[tuple[str, str], tuple[str, str], tuple[str, str], tuple[str, str]],
    cities: tuple[str, str, str],
    phone_prefix: str,
) -> DemoTenantProfile:
    """Build a realistic Ghanaian tenant profile tailored to a catalog business type."""
    p0, p1, p2, p3 = person_names
    c0, c1, c2 = company_names
    ct0, ct1, ct2 = cities
    domain_base = f'{short_code}.demo.gh'

    accounts = (
        (
            c0,
            ct0,
            f'+233 {phone_prefix} 110 201',
            f'info@{short_code}-north.gh',
            f'Key {sector_noun} account in {ct0}.',
        ),
        (
            c1,
            ct1,
            f'+233 {phone_prefix} 220 302',
            f'ops@{short_code}-central.gh',
            f'Regional {sector_noun} partner operating out of {ct1}.',
        ),
        (
            c2,
            ct2,
            f'+233 {phone_prefix} 330 403',
            f'accounts@{short_code}-west.gh',
            f'Growth {sector_noun} client in {ct2}.',
        ),
    )
    contacts = (
        (p0[0], p0[1], f'c1.{short_code}@{domain_base}', '+233 24 410 1001', True, 0),
        (p1[0], p1[1], f'c2.{short_code}@{domain_base}', '+233 20 520 2002', False, 1),
        (p2[0], p2[1], f'c3.{short_code}@{domain_base}', '+233 55 630 3003', True, 2),
        (p3[0], p3[1], f'c4.{short_code}@{domain_base}', '+233 50 740 4004', True, 0),
    )
    open_leads = (
        (
            f'Nana Yaw Boateng ({short_code.title()})',
            c0,
            f'lead1.{short_code}@{domain_base}',
            '+233 24 811 5101',
            True,
            f'Inbound {sector_noun} inquiry matching existing account {c0} for duplicate-match testing.',
        ),
        (
            f'Efua Mensah ({short_code.title()})',
            f'{ct1} Horizon Ventures',
            f'lead2.{short_code}@{domain_base}',
            '+233 20 922 5202',
            False,
            f'Referred {sector_noun} prospect requesting GHS quotation.',
        ),
    )
    converted_lead = (
        f'Kojo Appiah ({short_code.title()})',
        c1,
        f'lead.conv.{short_code}@{domain_base}',
        '+233 55 333 5303',
        True,
        f'Converted {sector_noun} lead linked to account, contact, and active pipeline.',
    )
    deals = (
        (f'{c0} — Q2 {sector_noun.title()} Expansion', 'New', '18500.00', 20, 21, '3700.00', 0, 0),
        (f'{c1} — Annual {sector_noun.title()} Contract', 'Qualified', '42000.00', 45, 30, '9800.00', 1, 1),
        (f'{c2} — {ct2} Rollout Package', 'Proposal', '64500.00', 70, 14, '16200.00', 2, 2),
        (f'{c0} — Priority Retainer Renewal', 'Won', '31200.00', 100, -5, '8400.00', 0, 3),
        (f'{ct1} Pilot Bid ({sector_noun.title()})', 'Lost', '15000.00', 0, -12, '2900.00', 1, 1),
    )
    jobs = (
        (f'{c0} onboarding & site setup', 'In Progress', 4, 0, 0, f'Coordinating {sector_noun} deliverables in {ct0}.'),
        (f'{c1} quarterly inspection & handover', 'Pending', 9, 1, 1, f'Scheduled operational review in {ct1}.'),
        (f'{c2} baseline audit sign-off', 'Completed', -3, 2, 2, f'Completed initial {sector_noun} checklist.'),
    )
    appointments = (
        (f'{sector_noun.title()} discovery review with {p0[0]}', 'Confirmed', 2, 0, f'Reviewing scope and GHS pricing in {city}.'),
        (f'Follow-up check-in with {p1[0]}', 'Scheduled', 5, 1, 'Confirming timeline and team assignments.'),
        (f'Onboarding kickoff with {p2[0]}', 'Completed', -4, 2, 'Initial requirements captured and agreed.'),
    )
    visits = (
        (0, -2, f'Routine {sector_noun} consultation', 'Reviewed status and updated profile.', 'Follow up in 14 days with summary.'),
    )
    tickets = (
        (f'Billing clarification for {c0}', 'Open', 'High', 0, 0, f'Customer requested itemized GHS breakdown for {sector_noun} order.'),
        (f'Account access update for {p1[0]}', 'Pending', 'Normal', 1, 1, 'Awaiting confirmation on primary contact details.'),
        (f'Delivery receipt request — {c2}', 'Resolved', 'Low', 2, 2, 'Signed confirmation shared with operations team.'),
    )
    return DemoTenantProfile(
        pack_key=pack_key,
        slug=_demo_slug(pack_key),
        workspace_name=workspace_name,
        short_code=short_code,
        city=city,
        accounts=accounts,
        contacts=contacts,
        open_leads=open_leads,
        converted_lead=converted_lead,
        deals=deals,
        jobs=jobs,
        appointments=appointments,
        visits=visits,
        tickets=tickets,
    )


def _build_all_profiles() -> dict[str, DemoTenantProfile]:
    """Return curated Ghanaian demo profiles for all 20 catalog business types."""
    profiles: list[DemoTenantProfile] = [
        # 1. Headline QA Pack: General services
        DemoTenantProfile(
            pack_key='general_services',
            slug=_demo_slug('general_services'),
            workspace_name='Kora Industrial Services',
            short_code='general',
            city='Accra',
            accounts=(
                (
                    'Volta Meridian Engineering Ltd',
                    'Accra',
                    '+233 30 278 4410',
                    'procurement@voltameridian.gh',
                    'Industrial HVAC and power backup client across Airport Residential and Tema.',
                ),
                (
                    'Ashanti Timber & Fabrication Co.',
                    'Kumasi',
                    '+233 32 204 8821',
                    'facilities@ashantitimber.gh',
                    'Multi-site workshop requiring preventive maintenance SLA.',
                ),
                (
                    'Sekondi Marine Support Services',
                    'Takoradi',
                    '+233 31 202 3319',
                    'ops@sekondimarine.gh',
                    'Harbour equipment servicing account with quarterly inspections.',
                ),
                (
                    'Tema ColdChain Warehousing',
                    'Tema',
                    '+233 30 321 9045',
                    'admin@temacoldchain.gh',
                    'Cold-room electrical and generator upkeep account.',
                ),
            ),
            contacts=(
                ('Kwame Mensah', 'Head of Facilities', 'kwame.mensah@voltameridian.gh', '+233 24 456 7101', True, 0),
                ('Ama Boateng', 'Operations Manager', 'ama.boateng@ashantitimber.gh', '+233 20 812 3402', False, 1),
                ('Fiifi Quansah', 'Port Logistics Lead', 'fiifi.quansah@sekondimarine.gh', '+233 55 319 8803', True, 2),
                ('Abena Osei', 'Procurement Officer', 'abena.osei@temacoldchain.gh', '+233 50 644 2104', True, 3),
                ('Selorm Adjei', 'Plant Engineer', 'selorm.adjei@voltameridian.gh', '+233 24 778 9905', False, 0),
            ),
            open_leads=(
                (
                    'Yaw Darko',
                    'Volta Meridian Engineering Ltd',
                    'yaw.darko@voltameridian.gh',
                    '+233 24 511 4020',
                    True,
                    'Referred by Kwame Mensah for standby generator overhaul; matches existing Account for duplicate-reuse QA.',
                ),
                (
                    'Esi Hammond',
                    'Osu Commercial Towers',
                    'esi.hammond@osutowers.gh',
                    '+233 20 744 6190',
                    True,
                    'Requested annual lift and electrical preventive maintenance quote in GHS.',
                ),
                (
                    'Ibrahim Fuseini',
                    'Northern Grain Silos Ltd',
                    'ibrahim.fuseini@northernsilos.gh',
                    '+233 55 901 1280',
                    False,
                    'Interested in solar-hybrid backup service contract in Tamale.',
                ),
            ),
            converted_lead=(
                'Abena Osei',
                'Tema ColdChain Warehousing',
                'abena.osei.lead@temacoldchain.gh',
                '+233 50 644 2104',
                True,
                'Converted after site walk-through at Tema Community 1 depot.',
            ),
            deals=(
                ('Tema ColdChain — Compressor Upgrade', 'New', '28500.00', 25, 28, '6400.00', 3, 3),
                ('Volta Meridian — Annual HVAC SLA', 'Qualified', '74000.00', 50, 18, '19500.00', 0, 0),
                ('Ashanti Timber — Workshop Rewiring', 'Proposal', '118500.00', 75, 10, '31000.00', 1, 1),
                ('Sekondi Marine — Dockside Generator Service', 'Proposal', '52000.00', 65, 15, '13800.00', 2, 2),
                ('Volta Meridian — Airport Annex Commissioning', 'Won', '96000.00', 100, -6, '26400.00', 0, 4),
                ('Cape Coast Resort — Chiller Bid', 'Lost', '41000.00', 0, -14, '9200.00', 2, 2),
            ),
            jobs=(
                ('Preventive HVAC servicing — Airport Annex', 'In Progress', 3, 0, 0, 'Replace filter banks and test three-phase load transfer.'),
                ('Standby generator load test — Tema Depot', 'Pending', 7, 3, 3, 'Coordinate 2-hour shutdown window with Abena Osei.'),
                ('Switchgear thermal scan — Kumasi Mill', 'Pending', 11, 1, 1, 'Field team travel scheduled from Accra.'),
                ('Harbour crane electrical audit', 'Completed', -4, 2, 2, 'Audit report signed off and delivered to Sekondi Marine.'),
            ),
            appointments=(
                ('SLA renewal walk-through with Kwame Mensah', 'Confirmed', 2, 0, 'Airport Residential HQ, 10:00 GMT.'),
                ('Proposal review call with Ama Boateng', 'Scheduled', 4, 1, 'Walk through phased Kumasi rewiring schedule.'),
                ('Cold-room commissioning check with Abena Osei', 'Completed', -3, 3, 'Confirmed telemetry alarms and sign-off checklist.'),
            ),
            visits=(),
            tickets=(
                ('Chiller unit #2 tripping breaker at Airport Annex', 'Open', 'High', 0, 0, 'Voltage fluctuation logged after afternoon grid switchover.'),
                ('Invoice copy needed for Q1 maintenance visit', 'Pending', 'Normal', 1, 1, 'Finance requested VAT breakdown for Kumasi branch.'),
                ('Add secondary emergency phone contact', 'New', 'Low', 3, 3, 'Update dispatch contact sheet for night shift.'),
                ('Generator oil leak inspection completed', 'Resolved', 'Normal', 2, 2, 'Replaced gasket and verified clean pressure test.'),
            ),
            custom_role_name='Sales Coordinator',
            custom_role_hidden_fields=(('deal', 'estimated_margin'),),
            custom_role_readonly_fields=(('deal', 'win_probability'),),
        ),
        # 2. Headline QA Pack: Retail and trading
        DemoTenantProfile(
            pack_key='retail_trading',
            slug=_demo_slug('retail_trading'),
            workspace_name='Osu Market & Trading Co.',
            short_code='retail',
            city='Accra',
            accounts=(
                (
                    'Makola Wholesale Mart',
                    'Accra',
                    '+233 30 266 5412',
                    'buying@makolamart.gh',
                    'High-volume consumer staples and household goods buyer.',
                ),
                (
                    'Kejetia Superstores Ltd',
                    'Kumasi',
                    '+233 32 208 7740',
                    'orders@kejetiasuper.gh',
                    'Three retail outlets in central Kumasi with weekly restocks.',
                ),
                (
                    'East Legon Home & Living',
                    'Accra',
                    '+233 30 254 1198',
                    'merchandising@eastlegonhome.gh',
                    'Boutique kitchenware and appliances storefront.',
                ),
                (
                    'Takoradi Harbour Minimarts',
                    'Takoradi',
                    '+233 31 209 6610',
                    'stock@takoradiminimarts.gh',
                    'Coastal convenience chain supplied from Tema warehouse.',
                ),
            ),
            contacts=(
                ('Akua Appiah', 'Head Buyer', 'akua.appiah@makolamart.gh', '+233 24 311 8801', True, 0),
                ('Kofi Asante', 'Branch Director', 'kofi.asante@kejetiasuper.gh', '+233 20 655 1202', False, 1),
                ('Adwoa Tetteh', 'Store Manager', 'adwoa.tetteh@eastlegonhome.gh', '+233 55 842 9903', True, 2),
                ('Mawuli Dzidzornu', 'Inventory Lead', 'mawuli@takoradiminimarts.gh', '+233 50 412 3304', False, 3),
                ('Nana Ama Sarpong', 'Merchandising Analyst', 'nana.sarpong@makolamart.gh', '+233 24 990 1405', True, 0),
            ),
            open_leads=(
                (
                    'Daniel Ofori',
                    'Makola Wholesale Mart',
                    'daniel.ofori@makolamart.gh',
                    '+233 24 612 7001',
                    True,
                    'New category buyer for electronics; matches existing Makola Wholesale Mart account.',
                ),
                (
                    'Gifty Owusu',
                    'Madina Fresh & Goods',
                    'gifty.owusu@madinafresh.gh',
                    '+233 20 718 4502',
                    False,
                    'Exploring bulk beverage and dry-goods credit terms (no SMS consent yet).',
                ),
            ),
            converted_lead=(
                'Adwoa Tetteh',
                'East Legon Home & Living',
                'adwoa.lead@eastlegonhome.gh',
                '+233 55 842 9903',
                True,
                'Converted after showroom tasting and appliance catalog review.',
            ),
            deals=(
                ('Makola Mart — Festive Dry Goods Lot', 'New', '34000.00', 20, 25, '6100.00', 0, 0),
                ('Kejetia Superstores — Q2 Restock Agreement', 'Qualified', '89500.00', 50, 16, '17900.00', 1, 1),
                ('East Legon Home — Small Appliances Launch', 'Proposal', '62000.00', 70, 9, '15500.00', 2, 2),
                ('Takoradi Minimarts — Coastal Supply Order', 'Won', '47800.00', 100, -4, '10400.00', 3, 3),
                ('Spintex Pop-up Kiosk Supply', 'Lost', '19200.00', 0, -10, '3800.00', 0, 4),
            ),
            jobs=(
                ('Spintex to Kumasi pallet dispatch', 'In Progress', 2, 1, 1, 'Verify carton counts before loading Tema van.'),
                ('Osu showroom seasonal display reset', 'Pending', 6, 2, 2, 'Coordinate branded end-cap stands with Adwoa Tetteh.'),
                ('Makola quarterly stock reconciliation', 'Completed', -2, 0, 0, 'Closed zero-variance stock count.'),
            ),
            appointments=(),
            visits=(),
            tickets=(),
            custom_role_name='Sales Coordinator',
            custom_role_hidden_fields=(('deal', 'estimated_margin'),),
            custom_role_readonly_fields=(('deal', 'win_probability'),),
        ),
        # 3. Headline QA Pack: Healthcare and wellness (Contact -> Patient, Visit enabled, Deal disabled)
        DemoTenantProfile(
            pack_key='healthcare_wellness',
            slug=_demo_slug('healthcare_wellness'),
            workspace_name='Cantonments Care Clinic',
            short_code='health',
            city='Accra',
            accounts=(
                (
                    'Ridge Corporate Health Plan',
                    'Accra',
                    '+233 30 276 3100',
                    'wellness@ridgecorp.gh',
                    'Employer corporate wellness sponsor covering 45 staff members.',
                ),
                (
                    'Labadi Family HMO Group',
                    'Accra',
                    '+233 30 277 8420',
                    'claims@labadihmo.gh',
                    'Private outpatient medical insurance partner.',
                ),
                (
                    'Tema Port Staff Welfare Scheme',
                    'Tema',
                    '+233 30 320 4490',
                    'welfare@temaport.gh',
                    'Annual occupational health screening sponsor.',
                ),
            ),
            contacts=(
                ('Mariama Alhassan', 'Outpatient — Corporate Plan', 'mariama.alhassan@patient.demo.gh', '+233 24 402 9101', True, 0),
                ('Yaw Osei-Tutu', 'Outpatient — Chronic Care', 'yaw.oseitutu@patient.demo.gh', '+233 20 518 7202', True, 1),
                ('Naa Ayeley Laryea', 'Outpatient — Wellness Subscription', 'naa.laryea@patient.demo.gh', '+233 55 629 3303', False, 0),
                ('Kojo Baffour', 'Outpatient — Occupational Check', 'kojo.baffour@patient.demo.gh', '+233 50 731 4404', True, 2),
                ('Beatrice Nkansah', 'Outpatient — Maternity Follow-up', 'beatrice.nkansah@patient.demo.gh', '+233 24 845 5505', True, None),
            ),
            open_leads=(
                (
                    'Samuel Kwarteng',
                    'Ridge Corporate Health Plan',
                    'samuel.kwarteng@ridgecorp.gh',
                    '+233 24 912 6601',
                    True,
                    'HR coordinator inquiring about executive screening packages; matches existing corporate account.',
                ),
                (
                    'Priscilla Addo',
                    'Airport Hills Physiotherapy',
                    'priscilla.addo@airporthills.gh',
                    '+233 20 823 7702',
                    True,
                    'Walk-in wellness membership inquiry for family care plan.',
                ),
            ),
            converted_lead=(
                'Mariama Alhassan',
                'Ridge Corporate Health Plan',
                'mariama.lead@patient.demo.gh',
                '+233 24 402 9101',
                True,
                'Converted from corporate screening registration to active Patient record.',
            ),
            deals=(),
            jobs=(
                ('Prepare Q2 clinic supply restock checklist', 'In Progress', 3, 0, 0, 'Verify storage readiness logs before pharmacy order.'),
                ('Compile Ridge Corporate wellness utilization summary', 'Pending', 8, 0, 0, 'Share anonymized attendance count with HR.'),
                ('Calibrate wellness check-in equipment in Rooms 2 & 3', 'Completed', -2, 0, None, 'Biomedical technician signed equipment service sheet for Ridge Corporate schedule.'),
            ),
            appointments=(
                ('Annual executive wellness check-in — Mariama Alhassan', 'Confirmed', 1, 0, 'Morning intake at 08:30 GMT; Room 1.'),
                ('Follow-up wellness review — Yaw Osei-Tutu', 'Scheduled', 3, 1, 'Bring personal logbook for scheduling check.'),
                ('Quarterly wellness plan check — Naa Ayeley Laryea', 'Scheduled', 6, 2, 'Nutrition & subscription benefits discussion.'),
                ('Occupational screening clearance — Kojo Baffour', 'Completed', -2, 3, 'Completed scheduled wellness screening clearance.'),
                ('Post-natal care check-in — Beatrice Nkansah', 'Confirmed', 2, 4, 'Routine 6-week wellness follow-up visit.'),
            ),
            visits=(
                (
                    0,
                    -5,
                    'Annual preventive wellness consultation',
                    'Patient completed annual check-in and updated emergency contact and corporate sponsor details.',
                    'Share summary sheet via email and schedule 6-month wellness review.',
                ),
                (
                    1,
                    -3,
                    'Routine chronic-care coordination visit',
                    'Reviewed ongoing attendance schedule and patient adherence goals with zero missed appointments.',
                    'Follow up in 4 weeks with next appointment slot confirmation.',
                ),
                (
                    3,
                    -2,
                    'Occupational wellness readiness review',
                    'Completed standard workplace wellness consultation and verified attendance documentation.',
                    'Send attendance clearance confirmation to Tema Port Welfare.',
                ),
                (
                    4,
                    -1,
                    'Routine maternal wellness consultation',
                    'Checked recovery schedule, support preferences, and next clinic visit availability.',
                    'Review again at next scheduled appointment in 2 weeks.',
                ),
            ),
            tickets=(
                ('Attendance clearance letter for Mariama Alhassan', 'Open', 'High', 0, 0, 'Patient requested stamped confirmation letter for travel scheduling.'),
                ('HMO pre-authorization code pending', 'Pending', 'Normal', 1, 1, 'Awaiting Labadi HMO confirmation for specialist referral.'),
                ('Update patient emergency phone number', 'Resolved', 'Low', 2, 3, 'Front desk verified and saved updated contact number.'),
            ),
            custom_role_name='Front Desk Coordinator',
            custom_role_hidden_fields=(('visit', 'notes'),),
            custom_role_readonly_fields=(('visit', 'follow_up'),),
        ),
        # 4. Headline QA Pack: Financial services
        DemoTenantProfile(
            pack_key='financial_services',
            slug=_demo_slug('financial_services'),
            workspace_name='Accra Ridge Advisory',
            short_code='finance',
            city='Accra',
            accounts=(
                (
                    'GoldCoast Microfinance Trust',
                    'Accra',
                    '+233 30 225 9100',
                    'treasury@goldcoastmfi.gh',
                    'Tier-2 microfinance institution managing SME credit portfolios.',
                ),
                (
                    'Kumasi Cooperative Credit Union',
                    'Kumasi',
                    '+233 32 203 5510',
                    'board@kumasicredit.gh',
                    'Member-owned credit union seeking treasury and policy advisory.',
                ),
                (
                    'Harbour View Insurance Brokers',
                    'Tema',
                    '+233 30 321 6780',
                    'underwriting@harbourbrokers.gh',
                    'Commercial marine and motor policy brokerage partner.',
                ),
            ),
            contacts=(
                ('Nana Agyemang', 'Chief Risk Officer', 'nana.agyemang@goldcoastmfi.gh', '+233 24 501 2201', True, 0),
                ('Efua Owusu', 'General Manager', 'efua.owusu@kumasicredit.gh', '+233 20 602 3302', True, 1),
                ('Kweku Baah', 'Managing Broker', 'kweku.baah@harbourbrokers.gh', '+233 55 703 4403', False, 2),
                ('Lydia Amoako', 'Compliance Lead', 'lydia.amoako@goldcoastmfi.gh', '+233 50 804 5504', True, 0),
            ),
            open_leads=(
                (
                    'Josephine Mensah',
                    'GoldCoast Microfinance Trust',
                    'josephine.mensah@goldcoastmfi.gh',
                    '+233 24 905 6601',
                    True,
                    'Treasury director requesting fixed-income advisory; matches existing GoldCoast account.',
                ),
                (
                    'Bernard Osei',
                    'Sunyani Farmers Credit Society',
                    'bernard.osei@sunyanicredit.gh',
                    '+233 20 415 7702',
                    False,
                    'Inquiring about group life policy administration and recurring billing.',
                ),
            ),
            converted_lead=(
                'Kweku Baah',
                'Harbour View Insurance Brokers',
                'kweku.lead@harbourbrokers.gh',
                '+233 55 703 4403',
                True,
                'Converted after brokerage compliance workshop in Tema.',
            ),
            deals=(
                ('GoldCoast MFI — Treasury Yield Mandate', 'New', '145000.00', 25, 30, '24000.00', 0, 0),
                ('Kumasi Credit Union — Group Policy Retainer', 'Qualified', '92000.00', 55, 20, '18500.00', 1, 1),
                ('Harbour Brokers — Commercial Risk Audit', 'Proposal', '128000.00', 75, 12, '29000.00', 2, 2),
                ('GoldCoast MFI — Annual Compliance Retainer', 'Won', '84000.00', 100, -7, '21000.00', 0, 3),
                ('Cape Coast Pension Advisory Tender', 'Lost', '65000.00', 0, -15, '14000.00', 1, 1),
            ),
            jobs=(
                ('Prepare Bank of Ghana quarterly compliance pack', 'In Progress', 5, 0, 3, 'Review advisory mandate schedules with Lydia Amoako.'),
                ('Draft credit union liquidity stress-test memo', 'Pending', 9, 1, 1, 'Finalize Q2 GHS interest rate scenarios.'),
                ('Reconcile marine policy renewal endorsements', 'Completed', -3, 2, 2, 'Delivered updated schedule to Harbour View Brokers.'),
            ),
            appointments=(
                ('Q2 portfolio review with Nana Agyemang', 'Confirmed', 2, 0, 'Ridge Tower Boardroom, 14:00 GMT.'),
                ('Policy renewal consultation with Efua Owusu', 'Scheduled', 5, 1, 'Virtual video call with Kumasi committee.'),
                ('Compliance sign-off briefing with Lydia Amoako', 'Completed', -4, 3, 'All statutory filings confirmed.'),
            ),
            visits=(),
            tickets=(
                ('Statement export discrepancy on March fee schedule', 'Open', 'High', 0, 0, 'Verify withholding tax calculation on advisory retainer.'),
                ('Add new authorized signatory to board pack', 'Pending', 'Normal', 1, 1, 'Waiting for stamped board resolution.'),
                ('Updated broker certificate uploaded', 'Resolved', 'Low', 2, 2, 'Verified expiry date extended through next year.'),
            ),
            custom_role_name='Sales Coordinator',
            custom_role_hidden_fields=(('deal', 'estimated_margin'),),
            custom_role_readonly_fields=(('deal', 'win_probability'),),
        ),
        # 5-20. Tailored Ghanaian profiles for the remaining 16 catalog business types
        _build_generic_profile(
            pack_key='wholesale_distribution',
            workspace_name='Tema Bulk Distribution Ltd',
            short_code='wholesale',
            city='Tema',
            sector_noun='wholesale distribution',
            company_names=('Achimota Depot Traders', 'Suame Bulk Hardware', 'Tamale Northern Stores'),
            person_names=(('Kwabena Osei', 'Procurement Head'), ('Abena Asare', 'Warehouse Manager'), ('Salifu Yakubu', 'Regional Buyer'), ('Doris Tetteh', 'Accounts Lead')),
            cities=('Accra', 'Kumasi', 'Tamale'),
            phone_prefix='30',
        ),
        _build_generic_profile(
            pack_key='agriculture_agribusiness',
            workspace_name='Techiman Agro & Produce Ltd',
            short_code='agri',
            city='Techiman',
            sector_noun='agribusiness',
            company_names=('Bono Cocoa & Inputs Coop', 'Afram Plains Grain Mills', 'Northern Shea Processors'),
            person_names=(('Kofi Bonsu', 'Coop Chairman'), ('Hajia Alima Sule', 'Mill Operations Lead'), ('Yaw Adomako', 'Agronomist'), ('Mercy Ofori', 'Supply Coordinator')),
            cities=('Sunyani', 'Koforidua', 'Tamale'),
            phone_prefix='35',
        ),
        _build_generic_profile(
            pack_key='food_beverage',
            workspace_name='Labone Kitchen & Roastery',
            short_code='food',
            city='Accra',
            sector_noun='food and beverage',
            company_names=('Osu Bistro Collective', 'Cantonments Event Catering', 'Airport Lounge Coffee Bar'),
            person_names=(('Maame Efua Dadzie', 'Executive Chef'), ('Kojo Mensah', 'Beverage Director'), ('Afi Kpodo', 'Events Manager'), ('Nii Lantey Oddoye', 'Purchasing Lead')),
            cities=('Accra', 'Tema', 'Cape Coast'),
            phone_prefix='30',
        ),
        _build_generic_profile(
            pack_key='education',
            workspace_name='Legon Crest Academy',
            short_code='edu',
            city='Accra',
            sector_noun='education',
            company_names=('East Legon Parent Association', 'Tema STEM Scholarship Fund', 'Kumasi Alumni Network'),
            person_names=(('Rev. Emmanuel Boateng', 'Parent / Guardian'), ('Dr. Akosua Manu', 'Sponsor Guardian'), ('Francis Adjei', 'Parent / Guardian'), ('Grace Osei', 'Admissions Liaison')),
            cities=('Accra', 'Tema', 'Kumasi'),
            phone_prefix='30',
        ),
        _build_generic_profile(
            pack_key='construction_real_estate',
            workspace_name='Airport City Estates & Build',
            short_code='build',
            city='Accra',
            sector_noun='construction and real estate',
            company_names=('Cantonments Skyline Developers', 'Spintex Concrete & Steel', 'Aburi Ridge Villas Ltd'),
            person_names=(('Eng. Kwasi Peprah', 'Project Director'), ('Vida Nartey', 'Quantity Surveyor'), ('Michael Ansah', 'Site Manager'), ('Faustina Quaye', 'Estate Broker')),
            cities=('Accra', 'Tema', 'Koforidua'),
            phone_prefix='30',
        ),
        _build_generic_profile(
            pack_key='transport_logistics_storage',
            workspace_name='Takoradi Corridor Logistics',
            short_code='logistics',
            city='Takoradi',
            sector_noun='transport and logistics',
            company_names=('Tema Freight Forwarders', 'Kumasi Inland Dry Port', 'Western Mining Haulage'),
            person_names=(('Capt. Francis Mensah', 'Fleet Director'), ('Mavis Amoah', 'Customs Lead'), ('Issahaku Moro', 'Dispatch Supervisor'), ('Nii Odartey', 'Warehouse Controller')),
            cities=('Tema', 'Kumasi', 'Takoradi'),
            phone_prefix='31',
        ),
        _build_generic_profile(
            pack_key='ict_digital_services',
            workspace_name='Spintex Cloud & Systems',
            short_code='ict',
            city='Accra',
            sector_noun='ICT and digital services',
            company_names=('Osu FinTech Innovations', 'Accra Digital Commerce Hub', 'Kumasi Tech Incubator'),
            person_names=(('Kobby Osei-Bonsu', 'CTO'), ('Dzifa Amegashie', 'Product Manager'), ('Richmond Boateng', 'IT Infrastructure Lead'), ('Sena Akakpo', 'Security Architect')),
            cities=('Accra', 'Kumasi', 'Tema'),
            phone_prefix='30',
        ),
        _build_generic_profile(
            pack_key='professional_services',
            workspace_name='Dzorwulu Legal & Consulting',
            short_code='pro',
            city='Accra',
            sector_noun='professional services',
            company_names=('Roman Ridge Holdings', 'Tema Industrial Park Tenants', 'Cape Coast Maritime Trust'),
            person_names=(('Nana Kofi Twum', 'Managing Partner'), ('Audrey Lomotey', 'Corporate Counsel'), ('Patrick Owusu', 'Finance Director'), ('Evelyn Boadu', 'Operations Partner')),
            cities=('Accra', 'Tema', 'Cape Coast'),
            phone_prefix='30',
        ),
        _build_generic_profile(
            pack_key='hospitality_tourism',
            workspace_name='Cape Coast Heritage Suites',
            short_code='hospitality',
            city='Cape Coast',
            sector_noun='hospitality and tourism',
            company_names=('Gold Coast Safari Tours', 'Accra Diaspora Travel Desk', 'Volta Lake Eco Resorts'),
            person_names=(('Ekow Sackey', 'Tour Director'), ('Akosua Agyapong', 'Group Reservations'), ('Delali Agbovi', 'Guest Experience Lead'), ('Philip Arthur', 'Corporate Travel Buyer')),
            cities=('Cape Coast', 'Accra', 'Ho'),
            phone_prefix='33',
        ),
        _build_generic_profile(
            pack_key='manufacturing_light_industry',
            workspace_name='Tema Light Plastics & Pack',
            short_code='mfg',
            city='Tema',
            sector_noun='light manufacturing',
            company_names=('Accra Bottling Works', 'Kasoa Food Packaging Co.', 'Kumasi Agro-Sacks Ltd'),
            person_names=(('Stephen Oduro', 'Plant Manager'), ('Comfort Addai', 'Quality Assurance Lead'), ('Tetteh Quarshie Jr.', 'Supply Chain Head'), ('Hannah Boateng', 'Production Planner')),
            cities=('Tema', 'Accra', 'Kumasi'),
            phone_prefix='30',
        ),
        _build_generic_profile(
            pack_key='telecom_airtime_services',
            workspace_name='Adom Fibre & Airtime Ltd',
            short_code='telecom',
            city='Accra',
            sector_noun='telecom and airtime',
            company_names=('Circle Mobile Dealers Union', 'Airport Business Park Fibre', 'Takoradi Offshore Comms'),
            person_names=(('Prince Asamoah', 'Channel Manager'), ('Joyce Mensah', 'Enterprise Accounts'), ('Abdul-Rahaman Seidu', 'Network Ops Lead'), ('Millicent Osei', 'Partner Support')),
            cities=('Accra', 'Takoradi', 'Tamale'),
            phone_prefix='30',
        ),
        _build_generic_profile(
            pack_key='nonprofit_faith_membership',
            workspace_name='Accra Youth Hope Foundation',
            short_code='nonprofit',
            city='Accra',
            sector_noun='community outreach',
            company_names=('Greater Accra Civic Partners', 'Volta Community Health Donors', 'Kumasi Skills Fellowship'),
            person_names=(('Pastor Samuel Osei', 'Lead Donor'), ('Amina Mohammed', 'Community Member'), ('Dr. Kofi Annan-Mensah', 'Board Patron'), ('Esther Tetteh', 'Volunteer Coordinator')),
            cities=('Accra', 'Ho', 'Kumasi'),
            phone_prefix='30',
        ),
        _build_generic_profile(
            pack_key='beauty_personal_care',
            workspace_name='East Legon Glow Studio',
            short_code='beauty',
            city='Accra',
            sector_noun='beauty and personal care',
            company_names=('Cantonments Bridal Collective', 'Osu Natural Shea Apothecary', 'Airport Spa & Wellness'),
            person_names=(('Nadia Boateng', 'VIP Client'), ('Favour Agyei', 'Bridal Coordinator'), ('Christabel Osei', 'Membership Client'), ('Zainab Issah', 'Retail Stockist')),
            cities=('Accra', 'Tema', 'Kumasi'),
            phone_prefix='30',
        ),
        _build_generic_profile(
            pack_key='automotive',
            workspace_name='Abossey Okai Auto Works',
            short_code='auto',
            city='Accra',
            sector_noun='automotive workshop',
            company_names=('Spintex Executive Fleet', 'Tema Haulage Trucks Ltd', 'Accra Ride-Share Cooperative'),
            person_names=(('Nii Armah Tagoe', 'Fleet Manager'), ('Kwaku Frimpong', 'Service Advisor'), ('Bright Asiedu', 'Parts Procurement'), ('Sarah Konadu', 'Corporate Fleet Lead')),
            cities=('Accra', 'Tema', 'Kumasi'),
            phone_prefix='30',
        ),
        _build_generic_profile(
            pack_key='media_creative_agencies',
            workspace_name='Osu Creative & Media House',
            short_code='media',
            city='Accra',
            sector_noun='media and creative production',
            company_names=('Accra Beverage Brands Ltd', 'Ghana Telecom Launch Team', 'West Africa Retail Expo'),
            person_names=(('Kojo Yankson', 'Brand Director'), ('Abena Korkor Mensah', 'Campaign Producer'), ('Nii Adotei', 'Media Buyer'), ('Belinda Owusu', 'Digital Strategist')),
            cities=('Accra', 'Kumasi', 'Takoradi'),
            phone_prefix='30',
        ),
        _build_generic_profile(
            pack_key='energy_fuel',
            workspace_name='Takoradi Solar & Fuel Depot',
            short_code='energy',
            city='Takoradi',
            sector_noun='energy and fuel supply',
            company_names=('Western Quarry & Aggregates', 'Tema Port Generators Corp', 'Sunyani Solar Microgrid'),
            person_names=(('George Ackah', 'Depot Director'), ('Faustina Mensah', 'Energy Procurement'), ('Alhaji Haruna Braimah', 'Bulk Fuel Buyer'), ('David Kyei', 'Field Engineer')),
            cities=('Takoradi', 'Tema', 'Sunyani'),
            phone_prefix='31',
        ),
    ]
    by_key = {profile.pack_key: profile for profile in profiles}
    missing = [bt.key for bt in BUSINESS_TYPES if bt.key not in by_key]
    if missing:  # pragma: no cover - static integrity guard
        raise RuntimeError(f'Missing demo seed profiles for business types: {missing}')
    return by_key


DEMO_PROFILES_BY_KEY: dict[str, DemoTenantProfile] = _build_all_profiles()
DEMO_WORKSPACE_SLUGS: frozenset[str] = frozenset(
    profile.slug for profile in DEMO_PROFILES_BY_KEY.values()
)


def resolve_demo_pack_keys(only_packs: Iterable[str] | None = None) -> list[str]:
    """Validate and resolve requested pack keys, defaulting to all 20 business types."""
    if not only_packs:
        return [bt.key for bt in BUSINESS_TYPES]

    resolved: list[str] = []
    unknown: list[str] = []
    for raw_item in only_packs:
        if not raw_item:
            continue
        for token in str(raw_item).split(','):
            cleaned = token.strip().lower()
            if not cleaned:
                continue
            if cleaned not in BUSINESS_TYPES_BY_KEY:
                unknown.append(cleaned)
            elif cleaned not in resolved:
                resolved.append(cleaned)

    if unknown:
        valid_list = ', '.join(bt.key for bt in BUSINESS_TYPES)
        raise DemoSeedError(
            f'Unknown pack key(s): {", ".join(unknown)}. Valid pack keys: {valid_list}.'
        )
    if not resolved:
        return [bt.key for bt in BUSINESS_TYPES]
    return resolved


def resolve_demo_password(
    *,
    password: str | None = None,
    debug: bool | None = None,
) -> str:
    """Resolve demo password, failing closed when DEBUG=False lacks an explicit password."""
    is_debug = settings.DEBUG if debug is None else bool(debug)
    explicit = (password or '').strip() if isinstance(password, str) else ''
    env_pw = os.environ.get(DEMO_PASSWORD_ENV_VAR, '').strip()

    chosen = explicit or env_pw
    if not chosen:
        if is_debug:
            chosen = DEFAULT_DEV_DEMO_PASSWORD
        else:
            raise DemoSeedError(
                'A password is required when running seed_demo_data with DEBUG=False and --force. '
                f'Pass --password or set {DEMO_PASSWORD_ENV_VAR}.'
            )

    if len(chosen) < 8:
        raise DemoSeedError('Demo password must be at least 8 characters long.')
    return chosen


def _is_demo_email(email: str | None) -> bool:
    return bool(email) and str(email).strip().lower().endswith(DEMO_EMAIL_DOMAIN)


def _assert_safe_demo_workspace(workspace: Workspace, *, expected_slug: str) -> None:
    """Verify an existing workspace occupying a demo slug was created & owned by demo accounts."""
    if workspace.slug != expected_slug or workspace.slug not in DEMO_WORKSPACE_SLUGS:
        raise DemoSeedError(
            f"Refusing to touch workspace '{workspace.slug}' because it is not a recognized demo slug."
        )

    creator = workspace.created_by
    if creator is None or not _is_demo_email(creator.email):
        raise DemoSeedError(
            f"Refusing to modify or reset workspace '{workspace.slug}': "
            f'it was not created by a {DEMO_EMAIL_DOMAIN} demo account.'
        )

    owner_users = list(workspace.users.filter(role__is_owner=True).only('email'))
    if not owner_users or any(not _is_demo_email(u.email) for u in owner_users):
        raise DemoSeedError(
            f"Refusing to modify or reset workspace '{workspace.slug}': "
            f'its Owner account is not a {DEMO_EMAIL_DOMAIN} demo account.'
        )


def _ensure_demo_user(
    *,
    workspace: Workspace,
    email: str,
    role: Role,
    password: str,
    verified_at,
) -> User:
    """Get or create a verified demo user inside the target workspace."""
    existing = User.objects.select_related('workspace').filter(email=email).first()
    if existing is not None:
        if existing.workspace_id != workspace.id:
            raise DemoSeedError(
                f"Demo email '{email}' already belongs to workspace '{existing.workspace.slug}'."
            )
        return existing

    return User.objects.create_user(
        email=email,
        workspace=workspace,
        password=password,
        role=role,
        timezone='Africa/Accra',
        email_verified=True,
        email_verified_at=verified_at,
        is_active=True,
    )


def _ensure_custom_role(
    *,
    workspace: Workspace,
    profile: DemoTenantProfile,
) -> Role | None:
    """Idempotently create a QA cloned custom role with hidden/read-only fields when configured."""
    if not profile.custom_role_name:
        return None

    existing = Role.objects.filter(
        workspace=workspace,
        name__iexact=profile.custom_role_name,
    ).first()
    if existing is not None:
        return existing

    field_rights = build_default_field_rights()
    for obj_key, field_key in profile.custom_role_hidden_fields:
        if obj_key in field_rights and field_key in field_rights[obj_key]:
            field_rights[obj_key][field_key] = FIELD_RIGHT_HIDDEN
    for obj_key, field_key in profile.custom_role_readonly_fields:
        if obj_key in field_rights and field_key in field_rights[obj_key]:
            field_rights[obj_key][field_key] = FIELD_RIGHT_READ_ONLY

    return Role.objects.create(
        workspace=workspace,
        name=profile.custom_role_name,
        is_owner=False,
        is_default_staff=False,
        description='Cloned from Staff for QA verification of field-level hiding and role-shared records.',
        can_manage_seat_plan=False,
        can_manage_sms_wallet=False,
        allow_import=STAFF_ROLE_DEFAULTS['allow_import'],
        allow_export=STAFF_ROLE_DEFAULTS['allow_export'],
        automations=STAFF_ROLE_DEFAULTS['automations'],
        scripts=STAFF_ROLE_DEFAULTS['scripts'],
        users_and_roles=STAFF_ROLE_DEFAULTS['users_and_roles'],
        theme_and_layouts=STAFF_ROLE_DEFAULTS['theme_and_layouts'],
        unattended_agent=STAFF_ROLE_DEFAULTS['unattended_agent'],
        object_rights=build_default_object_rights(is_owner=False),
        field_rights=field_rights,
    )


def _ensure_audit_log(
    *,
    workspace: Workspace,
    object_type: str,
    object_id,
    record_label: str,
    field_name: str,
    previous_value,
    new_value,
    actor_user: User,
    action: str,
    record=None,
    created_at=None,
) -> None:
    """Idempotently insert one AuditLog entry if an identical transition is not already recorded."""
    str_id = '' if object_id is None else str(object_id)[:64]
    lookup = {
        'workspace': workspace,
        'object_type': object_type,
        'object_id': str_id,
        'action': action,
        'field_name': field_name,
    }
    if previous_value is None:
        lookup['previous_value__isnull'] = True
    else:
        lookup['previous_value'] = previous_value
    if new_value is None:
        lookup['new_value__isnull'] = True
    else:
        lookup['new_value'] = new_value

    if AuditLog.objects.filter(**lookup).exists():
        return
    record_audit_event(
        workspace=workspace,
        object_type=object_type,
        object_id=str_id,
        record_label=record_label,
        field_name=field_name,
        previous_value=previous_value,
        new_value=new_value,
        actor_kind=AuditLog.ACTOR_KIND_PERSON,
        actor_user=actor_user,
        action=action,
        record=record,
        created_at=created_at,
    )


def _seed_one_workspace(
    *,
    pack_key: str,
    password: str,
    reset: bool,
) -> SeededTenantSummary:
    bt = BUSINESS_TYPES_BY_KEY[pack_key]
    profile = DEMO_PROFILES_BY_KEY[pack_key]
    now = timezone.now()
    today = timezone.localdate()

    owner_email = f'owner.{profile.short_code}{DEMO_EMAIL_DOMAIN}'
    staff_email = f'staff.{profile.short_code}{DEMO_EMAIL_DOMAIN}'
    coord_email = (
        f'coord.{profile.short_code}{DEMO_EMAIL_DOMAIN}'
        if profile.custom_role_name
        else None
    )

    reset_performed = False
    created_workspace = False
    extension_counts: dict[str, dict[str, int]] = {}

    with transaction.atomic():
        existing_ws = Workspace.objects.select_related('created_by').filter(slug=profile.slug).first()
        if existing_ws is not None:
            _assert_safe_demo_workspace(existing_ws, expected_slug=profile.slug)
            if reset:
                old_ws_id = existing_ws.id
                existing_ws.delete()
                invalidate_workspace_cache(old_ws_id, domains=ALL_WORKSPACE_DOMAINS)
                existing_ws = None
                reset_performed = True

        # Ensure demo emails are not bound to a non-target workspace before creating.
        check_emails = [e for e in (owner_email, staff_email, coord_email) if e]
        conflicting_users = (
            User.objects.select_related('workspace')
            .filter(email__in=check_emails)
            .exclude(workspace__slug=profile.slug)
        )
        for conflict in conflicting_users:
            raise DemoSeedError(
                f"Demo email '{conflict.email}' is already used in workspace '{conflict.workspace.slug}'."
            )

        if existing_ws is None:
            workspace = Workspace.objects.create(
                name=profile.workspace_name,
                slug=profile.slug,
                business_type=bt.key,
                business_type_label=bt.label,
                contact_label=bt.default_contact_label,
            )
            created_workspace = True
        else:
            workspace = existing_ws

        owner_role, staff_role = ensure_workspace_default_roles(workspace)
        custom_role = _ensure_custom_role(workspace=workspace, profile=profile)

        owner = _ensure_demo_user(
            workspace=workspace,
            email=owner_email,
            role=owner_role,
            password=password,
            verified_at=now - timedelta(days=30),
        )
        if workspace.created_by_id is None:
            workspace.created_by = owner
            workspace.save(update_fields=['created_by'])

        staff = _ensure_demo_user(
            workspace=workspace,
            email=staff_email,
            role=staff_role,
            password=password,
            verified_at=now - timedelta(days=25),
        )
        coordinator = (
            _ensure_demo_user(
                workspace=workspace,
                email=coord_email,
                role=custom_role,
                password=password,
                verified_at=now - timedelta(days=20),
            )
            if coord_email and custom_role is not None
            else None
        )

        if created_workspace:
            initialize_workspace_catalog(workspace, bt.key, actor_user=owner)
        else:
            ensure_workspace_catalog(workspace)

        enabled_tables = frozenset(
            WorkspaceTable.objects.filter(
                workspace=workspace,
                is_enabled=True,
            ).values_list('object_key', flat=True)
        )

        pipeline = None
        stages_by_name: dict[str, object] = {}
        ordered_stages: list = []
        if 'deal' in enabled_tables:
            pipeline = ensure_workspace_pipeline(workspace, require_enabled=False)
            ordered_stages = get_ordered_pipeline_stages(workspace, require_enabled=False)
            stages_by_name = {stage.name.lower(): stage for stage in ordered_stages}

        share_target_role = custom_role or staff_role

        # 1. Accounts (core)
        accounts: list[Account] = []
        for idx, (name, city, phone, email, notes) in enumerate(profile.accounts):
            visibility = WorkspaceRecordBase.VISIBILITY_WORKSPACE
            shared_role = None
            rec_owner = owner if idx % 2 == 0 else staff
            if idx == len(profile.accounts) - 1:
                visibility = WorkspaceRecordBase.VISIBILITY_ROLE
                shared_role = share_target_role
            account, _ = Account.objects.get_or_create(
                workspace=workspace,
                name=name,
                defaults={
                    'city': city,
                    'phone': phone,
                    'email': email,
                    'notes': notes,
                    'owner': rec_owner,
                    'created_by': owner,
                    'visibility': visibility,
                    'shared_role': shared_role,
                },
            )
            accounts.append(account)

        # 2. Contacts / Patients (core)
        contacts: list[Contact] = []
        for idx, (name, title, email, phone, sms_consent, acct_idx) in enumerate(profile.contacts):
            linked_account = accounts[acct_idx] if acct_idx is not None and acct_idx < len(accounts) else None
            visibility = WorkspaceRecordBase.VISIBILITY_WORKSPACE
            shared_role = None
            rec_owner = owner if idx % 2 == 0 else staff
            if idx == len(profile.contacts) - 1 and len(profile.contacts) >= 3:
                visibility = WorkspaceRecordBase.VISIBILITY_OWNER
                rec_owner = owner
            contact, _ = Contact.objects.get_or_create(
                workspace=workspace,
                email=email,
                defaults={
                    'account': linked_account,
                    'name': name,
                    'title': title,
                    'phone': phone,
                    'sms_consent': sms_consent,
                    'notes': f'Primary {bt.default_contact_label.lower()} record in {profile.city}.',
                    'owner': rec_owner,
                    'created_by': owner,
                    'visibility': visibility,
                    'shared_role': shared_role,
                },
            )
            contacts.append(contact)

        # 3. Deals (pack-gated)
        deals: list[Deal] = []
        if 'deal' in enabled_tables and pipeline is not None and ordered_stages:
            for idx, (
                title,
                stage_name,
                amount_str,
                win_prob,
                close_offset_days,
                margin_str,
                acct_idx,
                contact_idx,
            ) in enumerate(profile.deals):
                stage_obj = stages_by_name.get(stage_name.lower()) or ordered_stages[idx % len(ordered_stages)]
                linked_account = accounts[acct_idx] if acct_idx < len(accounts) else None
                linked_contact = (
                    contacts[contact_idx]
                    if contact_idx is not None and contact_idx < len(contacts)
                    else None
                )
                visibility = WorkspaceRecordBase.VISIBILITY_WORKSPACE
                shared_role = None
                rec_owner = owner if idx % 2 == 0 else staff
                if idx == 0:
                    visibility = WorkspaceRecordBase.VISIBILITY_ROLE
                    shared_role = share_target_role
                elif idx == len(profile.deals) - 1:
                    visibility = WorkspaceRecordBase.VISIBILITY_OWNER
                    rec_owner = owner

                deal, _ = Deal.objects.get_or_create(
                    workspace=workspace,
                    title=title,
                    defaults={
                        'pipeline': pipeline,
                        'pipeline_stage': stage_obj,
                        'stage': stage_obj.name,
                        'account': linked_account,
                        'contact': linked_contact,
                        'amount': Decimal(amount_str),
                        'target_close_date': today + timedelta(days=close_offset_days),
                        'win_probability': win_prob,
                        'estimated_margin': Decimal(margin_str),
                        'notes': f'Seeded {bt.label} opportunity in {stage_obj.name} stage.',
                        'owner': rec_owner,
                        'created_by': owner,
                        'visibility': visibility,
                        'shared_role': shared_role,
                    },
                )
                deals.append(deal)

        # 4. Leads (pack-gated): open leads + 1 converted lead linked to Account/Contact/Deal
        leads: list[Lead] = []
        if 'lead' in enabled_tables:
            for idx, (name, company, email, phone, sms_consent, notes) in enumerate(profile.open_leads):
                lead, _ = Lead.objects.get_or_create(
                    workspace=workspace,
                    email=email,
                    defaults={
                        'name': name,
                        'company': company,
                        'phone': phone,
                        'sms_consent': sms_consent,
                        'status': Lead.STATUS_OPEN,
                        'notes': notes,
                        'owner': staff if idx % 2 else owner,
                        'created_by': owner,
                        'visibility': WorkspaceRecordBase.VISIBILITY_WORKSPACE,
                    },
                )
                leads.append(lead)

            if 'deal' in enabled_tables:
                conv_name, conv_company, conv_email, conv_phone, conv_sms, conv_notes = profile.converted_lead
                conv_account = next(
                    (a for a in accounts if a.name.strip().lower() == conv_company.strip().lower()),
                    accounts[0] if accounts else None,
                )
                conv_contact = next(
                    (c for c in contacts if c.name.strip().lower() == conv_name.strip().lower()),
                    contacts[0] if contacts else None,
                )
                conv_deal = next(
                    (
                        d
                        for d in deals
                        if (conv_account is not None and d.account_id == conv_account.id)
                        or (conv_contact is not None and d.contact_id == conv_contact.id)
                    ),
                    deals[0] if deals else None,
                )
                conv_lead, conv_created = Lead.objects.get_or_create(
                    workspace=workspace,
                    email=conv_email,
                    defaults={
                        'name': conv_name,
                        'company': conv_company,
                        'phone': conv_phone,
                        'sms_consent': conv_sms,
                        'status': Lead.STATUS_CONVERTED,
                        'notes': conv_notes,
                        'converted_at': now - timedelta(days=6),
                        'converted_by': owner,
                        'converted_account': conv_account,
                        'converted_contact': conv_contact,
                        'converted_deal': conv_deal,
                        'owner': owner,
                        'created_by': owner,
                        'visibility': WorkspaceRecordBase.VISIBILITY_WORKSPACE,
                    },
                )
                if conv_created:
                    if conv_account is not None and conv_account.source_lead_id is None:
                        conv_account.source_lead = conv_lead
                        conv_account.save(update_fields=['source_lead', 'updated_at'])
                    if conv_contact is not None and conv_contact.source_lead_id is None:
                        conv_contact.source_lead = conv_lead
                        conv_contact.save(update_fields=['source_lead', 'updated_at'])
                    if conv_deal is not None and conv_deal.source_lead_id is None:
                        conv_deal.source_lead = conv_lead
                        conv_deal.save(update_fields=['source_lead', 'updated_at'])
                leads.append(conv_lead)

        # 5. Jobs (pack-gated)
        jobs: list[Job] = []
        if 'job' in enabled_tables:
            for idx, (title, status, due_offset, acct_idx, contact_idx, notes) in enumerate(profile.jobs):
                linked_account = (
                    accounts[acct_idx]
                    if acct_idx is not None and acct_idx < len(accounts)
                    else None
                )
                linked_contact = (
                    contacts[contact_idx]
                    if contact_idx is not None and contact_idx < len(contacts)
                    else None
                )
                assignee = staff if idx % 2 == 0 else owner
                job, _ = Job.objects.get_or_create(
                    workspace=workspace,
                    title=title,
                    defaults={
                        'status': status,
                        'assignee': assignee,
                        'due_date': today + timedelta(days=due_offset),
                        'account': linked_account,
                        'contact': linked_contact,
                        'notes': notes,
                        'owner': owner if idx == 0 else staff,
                        'created_by': owner,
                        'visibility': WorkspaceRecordBase.VISIBILITY_WORKSPACE,
                    },
                )
                jobs.append(job)

        # 6. Appointments (pack-gated)
        appointments: list[Appointment] = []
        if 'appointment' in enabled_tables:
            for idx, (title, status, day_offset, contact_idx, notes) in enumerate(profile.appointments):
                linked_contact = (
                    contacts[contact_idx]
                    if contact_idx is not None and contact_idx < len(contacts)
                    else None
                )
                scheduled_dt = (now + timedelta(days=day_offset)).replace(
                    hour=9 + (idx % 5),
                    minute=0,
                    second=0,
                    microsecond=0,
                )
                appt, _ = Appointment.objects.get_or_create(
                    workspace=workspace,
                    title=title,
                    defaults={
                        'scheduled_at': scheduled_dt,
                        'status': status,
                        'assignee': owner if idx % 2 == 0 else staff,
                        'contact': linked_contact,
                        'notes': notes,
                        'owner': owner,
                        'created_by': owner,
                        'visibility': WorkspaceRecordBase.VISIBILITY_WORKSPACE,
                    },
                )
                appointments.append(appt)

        # 7. Visits (pack-gated; active on healthcare_wellness)
        visits: list[Visit] = []
        if 'visit' in enabled_tables:
            for idx, (contact_idx, day_offset, reason, notes, follow_up) in enumerate(profile.visits):
                linked_contact = contacts[contact_idx] if contact_idx < len(contacts) else None
                visit_dt = (now + timedelta(days=day_offset)).replace(
                    hour=10 + (idx % 4),
                    minute=30,
                    second=0,
                    microsecond=0,
                )
                visibility = WorkspaceRecordBase.VISIBILITY_WORKSPACE
                shared_role = None
                if idx == len(profile.visits) - 1 and len(profile.visits) >= 2:
                    visibility = WorkspaceRecordBase.VISIBILITY_ROLE
                    shared_role = share_target_role
                visit, _ = Visit.objects.get_or_create(
                    workspace=workspace,
                    contact=linked_contact,
                    reason=reason,
                    defaults={
                        'date': visit_dt,
                        'notes': notes,
                        'follow_up': follow_up,
                        'assignee': owner if idx % 2 == 0 else staff,
                        'owner': owner,
                        'created_by': owner,
                        'visibility': visibility,
                        'shared_role': shared_role,
                    },
                )
                visits.append(visit)

        # 8. Tickets (pack-gated)
        tickets: list[Ticket] = []
        if 'ticket' in enabled_tables:
            for idx, (title, status, priority, acct_idx, contact_idx, description) in enumerate(profile.tickets):
                linked_account = (
                    accounts[acct_idx]
                    if acct_idx is not None and acct_idx < len(accounts)
                    else None
                )
                linked_contact = (
                    contacts[contact_idx]
                    if contact_idx is not None and contact_idx < len(contacts)
                    else None
                )
                ticket, _ = Ticket.objects.get_or_create(
                    workspace=workspace,
                    title=title,
                    defaults={
                        'status': status,
                        'priority': priority,
                        'account': linked_account,
                        'contact': linked_contact,
                        'assignee': staff if idx % 2 == 0 else owner,
                        'description': description,
                        'owner': staff if idx % 2 == 0 else owner,
                        'created_by': owner,
                        'visibility': WorkspaceRecordBase.VISIBILITY_WORKSPACE,
                    },
                )
                tickets.append(ticket)

        # 9. Activities (core; attached across active CRM targets)
        activity_specs: list[tuple[str, int, str, str, object, bool, User]] = []
        if accounts:
            activity_specs.append((
                'account',
                accounts[0].id,
                Activity.TYPE_NOTE,
                f'Confirmed annual GHS billing contact and Accra office delivery hours for {accounts[0].name}.',
                None,
                False,
                owner,
            ))
        if contacts:
            activity_specs.append((
                'contact',
                contacts[0].id,
                Activity.TYPE_CALL,
                f'Introductory check-in call with {contacts[0].name} regarding upcoming milestones.',
                now - timedelta(days=1),
                True,
                staff,
            ))
        if leads:
            activity_specs.append((
                'lead',
                leads[0].id,
                Activity.TYPE_TASK,
                f'Follow up with {leads[0].name} on GHS quotation before end of week.',
                now - timedelta(days=1),  # Intentionally overdue task for QA
                False,
                staff,
            ))
        if deals:
            activity_specs.append((
                'deal',
                deals[min(2, len(deals) - 1)].id,
                Activity.TYPE_MEETING,
                f'Proposal walkthrough meeting for {deals[min(2, len(deals) - 1)].title}.',
                now + timedelta(days=2),
                False,
                owner,
            ))
        if jobs:
            activity_specs.append((
                'job',
                jobs[0].id,
                Activity.TYPE_TASK,
                f'Verify sign-off sheet for {jobs[0].title}.',
                now + timedelta(days=3),
                False,
                staff,
            ))
        if visits:
            activity_specs.append((
                'visit',
                visits[0].id,
                Activity.TYPE_TASK,
                f'Confirm follow-up reminder for {visits[0].reason}.',
                now + timedelta(days=2),
                False,
                staff,
            ))
        if tickets:
            activity_specs.append((
                'ticket',
                tickets[0].id,
                Activity.TYPE_NOTE,
                f'Triaged priority issue: {tickets[0].title}.',
                None,
                False,
                owner,
            ))

        activities: list[Activity] = []
        for target_type, target_id, act_type, summary, due_at, is_completed, assignee in activity_specs:
            activity, _ = Activity.objects.get_or_create(
                workspace=workspace,
                target_object_type=target_type,
                target_object_id=target_id,
                activity_type=act_type,
                summary=summary,
                defaults={
                    'due_at': due_at,
                    'is_completed': is_completed,
                    'assignee': assignee,
                    'owner': assignee,
                    'created_by': owner,
                    'visibility': WorkspaceRecordBase.VISIBILITY_WORKSPACE,
                },
            )
            activities.append(activity)

        # 10. Initial AuditLog entries so workspace and record history drawers are populated
        if accounts:
            _ensure_audit_log(
                workspace=workspace,
                object_type='account',
                object_id=accounts[0].id,
                record_label=accounts[0].name,
                field_name='record',
                previous_value=None,
                new_value=accounts[0].name,
                actor_user=owner,
                action=AuditLog.ACTION_CREATE,
                record=accounts[0],
                created_at=now - timedelta(days=10),
            )
        if deals and len(deals) >= 3:
            proposal_deal = deals[2]
            _ensure_audit_log(
                workspace=workspace,
                object_type='deal',
                object_id=proposal_deal.id,
                record_label=proposal_deal.title,
                field_name='stage',
                previous_value='New',
                new_value='Qualified',
                actor_user=staff,
                action=AuditLog.ACTION_UPDATE,
                record=proposal_deal,
                created_at=now - timedelta(days=5),
            )
            _ensure_audit_log(
                workspace=workspace,
                object_type='deal',
                object_id=proposal_deal.id,
                record_label=proposal_deal.title,
                field_name='stage',
                previous_value='Qualified',
                new_value=proposal_deal.stage,
                actor_user=owner,
                action=AuditLog.ACTION_UPDATE,
                record=proposal_deal,
                created_at=now - timedelta(days=2),
            )
        if visits:
            _ensure_audit_log(
                workspace=workspace,
                object_type='visit',
                object_id=visits[0].id,
                record_label=visits[0].reason,
                field_name='record',
                previous_value=None,
                new_value=visits[0].reason,
                actor_user=owner,
                action=AuditLog.ACTION_CREATE,
                record=visits[0],
                created_at=now - timedelta(days=4),
            )
        if custom_role is not None:
            _ensure_audit_log(
                workspace=workspace,
                object_type='role',
                object_id=custom_role.id,
                record_label=custom_role.name,
                field_name='role',
                previous_value=None,
                new_value=custom_role.name,
                actor_user=owner,
                action=AuditLog.ACTION_CREATE,
                created_at=now - timedelta(days=20),
            )

        # 11. Invoke any Sprint 3-5 registered demo seed extensions
        if _DEMO_SEED_EXTENSIONS:
            ctx = DemoSeedContext(
                workspace=workspace,
                business_type=bt,
                owner=owner,
                staff=staff,
                coordinator=coordinator,
                owner_role=owner_role,
                staff_role=staff_role,
                custom_role=custom_role,
                enabled_tables=enabled_tables,
                accounts=accounts,
                contacts=contacts,
                leads=leads,
                deals=deals,
                jobs=jobs,
                appointments=appointments,
                visits=visits,
                tickets=tickets,
                reset=reset_performed,
            )
            for ext_name, ext_callback in _DEMO_SEED_EXTENSIONS.items():
                result = ext_callback(ctx)
                if isinstance(result, dict):
                    extension_counts[ext_name] = {
                        str(k): int(v) for k, v in result.items()
                    }

        invalidate_workspace_cache(workspace.id, domains=ALL_WORKSPACE_DOMAINS)

        counts = {
            'users': User.objects.filter(workspace=workspace).count(),
            'accounts': Account.objects.filter(workspace=workspace).count(),
            'contacts': Contact.objects.filter(workspace=workspace).count(),
            'leads': Lead.objects.filter(workspace=workspace).count(),
            'deals': Deal.objects.filter(workspace=workspace).count(),
            'jobs': Job.objects.filter(workspace=workspace).count(),
            'appointments': Appointment.objects.filter(workspace=workspace).count(),
            'visits': Visit.objects.filter(workspace=workspace).count(),
            'tickets': Ticket.objects.filter(workspace=workspace).count(),
            'activities': Activity.objects.filter(workspace=workspace).count(),
            'audit_logs': AuditLog.objects.filter(workspace=workspace).count(),
        }

    return SeededTenantSummary(
        pack_key=bt.key,
        business_type_label=bt.label,
        workspace_name=workspace.name,
        workspace_slug=workspace.slug,
        contact_label=workspace.contact_label,
        created_workspace=created_workspace,
        reset_performed=reset_performed,
        owner_email=owner_email,
        staff_email=staff_email,
        coordinator_email=coord_email,
        counts=counts,
        extension_counts=extension_counts,
    )


def seed_demo_workspaces(
    *,
    reset: bool = False,
    only_packs: Iterable[str] | None = None,
    force: bool = False,
    password: str | None = None,
) -> list[SeededTenantSummary]:
    """Idempotently seed demo workspaces for manual QA across all or selected business types."""
    if not settings.DEBUG and not force:
        raise DemoSeedError(
            'Refusing to run seed_demo_data when DEBUG=False. '
            'Pass --force (with --password or DEMO_SEED_PASSWORD) to override in a non-production environment.'
        )

    resolved_password = resolve_demo_password(password=password, debug=settings.DEBUG)
    pack_keys = resolve_demo_pack_keys(only_packs)

    # Pre-flight safety check across all targeted slugs before mutating any workspace.
    for pack_key in pack_keys:
        profile = DEMO_PROFILES_BY_KEY[pack_key]
        existing_ws = Workspace.objects.select_related('created_by').filter(slug=profile.slug).first()
        if existing_ws is not None:
            _assert_safe_demo_workspace(existing_ws, expected_slug=profile.slug)

    summaries: list[SeededTenantSummary] = []
    for pack_key in pack_keys:
        summaries.append(
            _seed_one_workspace(
                pack_key=pack_key,
                password=resolved_password,
                reset=reset,
            )
        )
    return summaries
