from dataclasses import dataclass


@dataclass(frozen=True)
class StarterRoleDefinition:
    """Deletable industry-tailored starter role provisioned on workspace creation."""

    name: str
    description: str
    switches: tuple[str, ...] = ()
    restricted_objects: tuple[tuple[str, tuple[bool, bool, bool, bool]], ...] = ()
    field_rights: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class ObjectDefinition:
    """Canonical CRM starter object metadata for core and catalog packs."""

    key: str
    label: str
    plural_label: str
    description: str
    is_core: bool
    nav_group: str | None = None
    pane_id: str | None = None


@dataclass(frozen=True)
class BusinessTypeDefinition:
    """Canonical business-type definition mapping to 12 catalog pack objects and starter roles."""

    key: str
    label: str
    description: str
    default_contact_label: str
    pack_objects: tuple[str, ...]
    starter_roles: tuple[StarterRoleDefinition, ...] = ()


CORE_OBJECTS: tuple[ObjectDefinition, ...] = (
    ObjectDefinition(
        key='account',
        label='Account',
        plural_label='Accounts',
        description='A company or organization the tenant works with.',
        is_core=True,
        nav_group='crm',
        pane_id='accounts',
    ),
    ObjectDefinition(
        key='contact',
        label='Contact',
        plural_label='Contacts',
        description='A person linked to an Account or workspace workflow.',
        is_core=True,
        nav_group='crm',
        pane_id='contacts',
    ),
    ObjectDefinition(
        key='activity',
        label='Activity',
        plural_label='Activities',
        description='A note, task, call, meeting, or logged email on a record.',
        is_core=True,
    ),
    ObjectDefinition(
        key='file',
        label='File',
        plural_label='Files',
        description='An attachment stored on any record.',
        is_core=True,
    ),
    ObjectDefinition(
        key='tag',
        label='Tag',
        plural_label='Tags',
        description='A workspace label applied across records.',
        is_core=True,
    ),
)

