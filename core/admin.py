from django.contrib import admin
from django.shortcuts import redirect

from core.models import Plugin


@admin.register(Plugin)
class PluginAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "status", "source", "version", "updated_at")
    list_filter = ("status", "source")
    search_fields = ("name", "slug")
    readonly_fields = ("installed_at", "activated_at", "updated_at")


# F69 (2.0): the Django admin is a superuser escape hatch for the Plugin row
# only. django.contrib.auth (Groups), django_q (scheduled/queued/finished tasks)
# and axes (login attempts) register their own models; editing those here
# bypasses PyRun's services (a hand-edited schedule desyncs ScriptSchedule,
# Groups do nothing — RBAC is per workspace). Lockouts are cleared with
# `manage.py user unlock`. core is listed after those apps in INSTALLED_APPS,
# so their admin modules are already imported when this one runs.
for _model in list(admin.site._registry):
    if _model is not Plugin:
        admin.site.unregister(_model)


def _console_sign_in(request, extra_context=None):
    """The admin's own login form would start a session on the password alone,
    skipping two-factor sign-in (2.5 T46): send it to the console's sign-in."""
    return redirect("auth:login")


admin.site.login = _console_sign_in
