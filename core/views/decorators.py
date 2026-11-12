"""Shared decorators for the HTML (cpanel) views.

``superuser_required`` gates instance-global / admin surfaces — settings,
services, backups, environments, plugins, the application log, user management,
and workspace creation. A signed-out visitor is sent to the login page; a
signed-in user who isn't an instance admin gets the console's 403 page (with the
sidebar and the reason) instead of a silent bounce to the dashboard.

JSON / ``fetch()`` endpoints must NOT use this: an HTML 403 page or a 302 to the
login page is useless to a JSON client, so those endpoints do an in-body
``request.user.is_superuser`` check and return a JSON 403 instead (e.g. the
settings and services JSON endpoints).
"""
from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied

ADMIN_ONLY_MESSAGE = "Only instance admins can open this page."

# Server set-up hints (env var names, management commands) are for the people
# who run the server. Everyone else is told to ask them (2.5 test pass 8, F4).
ENCRYPTION_MISSING_MESSAGE = (
    "Encryption isn't set up on this server yet. Ask an administrator to set up encryption."
)
DATA_SERVER_MISSING_MESSAGE = (
    "No data server is attached to this server. Ask an administrator to attach one."
)


def operator_message(user, for_operators: str, for_others: str) -> str:
    """The set-up hint for a superuser, the neutral text for anyone else."""
    return for_operators if getattr(user, "is_superuser", False) else for_others


def superuser_required(view_func):
    """Require an authenticated superuser.

    Signed out: redirect to ``LOGIN_URL`` (``auth:login``) with ``next``.
    Signed in without superuser: raise ``PermissionDenied`` (rendered by
    ``403.html`` inside the console shell). For a JSON endpoint, use an in-body
    check instead — see the module docstring.
    """

    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if request.user.is_superuser:
            return view_func(request, *args, **kwargs)
        if request.user.is_authenticated:
            raise PermissionDenied(ADMIN_ONLY_MESSAGE)
        return redirect_to_login(request.get_full_path())

    return _wrapped
