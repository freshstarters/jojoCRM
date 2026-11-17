from decimal import Decimal

from django.db import models


class WorkspaceRecordBase(models.Model):
    """Abstract base for tenant-scoped CRM records with ownership and sharing fields."""

    VISIBILITY_WORKSPACE = 'workspace'
    VISIBILITY_ROLE = 'role'
    VISIBILITY_OWNER = 'owner'

    VISIBILITY_CHOICES = [
        (VISIBILITY_WORKSPACE, 'Whole workspace'),
        (VISIBILITY_ROLE, 'Role'),
        (VISIBILITY_OWNER, 'Owner only'),
    ]

    workspace = models.ForeignKey(
        'mainapp.Workspace',
        on_delete=models.CASCADE,
        related_name='%(class)s_records',
    )
    owner = models.ForeignKey(
        'mainapp.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='owned_%(class)s_records',
    )
    created_by = models.ForeignKey(
        'mainapp.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='created_%(class)s_records',
    )
    visibility = models.CharField(
        max_length=20,
        choices=VISIBILITY_CHOICES,
        default=VISIBILITY_WORKSPACE,
    )
    shared_role = models.ForeignKey(
        'mainapp.Role',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='shared_%(class)s_records',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Account(WorkspaceRecordBase):
    """Core company or organization record belonging to one workspace."""

    OBJECT_KEY = 'account'

    name = models.CharField(max_length=255)
    city = models.CharField(max_length=120, blank=True, default='')
    phone = models.CharField(max_length=64, blank=True, default='')
    email = models.CharField(max_length=254, blank=True, default='')
    notes = models.TextField(blank=True, default='')
    source_lead = models.ForeignKey(
        'mainapp.Lead',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='converted_accounts',
    )

    class Meta:
        ordering = ['-updated_at', '-id']
        indexes = [
            models.Index(fields=['workspace', 'name'], name='acc_ws_name_idx'),
            models.Index(fields=['workspace', '-updated_at'], name='acc_ws_updated_idx'),
        ]

    def __str__(self):
        return f"{self.name} ({self.workspace_id})"


class Contact(WorkspaceRecordBase):
    """Core person record that may sit on an Account."""

    OBJECT_KEY = 'contact'

    account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='contacts',
    )
    name = models.CharField(max_length=255)
    title = models.CharField(max_length=150, blank=True, default='')
    email = models.CharField(max_length=254, blank=True, default='')
    phone = models.CharField(max_length=64, blank=True, default='')
    sms_consent = models.BooleanField(default=False)
    notes = models.TextField(blank=True, default='')
    source_lead = models.ForeignKey(
        'mainapp.Lead',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='converted_contacts',
    )

    class Meta:
        ordering = ['-updated_at', '-id']
        indexes = [
            models.Index(fields=['workspace', 'name'], name='con_ws_name_idx'),
            models.Index(fields=['workspace', 'account'], name='con_ws_account_idx'),
        ]

    def __str__(self):
        return f"{self.name} ({self.workspace_id})"


class Lead(WorkspaceRecordBase):
    """Pack prospect record kept separate until conversion."""

    OBJECT_KEY = 'lead'
    STATUS_OPEN = 'open'
    STATUS_CONVERTED = 'converted'
    STATUS_CHOICES = [
        (STATUS_OPEN, 'Open'),
        (STATUS_CONVERTED, 'Converted'),
    ]

    name = models.CharField(max_length=255)
    company = models.CharField(max_length=255, blank=True, default='')
    email = models.CharField(max_length=254, blank=True, default='')
    phone = models.CharField(max_length=64, blank=True, default='')
    sms_consent = models.BooleanField(default=False)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_OPEN)
    notes = models.TextField(blank=True, default='')
    converted_at = models.DateTimeField(null=True, blank=True)
    converted_by = models.ForeignKey(
        'mainapp.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='converted_leads',
    )
    converted_account = models.ForeignKey(
        'mainapp.Account',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='source_leads',
    )
    converted_contact = models.ForeignKey(
        'mainapp.Contact',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='source_leads',
    )
    converted_deal = models.ForeignKey(
        'mainapp.Deal',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='source_leads',
    )

    class Meta:
        ordering = ['-updated_at', '-id']
        indexes = [
            models.Index(fields=['workspace', 'status', '-updated_at'], name='lead_ws_status_idx'),
        ]

    def __str__(self):
        return f"{self.name} ({self.workspace_id})"


