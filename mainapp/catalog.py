from dataclasses import dataclass


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
    """Canonical business-type definition mapping to 12 catalog pack objects."""

    key: str
    label: str
    description: str
    default_contact_label: str
    pack_objects: tuple[str, ...]


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
    ),
)

BUSINESS_TYPES_BY_KEY: dict[str, BusinessTypeDefinition] = {
    bt.key: bt for bt in BUSINESS_TYPES
}
BUSINESS_TYPE_CHOICES: list[tuple[str, str]] = [
    (bt.key, bt.label) for bt in BUSINESS_TYPES
]