CATALOG_OBJECTS: tuple[ObjectDefinition, ...] = (
    ObjectDefinition(
        key='lead',
        label='Lead',
        plural_label='Leads',
        description='A person or company not yet converted.',
        is_core=False,
        nav_group='crm',
        pane_id='leads',
    ),
    ObjectDefinition(
        key='deal',
        label='Deal',
        plural_label='Deals',
        description='An opportunity on a pipeline.',
        is_core=False,
        nav_group='core',
        pane_id='deals',
    ),
    ObjectDefinition(
        key='product',
        label='Product',
        plural_label='Products',
        description='Something the tenant sells.',
        is_core=False,
        nav_group='crm',
        pane_id='products',
    ),
    ObjectDefinition(
        key='line_item',
        label='Line item',
        plural_label='Line items',
        description='A Product quantity and price on a parent record.',
        is_core=False,
    ),
    ObjectDefinition(
        key='invoice',
        label='Invoice',
        plural_label='Invoices',
        description="A commercial bill to the tenant's customer.",
        is_core=False,
        nav_group='core',
        pane_id='invoicing',
    ),
    ObjectDefinition(
        key='payment',
        label='Payment',
        plural_label='Payments',
        description='Money recorded against an Invoice.',
        is_core=False,
        nav_group='core',
        pane_id='invoicing',
    ),
    ObjectDefinition(
        key='inventory',
        label='Inventory',
        plural_label='Inventory',
        description='Quantity of a Product in a warehouse.',
        is_core=False,
        nav_group='core',
        pane_id='invoicing',
    ),
    ObjectDefinition(
        key='campaign',
        label='Campaign',
        plural_label='Campaigns',
        description='One outreach by email, SMS, or phone.',
        is_core=False,
        nav_group='platform',
        pane_id='campaigns',
    ),
    ObjectDefinition(
        key='contract',
        label='Contract',
        plural_label='Contracts',
        description='An agreement with an Account.',
        is_core=False,
        nav_group='crm',
        pane_id='contracts',
    ),
    ObjectDefinition(
        key='ticket',
        label='Ticket',
        plural_label='Tickets',
        description='A support case.',
        is_core=False,
        nav_group='operations',
        pane_id='tickets',
    ),
    ObjectDefinition(
        key='customer_subscription',
        label='Customer subscription',
        plural_label='Customer subscriptions',
        description="A recurring sale to the tenant's customer.",
        is_core=False,
        nav_group='core',
        pane_id='invoicing',
    ),
    ObjectDefinition(
        key='job',
        label='Job',
        plural_label='Jobs',
        description='A piece of work with status, assignee, due date, and linked Account or Contact.',
        is_core=False,
        nav_group='operations',
        pane_id='jobs',
    ),
    ObjectDefinition(
        key='appointment',
        label='Appointment',
        plural_label='Appointments',
        description='A scheduled time with status, assignee, and a linked Contact.',
        is_core=False,
        nav_group='operations',
        pane_id='appointments',
    ),
    ObjectDefinition(
        key='supplier',
        label='Supplier',
        plural_label='Suppliers',
        description='A vendor, with what they supply.',
        is_core=False,
        nav_group='supply',
        pane_id='suppliers',
    ),
    ObjectDefinition(
        key='purchase',
        label='Purchase',
        plural_label='Purchases',
        description='Goods ordered from a Supplier.',
        is_core=False,
        nav_group='supply',
        pane_id='purchases',
    ),
    ObjectDefinition(
        key='shipment',
        label='Shipment',
        plural_label='Shipments',
        description='A movement of goods, with status, origin, destination, and linked invoice or purchase.',
        is_core=False,
        nav_group='supply',
        pane_id='shipments',
    ),
    ObjectDefinition(
        key='vehicle',
        label='Vehicle',
        plural_label='Vehicles',
        description='A vehicle the business tracks, with identity, status, and a linked Account.',
        is_core=False,
        nav_group='operations',
        pane_id='vehicles',
    ),
    ObjectDefinition(
        key='property',
        label='Property',
        plural_label='Properties',
        description='A site or unit, with address, status, and a linked Account.',
        is_core=False,
        nav_group='operations',
        pane_id='properties',
    ),
    ObjectDefinition(
        key='booking',
        label='Booking',
        plural_label='Bookings',
        description='A reserved stay or resource, with dates, status, and a linked Contact.',
        is_core=False,
        nav_group='operations',
        pane_id='bookings',
    ),
    ObjectDefinition(
        key='program',
        label='Program',
        plural_label='Programs',
        description='A course or program the tenant offers.',
        is_core=False,
        nav_group='operations',
        pane_id='programs',
    ),
    ObjectDefinition(
        key='enrollment',
        label='Enrollment',
        plural_label='Enrollments',
        description='A Contact joined to a Program, with status and start date.',
        is_core=False,
        nav_group='operations',
        pane_id='enrollments',
    ),
    ObjectDefinition(
        key='visit',
        label='Visit',
        plural_label='Visits',
        description='A healthcare meeting record: date, reason, notes, and follow-up.',
        is_core=False,
        nav_group='operations',
        pane_id='visits',
    ),
    ObjectDefinition(
        key='policy',
        label='Policy',
        plural_label='Policies',
        description='A finance product record: product, premium, status, and renewal.',
        is_core=False,
        nav_group='crm',
        pane_id='policies',
    ),
    ObjectDefinition(
        key='membership',
        label='Membership',
        plural_label='Memberships',
        description='A Contact on a plan, with status, start, and end.',
        is_core=False,
        nav_group='crm',
        pane_id='memberships',
    ),
    ObjectDefinition(
        key='asset',
        label='Asset',
        plural_label='Assets',
        description='An item the business owns or tracks, with status and location.',
        is_core=False,
        nav_group='operations',
        pane_id='assets',
    ),
)

ALL_OBJECTS: tuple[ObjectDefinition, ...] = CORE_OBJECTS + CATALOG_OBJECTS
OBJECTS_BY_KEY: dict[str, ObjectDefinition] = {obj.key: obj for obj in ALL_OBJECTS}
CORE_OBJECT_KEYS: tuple[str, ...] = tuple(obj.key for obj in CORE_OBJECTS)
CATALOG_OBJECT_KEYS: tuple[str, ...] = tuple(obj.key for obj in CATALOG_OBJECTS)

CONTACT_LABEL_PRESETS: tuple[str, ...] = (
    'Contact',
    'Patient',
    'Student or Guardian',
    'Donor or Member',
)

CONTACT_PLURAL_OVERRIDES: dict[str, str] = {
    'contact': 'Contacts',
    'patient': 'Patients',
    'student or guardian': 'Students or Guardians',
    'donor or member': 'Donors or Members',
}

_FIELD_RESTRICTIONS_STANDARD = (
    ('deal', 'estimated_margin', 'hidden'),
)

_FIELD_RESTRICTIONS_FRONTLINE = (
    ('deal', 'estimated_margin', 'hidden'),
    ('deal', 'win_probability', 'read_only'),
)

_FIELD_RESTRICTIONS_HEALTH = (
    ('visit', 'follow_up', 'read_only'),
)


