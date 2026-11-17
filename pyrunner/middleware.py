"""Custom middleware for PyRun."""

import re
from urllib.parse import urlsplit

from django.conf import settings

# Where a request keeps the extra form-action origins of its own page.
_FORM_TARGETS_ATTR = "_csp_form_targets"
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")


def https_origin(url) -> str:
    """``https://host[:port]`` of an https URL, or "" for anything else.

    No path, query, user or password, and a host of plain DNS characters only,
    so the result is always a single, harmless CSP source expression.
    """
    try:
        parts = urlsplit(str(url))
        port = parts.port
    except ValueError:
        return ""
    host = parts.hostname or ""
    if parts.scheme != "https" or parts.username or parts.password or not _HOST.fullmatch(host):
        return ""
    return f"https://{host}" + (f":{port}" if port and port != 443 else "")


def allow_form_target(request, url) -> bool:
    """Let the page this request renders submit a form that ends at ``url``'s origin.

    Browsers check ``form-action`` against every redirect of a form submission,
    so a form whose POST answers with a redirect to another site (OAuth Connect
    to the provider's sign-in page) needs that site in its page's policy. Only
    the https origin is added, and only to this one response: every other page
    keeps ``form-action 'self'``. Returns whether the URL was accepted.
    """
    origin = https_origin(url)
    if not origin:
        return False
    targets = getattr(request, _FORM_TARGETS_ATTR, None)
    if targets is None:
        targets = set()
        setattr(request, _FORM_TARGETS_ATTR, targets)
    targets.add(origin)
    return True


def with_form_targets(policy, origins) -> str:
    """``policy`` with ``origins`` added to its ``form-action`` directive.

    A policy without form-action (no limit) or with ``'none'`` stays as it is.
    """
    directives = [d.strip() for d in policy.split(";") if d.strip()]
    for i, directive in enumerate(directives):
        name, *sources = directive.split()
        if name.lower() == "form-action" and "'none'" not in sources:
            extra = [o for o in sorted(origins) if o not in sources]
            directives[i] = " ".join([name, *sources, *extra])
    return "; ".join(directives)


class ContentSecurityPolicyMiddleware:
    """Attach a scoped Content-Security-Policy to HTML responses.

    The policy (``settings.CONTENT_SECURITY_POLICY``) is deliberately narrow: it
    hardens clickjacking (``frame-ancestors``), ``<base>``/``<object>`` injection,
    and cross-origin form submission, but does NOT restrict ``script-src`` /
    ``style-src``. The templates rely on inline ``<script>`` blocks and inline
    event handlers, so dropping ``'unsafe-inline'`` from ``script-src`` needs a
    nonce-based template refactor first (see the note in settings.py). Scoped to
    ``text/html`` since CSP only governs document contexts. A page whose form
    must end on another site (OAuth Connect) adds that origin to its own
    ``form-action`` with ``allow_form_target``.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.policy = getattr(settings, "CONTENT_SECURITY_POLICY", "")

    def __call__(self, request):
        response = self.get_response(request)

        if self.policy and "text/html" in response.get("Content-Type", ""):
            policy = self.policy
            targets = getattr(request, _FORM_TARGETS_ATTR, None)
            if targets:  # a page with a form that leaves the site (allow_form_target)
                policy = with_form_targets(policy, targets)
            # setdefault: never clobber a stricter policy set by a specific view.
            response.setdefault("Content-Security-Policy", policy)

        return response


class NoCacheMiddleware:
    """Prevent browser caching of HTML responses.

    This ensures users always see fresh data when navigating the dashboard,
    without needing to hard-refresh the browser.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # Only add no-cache headers to HTML responses
        content_type = response.get("Content-Type", "")
        if "text/html" in content_type:
            response["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response["Pragma"] = "no-cache"
            response["Expires"] = "0"

        return response
