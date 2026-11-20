from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone


class Workspace(models.Model):
    name = models.CharField(max_length=150)
    slug = models.SlugField(max_length=80, unique=True)
    business_type = models.CharField(max_length=64, default='general_services')
    business_type_label = models.CharField(max_length=150, default='General services')
    contact_label = models.CharField(max_length=80, default='Contact')
    created_by = models.ForeignKey(
        'mainapp.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='created_workspaces',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f"{self.name} ({self.slug})"


class WorkspacePack(models.Model):
    """Tracks which business-type starter packs are active on a workspace."""

    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name='packs',
    )
    pack_key = models.CharField(max_length=64)
    is_primary = models.BooleanField(default=False)
    enabled_by = models.ForeignKey(
        'mainapp.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='enabled_workspace_packs',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-is_primary', 'created_at', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['workspace', 'pack_key'],
                name='unique_pack_per_workspace',
            ),
            models.UniqueConstraint(
                fields=['workspace'],
                condition=models.Q(is_primary=True),
                name='unique_primary_pack_per_workspace',
            ),
        ]

    def __str__(self):
        return f"{self.workspace.slug}:{self.pack_key}"


class WorkspaceTable(models.Model):
    """Tracks the core and pack catalog tables active inside a workspace."""

    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name='tables',
    )
    object_key = models.CharField(max_length=64)
    is_core = models.BooleanField(default=False)
    is_enabled = models.BooleanField(default=True)
    source_pack_key = models.CharField(max_length=64, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_core', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['workspace', 'object_key'],
                name='unique_object_per_workspace',
            ),
            models.CheckConstraint(
                condition=models.Q(is_core=False) | models.Q(is_enabled=True),
                name='core_workspace_table_always_enabled',
            ),
        ]

    def __str__(self):
        return f"{self.workspace.slug}:{self.object_key}"


class UserManager(BaseUserManager):
    def create_user(self, email, workspace, password=None, **extra_fields):
        if not email:
            raise ValueError('An email address is required.')
        if workspace is None:
            raise ValueError('A user must belong to a workspace.')
        normalized_email = self.normalize_email(email).strip().lower()
        extra_fields.setdefault('timezone', 'Africa/Accra')
        user = self.model(email=normalized_email, workspace=workspace, **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email, workspace, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        return self.create_user(email=email, workspace=workspace, password=password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name='users',
    )
    role = models.ForeignKey(
        'mainapp.Role',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='users',
    )
    timezone = models.CharField(max_length=64, default='Africa/Accra')
    email_verified = models.BooleanField(default=False)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    session_version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = UserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    class Meta:
        ordering = ['id']

    def __str__(self):
        return self.email


class EmailActionToken(models.Model):
    PURPOSE_VERIFY_EMAIL = 'verify_email'
    PURPOSE_RESET_PASSWORD = 'reset_password'

    PURPOSE_CHOICES = [
        (PURPOSE_VERIFY_EMAIL, 'Verify Email'),
        (PURPOSE_RESET_PASSWORD, 'Reset Password'),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='email_tokens',
    )
    purpose = models.CharField(max_length=32, choices=PURPOSE_CHOICES)
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.purpose} for {self.user_id}"


class ExternalIdentity(models.Model):
    PROVIDER_GOOGLE = 'google'
    PROVIDER_MICROSOFT = 'microsoft'

    PROVIDER_CHOICES = [
        (PROVIDER_GOOGLE, 'Google'),
        (PROVIDER_MICROSOFT, 'Microsoft'),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='external_identities',
    )
    provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES)
    subject = models.CharField(max_length=255)
    email_at_link = models.EmailField()
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['id']
        constraints = [
            models.UniqueConstraint(
                fields=['provider', 'subject'],
                name='unique_external_identity_provider_subject',
            ),
            models.UniqueConstraint(
                fields=['user', 'provider'],
                name='unique_external_identity_user_provider',
            ),
        ]

    def __str__(self):
        return f"{self.provider}:{self.user_id}"


class PasskeyCredential(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='passkeys',
    )
    credential_id = models.CharField(max_length=512, unique=True)
    public_key = models.TextField()
    sign_count = models.PositiveIntegerField(default=0)
    transports = models.JSONField(default=list, blank=True)
    name = models.CharField(max_length=100, default='Passkey')
    aaguid = models.CharField(max_length=64, blank=True, default='')
    backed_up = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['created_at', 'id']

    def __str__(self):
        return f"{self.name} ({self.user_id})"


class WorkspaceInvite(models.Model):
    """Single-use invitation allowing an external email to join one workspace."""

    STATUS_PENDING = 'pending'
    STATUS_ACCEPTED = 'accepted'
    STATUS_REVOKED = 'revoked'
    STATUS_EXPIRED = 'expired'

    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name='invites',
    )
    email = models.EmailField()
    role = models.ForeignKey(
        'mainapp.Role',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='invites',
    )
    invited_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='sent_invites',
    )
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    last_sent_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='accepted_invites',
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['workspace', 'email'],
                condition=models.Q(accepted_at__isnull=True, revoked_at__isnull=True),
                name='unique_active_invite_per_workspace_email',
            ),
        ]

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    def current_status(self, now=None) -> str:
        """Return pending, accepted, revoked, or expired for the invite lifecycle."""
        if self.accepted_at is not None:
            return self.STATUS_ACCEPTED
        if self.revoked_at is not None:
            return self.STATUS_REVOKED
        check_time = now or timezone.now()
        if self.expires_at <= check_time:
            return self.STATUS_EXPIRED
        return self.STATUS_PENDING

    def __str__(self):
        return f"Invite {self.email} -> {self.workspace.slug}"


