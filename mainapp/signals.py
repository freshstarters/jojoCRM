from django.contrib.auth.signals import user_logged_in
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import (
    Account,
    Activity,
    Appointment,
    Contact,
    Deal,
    Job,
    Lead,
    Pipeline,
    PipelineStage,
    Role,
    Ticket,
    User,
    Visit,
    Workspace,
    WorkspacePack,
    WorkspaceTable,
)
from .service.cache_service import (
    ALL_WORKSPACE_DOMAINS,
    DOMAIN_CATALOG,
    DOMAIN_LOOKUP_RECORDS,
    DOMAIN_MEMBERS,
    DOMAIN_RECORDS,
    DOMAIN_ROLES,
    invalidate_workspace_cache,
)
from .service.session_service import sync_session_version


@receiver(user_logged_in)
def handle_user_logged_in(sender, request, user, **kwargs):
    """Persist the user's current session_version when a Django session starts."""
    sync_session_version(request, user)


@receiver(post_save, sender=Workspace)
def invalidate_on_workspace_save(sender, instance, **kwargs):
    invalidate_workspace_cache(instance.pk, domains=(DOMAIN_CATALOG,))


@receiver(post_delete, sender=Workspace)
def invalidate_on_workspace_delete(sender, instance, **kwargs):
    invalidate_workspace_cache(instance.pk, domains=ALL_WORKSPACE_DOMAINS)


@receiver(post_save, sender=WorkspacePack)
@receiver(post_delete, sender=WorkspacePack)
@receiver(post_save, sender=WorkspaceTable)
@receiver(post_delete, sender=WorkspaceTable)
def invalidate_on_catalog_model_change(sender, instance, **kwargs):
    invalidate_workspace_cache(getattr(instance, 'workspace_id', None), domains=(DOMAIN_CATALOG,))


@receiver(post_save, sender=Role)
@receiver(post_delete, sender=Role)
def invalidate_on_role_change(sender, instance, **kwargs):
    invalidate_workspace_cache(getattr(instance, 'workspace_id', None), domains=(DOMAIN_ROLES,))


@receiver(post_save, sender=User)
def invalidate_on_user_save(sender, instance, update_fields=None, **kwargs):
    if update_fields is not None and frozenset(update_fields) <= {'last_login'}:
        return
    invalidate_workspace_cache(
        getattr(instance, 'workspace_id', None),
        domains=(DOMAIN_MEMBERS, DOMAIN_ROLES),
    )


@receiver(post_delete, sender=User)
def invalidate_on_user_delete(sender, instance, **kwargs):
    invalidate_workspace_cache(
        getattr(instance, 'workspace_id', None),
        domains=(DOMAIN_MEMBERS, DOMAIN_ROLES),
    )


@receiver(post_save, sender=Pipeline)
@receiver(post_delete, sender=Pipeline)
@receiver(post_save, sender=PipelineStage)
@receiver(post_delete, sender=PipelineStage)
@receiver(post_save, sender=Account)
@receiver(post_delete, sender=Account)
@receiver(post_save, sender=Contact)
@receiver(post_delete, sender=Contact)
def invalidate_on_lookup_record_change(sender, instance, **kwargs):
    invalidate_workspace_cache(
        getattr(instance, 'workspace_id', None),
        domains=(DOMAIN_RECORDS, DOMAIN_LOOKUP_RECORDS),
    )


@receiver(post_save, sender=Activity)
@receiver(post_delete, sender=Activity)
@receiver(post_save, sender=Lead)
@receiver(post_delete, sender=Lead)
@receiver(post_save, sender=Deal)
@receiver(post_delete, sender=Deal)
@receiver(post_save, sender=Job)
@receiver(post_delete, sender=Job)
@receiver(post_save, sender=Appointment)
@receiver(post_delete, sender=Appointment)
@receiver(post_save, sender=Visit)
@receiver(post_delete, sender=Visit)
@receiver(post_save, sender=Ticket)
@receiver(post_delete, sender=Ticket)
def invalidate_on_crm_record_change(sender, instance, **kwargs):
    invalidate_workspace_cache(
        getattr(instance, 'workspace_id', None),
        domains=(DOMAIN_RECORDS,),
    )