BUSINESS_TYPES: tuple[BusinessTypeDefinition, ...] = (
    BusinessTypeDefinition(
        key='retail_trading',
        label='Retail and trading',
        description='Shops, merchandising, storefront sales, inventory, and supplier fulfillment.',
        default_contact_label='Contact',
        pack_objects=(
            'lead', 'deal', 'product', 'line_item', 'invoice', 'payment',
            'inventory', 'campaign', 'supplier', 'purchase', 'shipment', 'job',
        ),
        starter_roles=(
            StarterRoleDefinition(
                name='Store Manager',
                description='Oversees storefront teams, deals, inventory restocks, and daily sales reporting.',
                switches=('allow_import', 'allow_export', 'automations'),
                field_rights=(('deal', 'estimated_margin', 'read_only'),),
            ),
            StarterRoleDefinition(
                name='Sales Associate',
                description='Handles customer inquiries, leads, and showroom opportunities without margin visibility.',
                restricted_objects=(
                    ('account', (True, True, True, False)),
                    ('deal', (True, True, True, False)),
                    ('invoice', (True, True, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_FRONTLINE,
            ),
            StarterRoleDefinition(
                name='Inventory Clerk',
                description='Tracks stock counts, supplier purchase orders, and incoming warehouse shipments.',
                switches=('allow_import',),
                restricted_objects=(
                    ('deal', (False, True, False, False)),
                    ('payment', (False, False, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
            StarterRoleDefinition(
                name='Cashier & Billing',
                description='Issues commercial invoices and records customer payments with export access.',
                switches=('allow_export',),
                restricted_objects=(
                    ('deal', (False, True, False, False)),
                    ('supplier', (False, True, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
        ),
    ),
    BusinessTypeDefinition(
        key='wholesale_distribution',
        label='Wholesale and distribution',
        description='Bulk supply contracts, warehouse stock, supplier procurement, and delivery tracking.',
        default_contact_label='Contact',
        pack_objects=(
            'lead', 'deal', 'product', 'line_item', 'invoice', 'payment',
            'inventory', 'contract', 'supplier', 'purchase', 'shipment', 'job',
        ),
        starter_roles=(
            StarterRoleDefinition(
                name='Key Account Manager',
                description='Manages trade accounts, bulk supply deals, and commercial contracts.',
                switches=('allow_import', 'allow_export', 'automations'),
                field_rights=(('deal', 'estimated_margin', 'read_only'),),
            ),
            StarterRoleDefinition(
                name='Warehouse Coordinator',
                description='Monitors stock levels, dispatches shipments, and reconciles warehouse transfers.',
                restricted_objects=(
                    ('deal', (False, True, False, False)),
                    ('payment', (False, True, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
            StarterRoleDefinition(
                name='Procurement Officer',
                description='Manages supplier relationships and restock purchase orders.',
                switches=('allow_import',),
                restricted_objects=(
                    ('deal', (False, False, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
            StarterRoleDefinition(
                name='Dispatch & Receivables Clerk',
                description='Coordinates fulfillment jobs, deliveries, and customer payment collection.',
                switches=('allow_export',),
                restricted_objects=(
                    ('account', (True, True, True, False)),
                    ('deal', (False, True, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
        ),
    ),
    BusinessTypeDefinition(
        key='agriculture_agribusiness',
        label='Agriculture and agribusiness',
        description='Farm inputs, produce trading, inventory lots, procurement, and distribution.',
        default_contact_label='Contact',
        pack_objects=(
            'lead', 'deal', 'product', 'line_item', 'invoice', 'payment',
            'inventory', 'supplier', 'purchase', 'shipment', 'contract', 'job',
        ),
        starter_roles=(
            StarterRoleDefinition(
                name='Agribusiness Manager',
                description='Coordinates off-taker contracts, commodity deals, and regional depot operations.',
                switches=('allow_import', 'allow_export', 'automations'),
            ),
            StarterRoleDefinition(
                name='Field Extension Officer',
                description='Tracks grower accounts, input leads, and field supervision jobs.',
                restricted_objects=(
                    ('account', (True, True, True, False)),
                    ('payment', (False, False, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_FRONTLINE,
            ),
            StarterRoleDefinition(
                name='Depot Storekeeper',
                description='Handles produce lot inventory, supplier intakes, and outward shipments.',
                restricted_objects=(
                    ('deal', (False, True, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
            StarterRoleDefinition(
                name='Procurement & Billing Clerk',
                description='Processes supplier purchase orders, commercial invoices, and settlement records.',
                switches=('allow_export',),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
        ),
    ),
    BusinessTypeDefinition(
        key='food_beverage',
        label='Food and beverage',
        description='Menu products, reservations, supplier orders, stock control, and loyalty plans.',
        default_contact_label='Contact',
        pack_objects=(
            'product', 'line_item', 'invoice', 'payment', 'inventory', 'campaign',
            'supplier', 'purchase', 'booking', 'appointment', 'job', 'membership',
        ),
        starter_roles=(
            StarterRoleDefinition(
                name='Branch Manager',
                description='Oversees bookings, memberships, stock replenishment, and outlet performance.',
                switches=('allow_import', 'allow_export', 'automations'),
            ),
            StarterRoleDefinition(
                name='Hospitality Host',
                description='Manages guest reservations, appointments, and membership sign-ups.',
                restricted_objects=(
                    ('purchase', (False, False, False, False)),
                    ('supplier', (False, True, False, False)),
                ),
            ),
            StarterRoleDefinition(
                name='Kitchen & Stock Controller',
                description='Monitors pantry inventory, supplier purchases, and preparation tasks.',
                restricted_objects=(
                    ('payment', (False, False, False, False)),
                    ('campaign', (False, True, False, False)),
                ),
            ),
            StarterRoleDefinition(
                name='Cashier & Orders Clerk',
                description='Handles guest billing, invoices, payments, and promo campaigns.',
                switches=('allow_export',),
                restricted_objects=(
                    ('account', (True, True, True, False)),
                ),
            ),
        ),
    ),
    BusinessTypeDefinition(
        key='financial_services',
        label='Financial services',
        description='Client advisory, contracts, recurring billing, policies, and support cases.',
        default_contact_label='Contact',
        pack_objects=(
            'lead', 'deal', 'product', 'contract', 'ticket', 'customer_subscription',
            'campaign', 'policy', 'appointment', 'job', 'invoice', 'payment',
        ),
        starter_roles=(
            StarterRoleDefinition(
                name='Branch Operations Lead',
                description='Manages advisory pipelines, client policies, and SLA ticket resolution.',
                switches=('allow_import', 'allow_export', 'automations'),
                field_rights=(('deal', 'estimated_margin', 'read_only'),),
            ),
            StarterRoleDefinition(
                name='Relationship Officer',
                description='Works client leads, deals, consultations, and recurring advisory contracts.',
                restricted_objects=(
                    ('account', (True, True, True, False)),
                    ('deal', (True, True, True, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_FRONTLINE,
            ),
            StarterRoleDefinition(
                name='Policy & Subscriptions Specialist',
                description='Administers client policies, renewal schedules, and billing plans.',
                switches=('allow_export',),
                restricted_objects=(
                    ('deal', (False, True, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
            StarterRoleDefinition(
                name='Client Support Desk',
                description='Resolves client service tickets, schedules callbacks, and logs follow-ups.',
                restricted_objects=(
                    ('deal', (False, True, False, False)),
                    ('policy', (False, True, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
        ),
    ),
    BusinessTypeDefinition(
        key='education',
        label='Education',
        description='Admissions leads, academic programs, student enrollments, and fee billing.',
        default_contact_label='Student or Guardian',
        pack_objects=(
            'lead', 'deal', 'invoice', 'payment', 'campaign', 'ticket',
            'program', 'enrollment', 'appointment', 'contract', 'job', 'membership',
        ),
        starter_roles=(
            StarterRoleDefinition(
                name='Admissions Officer',
                description='Follows up inquiry leads, campus tours, and enrollment applications.',
                switches=('allow_import',),
                restricted_objects=(
                    ('account', (True, True, True, False)),
                    ('payment', (False, True, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_FRONTLINE,
            ),
            StarterRoleDefinition(
                name='Academic Registrar',
                description='Manages academic programs, student enrollments, contracts, and records.',
                switches=('allow_import', 'allow_export', 'automations'),
            ),
            StarterRoleDefinition(
                name='Bursar & Fee Collector',
                description='Issues tuition invoices and reconciles fee payments for Students and Guardians.',
                switches=('allow_export',),
                restricted_objects=(
                    ('lead', (False, True, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
            StarterRoleDefinition(
                name='Student Support Coordinator',
                description='Handles parent/student support tickets, appointments, and campus memberships.',
                restricted_objects=(
                    ('invoice', (False, True, False, False)),
                    ('deal', (False, True, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
        ),
    ),
    BusinessTypeDefinition(
        key='healthcare_wellness',
        label='Healthcare and wellness',
        description='Patient appointments, visit notes, care subscriptions, inventory, and billing.',
        default_contact_label='Patient',
        pack_objects=(
            'lead', 'ticket', 'product', 'invoice', 'payment', 'campaign',
            'appointment', 'visit', 'contract', 'inventory', 'customer_subscription', 'job',
        ),
        starter_roles=(
            StarterRoleDefinition(
                name='Clinic Operations Manager',
                description='Supervises clinic scheduling, patient care workflows, billing, and supplies.',
                switches=('allow_import', 'allow_export', 'automations'),
            ),
            StarterRoleDefinition(
                name='Front Desk Receptionist',
                description='Registers Patients, books Appointments, and checks Visit intake details.',
                restricted_objects=(
                    ('contact', (True, True, True, False)),
                    ('inventory', (False, True, False, False)),
                    ('payment', (False, True, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_HEALTH,
            ),
            StarterRoleDefinition(
                name='Patient Care Coordinator',
                description='Records Visits, tracks follow-up plans, and resolves Patient support tickets.',
                restricted_objects=(
                    ('contact', (True, True, True, False)),
                    ('invoice', (False, True, False, False)),
                ),
            ),
            StarterRoleDefinition(
                name='Billing & Pharmacy Stock Clerk',
                description='Manages clinic invoices, Patient care subscriptions, and supply stock.',
                switches=('allow_export',),
                restricted_objects=(
                    ('visit', (False, True, False, False)),
                ),
                field_rights=(('visit', 'notes', 'hidden'),),
            ),
        ),
    ),
    BusinessTypeDefinition(
        key='construction_real_estate',
        label='Construction and real estate',
        description='Properties, site jobs, contractor procurement, materials stock, and invoicing.',
        default_contact_label='Contact',
        pack_objects=(
            'lead', 'deal', 'invoice', 'payment', 'contract', 'inventory',
            'job', 'property', 'appointment', 'supplier', 'purchase', 'shipment',
        ),
        starter_roles=(
            StarterRoleDefinition(
                name='Project Manager',
                description='Oversees site jobs, property milestones, contracts, and supplier deliveries.',
                switches=('allow_import', 'allow_export', 'automations'),
            ),
            StarterRoleDefinition(
                name='Property Sales Agent',
                description='Handles buyer leads, site viewing appointments, and property sales deals.',
                restricted_objects=(
                    ('purchase', (False, False, False, False)),
                    ('supplier', (False, True, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_FRONTLINE,
            ),
            StarterRoleDefinition(
                name='Site & Materials Supervisor',
                description='Logs construction jobs, site material stock, purchases, and shipments.',
                restricted_objects=(
                    ('deal', (False, True, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
            StarterRoleDefinition(
                name='Quantity & Billing Officer',
                description='Manages client stage invoices, supplier bills, and project payments.',
                switches=('allow_export',),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
        ),
    ),
    BusinessTypeDefinition(
        key='transport_logistics_storage',
        label='Transport, logistics, and storage',
        description='Fleet vehicles, cargo shipments, dispatch jobs, contracts, and tracked assets.',
        default_contact_label='Contact',
        pack_objects=(
            'lead', 'deal', 'ticket', 'invoice', 'payment', 'contract',
            'vehicle', 'shipment', 'job', 'appointment', 'asset', 'supplier',
        ),
        starter_roles=(
            StarterRoleDefinition(
                name='Logistics Operations Manager',
                description='Manages fleet allocation, haulage contracts, dispatch jobs, and corridor SLAs.',
                switches=('allow_import', 'allow_export', 'automations'),
            ),
            StarterRoleDefinition(
                name='Fleet & Dispatch Coordinator',
                description='Schedules haulage jobs, assigns vehicles, and updates active shipment tracking.',
                restricted_objects=(
                    ('deal', (False, True, False, False)),
                    ('payment', (False, False, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
            StarterRoleDefinition(
                name='Commercial Freight Rep',
                description='Develops shipper accounts, leads, and corridor haulage opportunities.',
                restricted_objects=(
                    ('account', (True, True, True, False)),
                    ('vehicle', (False, True, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_FRONTLINE,
            ),
            StarterRoleDefinition(
                name='Waybill & Billing Clerk',
                description='Handles freight invoicing, customer payments, and delivery support tickets.',
                switches=('allow_export',),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
        ),
    ),
    BusinessTypeDefinition(
        key='ict_digital_services',
        label='ICT and digital services',
        description='Software projects, recurring client subscriptions, support tickets, and assets.',
        default_contact_label='Contact',
        pack_objects=(
            'lead', 'deal', 'ticket', 'customer_subscription', 'invoice', 'payment',
            'contract', 'campaign', 'job', 'asset', 'appointment', 'product',
        ),
        starter_roles=(
            StarterRoleDefinition(
                name='Delivery & Support Manager',
                description='Coordinates implementation jobs, SLA tickets, subscriptions, and renewals.',
                switches=('allow_import', 'allow_export', 'automations'),
            ),
            StarterRoleDefinition(
                name='Solutions Sales Executive',
                description='Owns B2B software leads, demos, proposals, and commercial contracts.',
                restricted_objects=(
                    ('account', (True, True, True, False)),
                    ('asset', (False, True, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_FRONTLINE,
            ),
            StarterRoleDefinition(
                name='Technical Support Engineer',
                description='Resolves client tickets, manages IT assets, and executes deployment jobs.',
                restricted_objects=(
                    ('deal', (False, True, False, False)),
                    ('payment', (False, False, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
            StarterRoleDefinition(
                name='SaaS Billing Coordinator',
                description='Manages customer subscriptions, milestone invoices, and payment reconciliation.',
                switches=('allow_export',),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
        ),
    ),
    BusinessTypeDefinition(
        key='professional_services',
        label='Professional services',
        description='Consulting pipelines, retainers, client jobs, appointments, and invoicing.',
        default_contact_label='Contact',
        pack_objects=(
            'lead', 'deal', 'ticket', 'invoice', 'payment', 'contract',
            'campaign', 'job', 'appointment', 'product', 'line_item', 'membership',
        ),
        starter_roles=(
            StarterRoleDefinition(
                name='Practice Manager',
                description='Oversees engagement pipelines, client retainers, staffing jobs, and billing.',
                switches=('allow_import', 'allow_export', 'automations'),
            ),
            StarterRoleDefinition(
                name='Client Engagement Associate',
                description='Supports business development leads, advisory deals, and client meetings.',
                restricted_objects=(
                    ('account', (True, True, True, False)),
                    ('deal', (True, True, True, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_FRONTLINE,
            ),
            StarterRoleDefinition(
                name='Delivery Consultant',
                description='Executes client engagements, logs consultation calls, and closes project tasks.',
                restricted_objects=(
                    ('payment', (False, False, False, False)),
                    ('campaign', (False, True, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
            StarterRoleDefinition(
                name='Retainer Billing Officer',
                description='Prepares retainer invoices, fee schedules, and payment records.',
                switches=('allow_export',),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
        ),
    ),
    BusinessTypeDefinition(
        key='hospitality_tourism',
        label='Hospitality and tourism',
        description='Guest bookings, memberships, itineraries, inventory, and customer billing.',
        default_contact_label='Contact',
        pack_objects=(
            'lead', 'deal', 'invoice', 'payment', 'campaign', 'ticket',
            'booking', 'appointment', 'membership', 'inventory', 'product', 'job',
        ),
        starter_roles=(
            StarterRoleDefinition(
                name='Property & Guest Manager',
                description='Oversees room/tour bookings, VIP memberships, group deals, and operations.',
                switches=('allow_import', 'allow_export', 'automations'),
            ),
            StarterRoleDefinition(
                name='Front Desk & Reservations Agent',
                description='Confirms guest bookings, itinerary appointments, and service tickets.',
                restricted_objects=(
                    ('inventory', (False, True, False, False)),
                    ('deal', (True, True, True, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_FRONTLINE,
            ),
            StarterRoleDefinition(
                name='Concierge & Tours Coordinator',
                description='Arranges excursion jobs, loyalty memberships, and guest follow-ups.',
                restricted_objects=(
                    ('payment', (False, True, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
            StarterRoleDefinition(
                name='Night Audit & Accounts Clerk',
                description='Reconciles guest folios, invoices, payments, and amenity stock.',
                switches=('allow_export',),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
        ),
    ),
    BusinessTypeDefinition(
        key='manufacturing_light_industry',
        label='Manufacturing and light industry',
        description='Production jobs, factory assets, raw material purchasing, stock, and shipments.',
        default_contact_label='Contact',
        pack_objects=(
            'deal', 'product', 'line_item', 'invoice', 'payment', 'inventory',
            'contract', 'supplier', 'purchase', 'job', 'asset', 'shipment',
        ),
        starter_roles=(
            StarterRoleDefinition(
                name='Plant Operations Manager',
                description='Oversees production runs, factory assets, raw materials, and contracts.',
                switches=('allow_import', 'allow_export', 'automations'),
            ),
            StarterRoleDefinition(
                name='Production & Maintenance Lead',
                description='Manages work-order jobs, equipment assets, and quality checks.',
                restricted_objects=(
                    ('deal', (False, True, False, False)),
                    ('payment', (False, False, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
            StarterRoleDefinition(
                name='Materials & Purchasing Officer',
                description='Coordinates suppliers, raw material purchase orders, and stock levels.',
                switches=('allow_import',),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
            StarterRoleDefinition(
                name='Industrial Sales & Dispatch',
                description='Manages wholesale deals, finished-goods shipments, and invoicing.',
                switches=('allow_export',),
                field_rights=_FIELD_RESTRICTIONS_FRONTLINE,
            ),
        ),
    ),
    BusinessTypeDefinition(
        key='telecom_airtime_services',
        label='Telecom and airtime services',
        description='Subscriber plans, field assets, support tickets, outreach campaigns, and billing.',
        default_contact_label='Contact',
        pack_objects=(
            'lead', 'deal', 'product', 'customer_subscription', 'ticket', 'invoice',
            'payment', 'campaign', 'asset', 'job', 'contract', 'appointment',
        ),
        starter_roles=(
            StarterRoleDefinition(
                name='Network & Commercial Manager',
                description='Supervises regional enterprise deals, subscriber plans, and SLA compliance.',
                switches=('allow_import', 'allow_export', 'automations'),
            ),
            StarterRoleDefinition(
                name='Enterprise Sales & Agent Rep',
                description='Converts B2B connectivity leads, corporate deals, and airtime contracts.',
                restricted_objects=(
                    ('account', (True, True, True, False)),
                    ('asset', (False, True, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_FRONTLINE,
            ),
            StarterRoleDefinition(
                name='NOC & Field Support Tech',
                description='Resolves outage tickets, tracks field assets, and completes installation jobs.',
                restricted_objects=(
                    ('deal', (False, True, False, False)),
                    ('payment', (False, False, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
            StarterRoleDefinition(
                name='Subscriptions & Collections Clerk',
                description='Handles recurring subscriber billing, invoices, and payment postings.',
                switches=('allow_export',),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
        ),
    ),
    BusinessTypeDefinition(
        key='nonprofit_faith_membership',
        label='Nonprofit, faith, and membership',
        description='Donor and member care, outreach campaigns, community programs, and memberships.',
        default_contact_label='Donor or Member',
        pack_objects=(
            'lead', 'campaign', 'invoice', 'payment', 'ticket', 'membership',
            'appointment', 'job', 'contract', 'program', 'enrollment', 'asset',
        ),
        starter_roles=(
            StarterRoleDefinition(
                name='Program & Outreach Director',
                description='Coordinates community programs, outreach campaigns, and partner contracts.',
                switches=('allow_import', 'allow_export', 'automations'),
            ),
            StarterRoleDefinition(
                name='Member & Donor Care Officer',
                description='Maintains Donor/Member profiles, memberships, enrollments, and care appointments.',
                restricted_objects=(
                    ('contact', (True, True, True, False)),
                    ('asset', (False, True, False, False)),
                ),
            ),
            StarterRoleDefinition(
                name='Stewardship & Grants Clerk',
                description='Records pledge invoices, contributions, and grant asset tracking.',
                switches=('allow_export',),
                restricted_objects=(
                    ('campaign', (False, True, False, False)),
                ),
            ),
            StarterRoleDefinition(
                name='Volunteer & Field Coordinator',
                description='Organizes field outreach jobs, inquiries, and community support cases.',
                restricted_objects=(
                    ('payment', (False, False, False, False)),
                ),
            ),
        ),
    ),
    BusinessTypeDefinition(
        key='beauty_personal_care',
        label='Beauty and personal care',
        description='Salon appointments, retail products, care memberships, and recurring packages.',
        default_contact_label='Contact',
        pack_objects=(
            'lead', 'product', 'invoice', 'payment', 'campaign', 'appointment',
            'inventory', 'membership', 'ticket', 'job', 'line_item', 'customer_subscription',
        ),
        starter_roles=(
            StarterRoleDefinition(
                name='Salon & Studio Manager',
                description='Manages stylist schedules, care memberships, retail stock, and campaigns.',
                switches=('allow_import', 'allow_export', 'automations'),
            ),
            StarterRoleDefinition(
                name='Front Desk Booking Desk',
                description='Schedules client appointments, handles inquiries, and manages memberships.',
                restricted_objects=(
                    ('inventory', (False, True, False, False)),
                ),
            ),
            StarterRoleDefinition(
                name='Senior Specialist / Stylist',
                description='Views assigned client appointments, treatment tasks, and follow-up preferences.',
                restricted_objects=(
                    ('payment', (False, False, False, False)),
                    ('campaign', (False, True, False, False)),
                ),
            ),
            StarterRoleDefinition(
                name='POS & Retail Stock Clerk',
                description='Tracks beauty product stock, subscriptions, client invoices, and payments.',
                switches=('allow_export',),
            ),
        ),
    ),
    BusinessTypeDefinition(
        key='automotive',
        label='Automotive',
        description='Vehicle tracking, workshop service jobs, parts inventory, suppliers, and billing.',
        default_contact_label='Contact',
        pack_objects=(
            'lead', 'deal', 'product', 'line_item', 'invoice', 'payment',
            'inventory', 'ticket', 'vehicle', 'job', 'appointment', 'supplier',
        ),
        starter_roles=(
            StarterRoleDefinition(
                name='Service Workshop Manager',
                description='Oversees service bays, repair jobs, vehicle intake, and parts availability.',
                switches=('allow_import', 'allow_export', 'automations'),
            ),
            StarterRoleDefinition(
                name='Service Advisor',
                description='Books vehicle drop-offs, opens service estimates/deals, and updates owners.',
                restricted_objects=(
                    ('account', (True, True, True, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_FRONTLINE,
            ),
            StarterRoleDefinition(
                name='Spare Parts & Stores Clerk',
                description='Manages parts inventory, suppliers, and workshop line-item allocations.',
                restricted_objects=(
                    ('deal', (False, True, False, False)),
                    ('payment', (False, False, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
            StarterRoleDefinition(
                name='Diagnostic & Support Lead',
                description='Tracks workshop jobs, vehicle logs, and post-service warranty tickets.',
                restricted_objects=(
                    ('deal', (False, True, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
        ),
    ),
    BusinessTypeDefinition(
        key='media_creative_agencies',
        label='Media and creative agencies',
        description='Client pitches, production jobs, creative assets, campaigns, and retainer billing.',
        default_contact_label='Contact',
        pack_objects=(
            'lead', 'deal', 'campaign', 'invoice', 'payment', 'contract',
            'ticket', 'job', 'appointment', 'asset', 'product', 'line_item',
        ),
        starter_roles=(
            StarterRoleDefinition(
                name='Account & Agency Producer',
                description='Leads client pitches, production jobs, retainer contracts, and billing.',
                switches=('allow_import', 'allow_export', 'automations'),
            ),
            StarterRoleDefinition(
                name='Business Development Rep',
                description='Qualifies brand leads, manages pitch deals, and schedules discovery calls.',
                restricted_objects=(
                    ('account', (True, True, True, False)),
                    ('asset', (False, True, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_FRONTLINE,
            ),
            StarterRoleDefinition(
                name='Creative & Production Lead',
                description='Executes deliverable jobs, tracks creative assets, and handles client feedback tickets.',
                restricted_objects=(
                    ('deal', (False, True, False, False)),
                    ('payment', (False, False, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
            StarterRoleDefinition(
                name='Finance & Retainer Billing',
                description='Issues campaign/retainer invoices and reconciles client payments.',
                switches=('allow_export',),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
        ),
    ),
    BusinessTypeDefinition(
        key='energy_fuel',
        label='Energy and fuel',
        description='Fuel stock, supplier deliveries, fleet vehicles, site assets, and commercial invoices.',
        default_contact_label='Contact',
        pack_objects=(
            'product', 'inventory', 'invoice', 'payment', 'contract', 'supplier',
            'purchase', 'asset', 'vehicle', 'shipment', 'job', 'ticket',
        ),
        starter_roles=(
            StarterRoleDefinition(
                name='Depot & Station Manager',
                description='Supervises depot fuel levels, commercial contracts, and safety compliance jobs.',
                switches=('allow_import', 'allow_export', 'automations'),
            ),
            StarterRoleDefinition(
                name='Dispatch & Fleet Officer',
                description='Coordinates tanker vehicles, bulk fuel shipments, and delivery tickets.',
                restricted_objects=(
                    ('payment', (False, False, False, False)),
                ),
            ),
            StarterRoleDefinition(
                name='Procurement & Inventory Analyst',
                description='Tracks station tank stock, supplier purchases, and site equipment assets.',
                switches=('allow_import',),
            ),
            StarterRoleDefinition(
                name='Commercial Billing Officer',
                description='Manages bulk account contracts, invoices, and settlement payments.',
                switches=('allow_export',),
                restricted_objects=(
                    ('vehicle', (False, True, False, False)),
                ),
            ),
        ),
    ),
    BusinessTypeDefinition(
        key='general_services',
        label='General services',
        description='Balanced sales pipeline, jobs, appointments, support tickets, contracts, and invoicing.',
        default_contact_label='Contact',
        pack_objects=(
            'lead', 'deal', 'job', 'appointment', 'ticket', 'product',
            'line_item', 'invoice', 'payment', 'contract', 'campaign', 'supplier',
        ),
        starter_roles=(
            StarterRoleDefinition(
                name='Operations Manager',
                description='Oversees service delivery jobs, client contracts, appointments, and SLA resolution.',
                switches=('allow_import', 'allow_export', 'automations'),
                field_rights=(('deal', 'estimated_margin', 'read_only'),),
            ),
            StarterRoleDefinition(
                name='Sales Representative',
                description='Qualifies leads, drives pipeline deals, and books client consultations.',
                restricted_objects=(
                    ('account', (True, True, True, False)),
                    ('deal', (True, True, True, False)),
                    ('invoice', (False, True, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_FRONTLINE,
            ),
            StarterRoleDefinition(
                name='Field Technician',
                description='Executes assigned field jobs, logs site notes, and checks appointment schedules.',
                restricted_objects=(
                    ('deal', (False, True, False, False)),
                    ('payment', (False, False, False, False)),
                    ('supplier', (False, True, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
            StarterRoleDefinition(
                name='Support & Billing Specialist',
                description='Resolves client tickets, manages contracts, and issues customer invoices.',
                switches=('allow_export',),
                restricted_objects=(
                    ('deal', (False, True, False, False)),
                ),
                field_rights=_FIELD_RESTRICTIONS_STANDARD,
            ),
        ),
    ),
)

BUSINESS_TYPES_BY_KEY: dict[str, BusinessTypeDefinition] = {
    bt.key: bt for bt in BUSINESS_TYPES
}
BUSINESS_TYPE_CHOICES: list[tuple[str, str]] = [
    (bt.key, bt.label) for bt in BUSINESS_TYPES
]
