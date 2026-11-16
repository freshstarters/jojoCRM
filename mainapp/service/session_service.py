from django.contrib.sessions.models import Session
from django.db.models import F
from django.utils import timezone

SESSION_VERSION_KEY = '_jojocrm_session_version'
# Upper bound on non-expired session rows decoded per invalidation. Revocation itself is
# enforced on every request by ActiveSessionEnforcementMiddleware via session_version, so
# deleting rows here is cleanup only and never needs an unbounded table scan.
MAX_SESSION_SCAN = 200


def sync_session_version(request, user) -> None:
    """Store the user's current session_version inside the active Django session."""
    if request is None or not hasattr(request, 'session') or user is None:
        return
    request.session[SESSION_VERSION_KEY] = getattr(user, 'session_version', 1)


def invalidate_user_sessions(user, *, exclude_session_key: str | None = None) -> int:
    """Bump session_version and delete a bounded set of active DB sessions owned by user."""
    if user is None or not user.pk:
        return 0

    type(user).objects.filter(pk=user.pk).update(
        session_version=F('session_version') + 1
    )
    user.refresh_from_db(fields=['session_version'])

    now = timezone.now()
    Session.objects.filter(expire_date__lt=now).delete()

    qs = Session.objects.filter(expire_date__gte=now).order_by('-expire_date')
    if exclude_session_key:
        qs = qs.exclude(session_key=exclude_session_key)

    target_user_id = str(user.pk)
    keys_to_delete = []
    for session in qs[:MAX_SESSION_SCAN]:
        try:
            decoded = session.get_decoded()
        except Exception:
            continue
        if str(decoded.get('_auth_user_id', '')) == target_user_id:
            keys_to_delete.append(session.session_key)

    if keys_to_delete:
        Session.objects.filter(session_key__in=keys_to_delete).delete()
    return len(keys_to_delete)