class Deal(WorkspaceRecordBase):
    """Pack sales opportunity positioned on a pipeline stage."""

    OBJECT_KEY = 'deal'
    DEFAULT_STAGES = ('New', 'Qualified', 'Proposal', 'Won', 'Lost')

    title = models.CharField(max_length=255)
    account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='deals',
    )
    contact = models.ForeignKey(
        Contact,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='deals',
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    stage = models.CharField(max_length=100, default='New')
    target_close_date = models.DateField(null=True, blank=True)
    win_probability = models.PositiveSmallIntegerField(null=True, blank=True)
    estimated_margin = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
    )
    notes = models.TextField(blank=True, default='')
    source_lead = models.ForeignKey(
        'mainapp.Lead',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='converted_deals',
    )

    class Meta:
        ordering = ['-updated_at', '-id']
        indexes = [
            models.Index(fields=['workspace', 'stage', '-updated_at'], name='deal_ws_stage_idx'),
        ]

    def __str__(self):
        return f"{self.title} ({self.workspace_id})"


class Job(WorkspaceRecordBase):
    """Pack operational work item with status, assignee, due date, and linked Account or Contact."""

    OBJECT_KEY = 'job'
    DEFAULT_STATUSES = ('Pending', 'In Progress', 'Completed', 'Cancelled')

    title = models.CharField(max_length=255)
    status = models.CharField(max_length=64, default='Pending')
    assignee = models.ForeignKey(
        'mainapp.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='assigned_jobs',
    )
    due_date = models.DateField(null=True, blank=True)
    account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='jobs',
    )
    contact = models.ForeignKey(
        Contact,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='jobs',
    )
    notes = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-updated_at', '-id']
        indexes = [
            models.Index(fields=['workspace', 'status', 'due_date'], name='job_ws_status_due_idx'),
        ]

    def __str__(self):
        return f"{self.title} ({self.workspace_id})"


class Appointment(WorkspaceRecordBase):
    """Pack scheduled time record with status, assignee, and linked Contact."""

    OBJECT_KEY = 'appointment'
    DEFAULT_STATUSES = ('Scheduled', 'Confirmed', 'Completed', 'Cancelled', 'No-show')

    title = models.CharField(max_length=255)
    scheduled_at = models.DateTimeField()
    status = models.CharField(max_length=64, default='Scheduled')
    assignee = models.ForeignKey(
        'mainapp.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='assigned_appointments',
    )
    contact = models.ForeignKey(
        Contact,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='appointments',
    )
    notes = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['scheduled_at', '-id']
        indexes = [
            models.Index(fields=['workspace', 'scheduled_at'], name='apt_ws_scheduled_idx'),
        ]

    def __str__(self):
        return f"{self.title} ({self.workspace_id})"


class Ticket(WorkspaceRecordBase):
    """Pack support case linked to an Account and/or Contact."""

    OBJECT_KEY = 'ticket'
    DEFAULT_STATUSES = ('New', 'Open', 'Pending', 'Resolved', 'Closed')
    DEFAULT_PRIORITIES = ('High', 'Normal', 'Low')

    title = models.CharField(max_length=255)
    status = models.CharField(max_length=64, default='New')
    priority = models.CharField(max_length=32, default='Normal')
    account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='tickets',
    )
    contact = models.ForeignKey(
        Contact,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='tickets',
    )
    assignee = models.ForeignKey(
        'mainapp.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='assigned_tickets',
    )
    description = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-updated_at', '-id']
        indexes = [
            models.Index(fields=['workspace', 'status', 'priority'], name='tck_ws_status_prio_idx'),
        ]

    def __str__(self):
        return f"{self.title} ({self.workspace_id})"


class Activity(WorkspaceRecordBase):
    """Core note, task, call, or meeting logged against a workspace CRM record."""

    OBJECT_KEY = 'activity'
    TYPE_NOTE = 'note'
    TYPE_TASK = 'task'
    TYPE_CALL = 'call'
    TYPE_MEETING = 'meeting'
    VALID_SPRINT2_TYPES = (TYPE_NOTE, TYPE_TASK, TYPE_CALL, TYPE_MEETING)

    TYPE_CHOICES = [
        (TYPE_NOTE, 'Note'),
        (TYPE_TASK, 'Task'),
        (TYPE_CALL, 'Call'),
        (TYPE_MEETING, 'Meeting'),
    ]

    VALID_TARGET_OBJECTS = (
        'account',
        'contact',
        'lead',
        'deal',
        'job',
        'appointment',
        'ticket',
    )

    activity_type = models.CharField(
        max_length=32,
        choices=TYPE_CHOICES,
        default=TYPE_NOTE,
    )
    target_object_type = models.CharField(max_length=64, db_index=True)
    target_object_id = models.PositiveBigIntegerField(db_index=True)
    summary = models.TextField()
    due_at = models.DateTimeField(null=True, blank=True)
    is_completed = models.BooleanField(default=False)
    assignee = models.ForeignKey(
        'mainapp.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='assigned_activities',
    )

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(
                fields=['workspace', 'target_object_type', 'target_object_id', '-created_at'],
                name='act_ws_target_idx',
            ),
        ]

    def __str__(self):
        return f"{self.activity_type} on {self.target_object_type}:{self.target_object_id}"
