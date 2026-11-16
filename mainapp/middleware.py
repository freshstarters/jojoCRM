from django.contrib.auth import logout

from .service.session_service import SESSION_VERSION_KEY


class ActiveSessionEnforcementMiddleware:
    """Log out sessions immediately when a user is deactivated or session_version rotates."""

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
        return self.get_response(request)
