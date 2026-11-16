from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

from .service.session_service import sync_session_version


@receiver(user_logged_in)
def handle_user_logged_in(sender, request, user, **kwargs):
    """Persist the user's current session_version when a Django session starts."""
    sync_session_version(request, user)
