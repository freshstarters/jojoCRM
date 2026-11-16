from django.contrib import admin
from .models import (
    Account,
    Activity,
    Appointment,
    AuditLog,
    Contact,
    Deal,
    ExternalIdentity,
    Job,
    Lead,
    PasskeyCredential,
    Role,
    Ticket,
    User,
    Workspace,
    WorkspaceInvite,
    WorkspacePack,
    WorkspaceTable,
)

admin.site.register(Workspace)
admin.site.register(WorkspacePack)
admin.site.register(WorkspaceTable)
admin.site.register(Role)
admin.site.register(User)
admin.site.register(ExternalIdentity)
admin.site.register(PasskeyCredential)
admin.site.register(WorkspaceInvite)
admin.site.register(AuditLog)
admin.site.register(Account)
admin.site.register(Contact)
admin.site.register(Lead)
admin.site.register(Deal)
admin.site.register(Job)
admin.site.register(Appointment)
admin.site.register(Ticket)
admin.site.register(Activity)
