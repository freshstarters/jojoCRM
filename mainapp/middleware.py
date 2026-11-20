from django.contrib.auth import logout

from .service.role_service import compute_user_permissions_version
from .service.session_service import SESSION_VERSION_KEY


class ActiveSessionEnforcementMiddleware:
    """Log out sessions immediately when deactivated and emit live X-Permissions-Version headers."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if user is not None and getattr(user, 'is_authenticated', False):
            session_ver = request.session.get(SESSION_VERSION_KEY)
            # A session without a stored version predates versioning and is treated as version 1,
            # so any rotation above 1 also revokes it.
            expected_ver = session_ver if session_ver is not None else 1
            user_ver = getattr(user, 'session_version', 1)
            if not getattr(user, 'is_active', False) or expected_ver != user_ver:
                logout(request)

        response = self.get_response(request)

        user = getattr(request, 'user', None)
        if (
            user is not None
            and getattr(user, 'is_authenticated', False)
            and getattr(user, 'is_active', False)
            and getattr(user, 'email_verified', False)
            and getattr(user, 'workspace_id', None) is not None
        ):
            try:
                version = compute_user_permissions_version(user, user.workspace)
                if version:
                    response['X-Permissions-Version'] = version
            except Exception:
                pass
        return response
