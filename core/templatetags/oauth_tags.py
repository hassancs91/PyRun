"""``{% allow_oauth_connect preset %}``: a page with an OAuth Connect button may send it to the provider.

Put it next to every Connect / Reconnect form: the POST answers with a redirect
to the provider's sign-in page, which the console's CSP (``form-action 'self'``)
would block. The tag adds that one origin to this page's policy only; it
renders nothing (core.services.oauth.allow_connect).
"""

from django import template

from core.services import oauth

register = template.Library()


@register.simple_tag(takes_context=True)
def allow_oauth_connect(context, preset):
    request = context.get("request")
    if request is not None:
        oauth.allow_connect(request, preset)
    return ""