class Role(models.Model):
    """Workspace-scoped permission role, starting with permanent Owner and default Staff."""

    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name='roles',
    )
    name = models.CharField(max_length=100)
    is_owner = models.BooleanField(default=False)
    is_default_staff = models.BooleanField(default=False)
    description = models.CharField(max_length=255, blank=True, default='')

    # Owner-exclusive commercial management capabilities.
    can_manage_seat_plan = models.BooleanField(default=False)
    can_manage_sms_wallet = models.BooleanField(default=False)

    # Stored PRD role switches (users_and_roles is active now; others activate in later sprints).
    allow_import = models.BooleanField(default=False)
    allow_export = models.BooleanField(default=False)
    automations = models.BooleanField(default=False)
    scripts = models.BooleanField(default=False)
    users_and_roles = models.BooleanField(default=False)
    theme_and_layouts = models.BooleanField(default=False)
    unattended_agent = models.BooleanField(default=False)

    # Permission maps populated across S2.x (object CRUD rights and field permissions).
    object_rights = models.JSONField(default=dict, blank=True)
    field_rights = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_owner', '-is_default_staff', 'name', 'id']
        constraints = [
            models.UniqueConstraint(
                Lower('name'),
                'workspace',
                name='unique_role_name_ci_per_workspace',
            ),
            models.UniqueConstraint(
                fields=['workspace'],
                condition=models.Q(is_owner=True),
                name='unique_owner_role_per_workspace',
            ),
            models.UniqueConstraint(
                fields=['workspace'],
                condition=models.Q(is_default_staff=True),
                name='unique_default_staff_role_per_workspace',
            ),
            models.CheckConstraint(
                condition=~(models.Q(is_owner=True) & models.Q(is_default_staff=True)),
                name='role_not_both_owner_and_default_staff',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(is_owner=True)
                    | (
                        models.Q(can_manage_seat_plan=False)
                        & models.Q(can_manage_sms_wallet=False)
                    )
                ),
                name='non_owner_role_cannot_manage_billing_or_sms_wallet',
            ),
        ]

    def switches_dict(self) -> dict:
        """Return the seven PRD role switches as a normalized boolean map."""
        return {
            'import': bool(self.allow_import),
            'export': bool(self.allow_export),
            'automations': bool(self.automations),
            'scripts': bool(self.scripts),
            'users_and_roles': bool(self.users_and_roles),
            'theme_and_layouts': bool(self.theme_and_layouts),
            'unattended_agent': bool(self.unattended_agent),
        }

    def __str__(self):
        return f"{self.name} ({self.workspace.slug})"


class AuditLog(models.Model):
    """Immutable workspace audit entry capturing actor, actor kind, timestamp, and before/after values."""

    ACTOR_KIND_PERSON = 'person'
    ACTOR_KIND_SCRIPT = 'script'
    ACTOR_KIND_AUTOMATION = 'automation'
    ACTOR_KIND_AGENT = 'agent'

    ACTOR_KIND_CHOICES = [
        (ACTOR_KIND_PERSON, 'Person'),
        (ACTOR_KIND_SCRIPT, 'Script'),
        (ACTOR_KIND_AUTOMATION, 'Automation'),
        (ACTOR_KIND_AGENT, 'Agent'),
    ]

    ACTION_CREATE = 'create'
    ACTION_UPDATE = 'update'
    ACTION_DELETE = 'delete'
    ACTION_ACTION = 'action'

    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name='audit_logs',
    )
    actor_kind = models.CharField(
        max_length=20,
        choices=ACTOR_KIND_CHOICES,
        default=ACTOR_KIND_PERSON,
        db_index=True,
    )
    actor_user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='audit_logs',
    )
    actor_label = models.CharField(max_length=255)
    object_type = models.CharField(max_length=64, db_index=True)
    object_id = models.CharField(max_length=64, blank=True, default='', db_index=True)
    record_label = models.CharField(max_length=255)
    action = models.CharField(max_length=32, default=ACTION_UPDATE)
    field_name = models.CharField(max_length=120)
    previous_value = models.JSONField(null=True, blank=True)
    new_value = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(
                fields=['workspace', '-created_at', '-id'],
                name='audit_ws_created_idx',
            ),
            models.Index(
                fields=['workspace', 'object_type', 'object_id', '-created_at'],
                name='audit_ws_object_idx',
            ),
        ]

    def __str__(self):
        return (
            f"[{self.actor_kind}] {self.actor_label} "
            f"{self.object_type}({self.record_label}).{self.field_name}"
        )
