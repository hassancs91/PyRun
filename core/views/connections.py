"""
Connections in the console (2.5 Stage 5, T23).

A workspace's members add a connection by picking a provider preset and filling
in its fields, then Test it, grant it to scripts, revoke or delete it. Like
Secrets, every page is scoped to the active workspace and open to its members;
secret fields are write-only (a blank field on edit keeps the saved value).
"""

import logging

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core import netguard
from core.forms import CHECK_CLASS, INPUT_CLASS
from core.integrations.presets import SMTP, get_preset, presets
from core.models import Connection, ConnectionGrant, Script
from core.services import EncryptionService
from core.services import connections as svc

logger = logging.getLogger(__name__)

def _form_field(field, editing):
    """The Django form field for one preset field."""
    attrs = {"class": INPUT_CLASS}
    if field.placeholder:
        attrs["placeholder"] = field.placeholder
    common = {"label": field.label, "help_text": field.help, "required": field.required}
    if field.secret:
        if editing:
            common["required"] = False
            common["help_text"] = " ".join(
                p for p in (field.help, "Leave blank to keep the saved value.") if p
            )
        widget = forms.PasswordInput(attrs={**attrs, "autocomplete": "new-password"})
        return forms.CharField(widget=widget, max_length=4096, **common)
    if field.kind == "int":
        return forms.IntegerField(
            min_value=1, max_value=65535, initial=field.default,
            widget=forms.NumberInput(attrs=attrs), **common,
        )
    if field.kind == "choice":
        return forms.ChoiceField(
            choices=field.choices, initial=field.default, widget=forms.Select(attrs=attrs), **common
        )
    if field.kind == "email":
        return forms.EmailField(widget=forms.EmailInput(attrs=attrs), **common)
    return forms.CharField(
        max_length=500, initial=field.default or None, strip=field.strip,
        widget=forms.TextInput(attrs=attrs), **common,
    )


class ConnectionForm(forms.Form):
    """Name, the preset's fields for one auth kind, and the wildcard grant."""

    name = forms.CharField(
        max_length=100,
        help_text=(
            f"How scripts name it: pyrunner_connections.client(\"name\") or smtp(\"name\"). {svc.NAME_HELP}"
        ),
        widget=forms.TextInput(attrs={"class": INPUT_CLASS, "autocomplete": "off"}),
    )
    grant_all_scripts = forms.BooleanField(
        required=False,
        label="Grant to all my scripts",
        help_text="Every script its creator makes in this workspace gets it, now and later (not plugins' scripts).",
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
    )

    def __init__(self, *args, preset, auth_kind, workspace, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.preset = preset
        self.auth_kind = auth_kind
        self.workspace = workspace
        self.instance = instance
        self.preset_fields = preset.fields_for(auth_kind)
        for field in self.preset_fields:
            self.fields[field.name] = _form_field(field, editing=instance is not None)
        if instance is not None and not self.is_bound:
            self.initial.update(instance.config or {})
            self.initial["name"] = instance.name
            self.initial["grant_all_scripts"] = instance.grant_all_scripts
        elif instance is None and not self.is_bound:
            self.initial.setdefault("name", preset.key)

    def preset_bound_fields(self):
        return [self[f.name] for f in self.preset_fields]

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not svc.NAME_RE.match(name):
            raise ValidationError(svc.NAME_HELP)
        clash = Connection.objects.filter(workspace=self.workspace, name=name)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise ValidationError("A connection with this name already exists in this workspace.")
        return name

    def clean(self):
        data = super().clean()
        for field in self.preset_fields:
            value = data.get(field.name)
            if field.kind != "url" or not value:
                continue
            if not value.lower().startswith(("https://", "http://")):
                self.add_error(field.name, "Start the URL with https:// (or http://).")
                continue
            ok, why = netguard.is_safe_endpoint_url(value)
            if not ok:
                self.add_error(field.name, why)
        host = data.get("host")
        if self.auth_kind == SMTP and host:
            if any(c in host for c in "/:@ "):
                self.add_error("host", "Enter a host name only, e.g. smtp.example.com.")
            else:
                ok, why = netguard.is_safe_endpoint_url(f"smtp://{host}")
                if not ok:
                    self.add_error("host", why)
        return data

    def save(self, user):
        connection = self.instance or Connection(
            workspace=self.workspace,
            provider=self.preset.key,
            auth_kind=self.auth_kind,
            created_by=user,
        )
        data = self.cleaned_data
        connection.name = data["name"]
        connection.grant_all_scripts = data.get("grant_all_scripts", False)
        connection.config = {
            f.name: (data.get(f.name) if data.get(f.name) is not None else "")
            for f in self.preset.config_fields(self.auth_kind)
        }
        try:
            saved = connection.get_credentials() if self.instance is not None else {}
        except Exception:
            # Unreadable (ENCRYPTION_KEY changed): only what is typed now is kept.
            logger.warning("Connection %s: saved credentials unreadable; replaced by the form's", connection.pk)
            saved = {}
        typed = {f.name: data.get(f.name) or "" for f in self.preset.secret_fields(self.auth_kind)}
        connection.set_credentials({k: typed[k] or saved.get(k, "") for k in typed})
        if connection.status == Connection.Status.REVOKED and (
            any(typed.values())
            or not any(f.required for f in self.preset.secret_fields(self.auth_kind))
        ):
            connection.status = Connection.Status.OK
            connection.last_error = ""
        connection.save()
        return connection


def _connection(request, pk):
    return get_object_or_404(Connection, pk=pk, workspace=request.workspace)


def _back(request, connection):
    if request.POST.get("next") == "edit":
        return redirect("cpanel:connection_edit", pk=connection.pk)
    return redirect("cpanel:connection_list")


@login_required
def connection_list_view(request: HttpRequest) -> HttpResponse:
    rows = []
    for connection in Connection.objects.filter(workspace=request.workspace).select_related("created_by"):
        preset = get_preset(connection.provider)
        using = list(svc.scripts_using(connection).values_list("name", flat=True))
        rows.append({
            "connection": connection,
            "provider_label": preset.label if preset else connection.provider,
            "used_by": using[:6],
            "used_by_count": len(using),
        })
    return render(request, "cpanel/connections/list.html", {
        "rows": rows,
        "encryption_configured": EncryptionService.is_configured(),
    })


@login_required
def connection_create_view(request: HttpRequest) -> HttpResponse:
    if not EncryptionService.is_configured():
        messages.error(request, "Set ENCRYPTION_KEY before adding connections: their credentials are encrypted.")
        return redirect("cpanel:connection_list")
    params = request.POST if request.method == "POST" else request.GET
    preset = get_preset(params.get("provider", ""))
    if preset is None:
        return render(request, "cpanel/connections/picker.html", {"presets": presets().values()})
    auth_kind = params.get("auth_kind") or preset.auth_kinds[0]
    if auth_kind not in preset.auth_kinds:
        auth_kind = preset.auth_kinds[0]
    if request.method == "POST":
        form = ConnectionForm(request.POST, preset=preset, auth_kind=auth_kind, workspace=request.workspace)
        if form.is_valid():
            connection = form.save(request.user)
            messages.success(request, f'Connection "{connection.name}" saved. Test it to check the credentials.')
            return redirect("cpanel:connection_edit", pk=connection.pk)
    else:
        form = ConnectionForm(preset=preset, auth_kind=auth_kind, workspace=request.workspace)
    return render(request, "cpanel/connections/form.html", {
        "form": form,
        "preset": preset,
        "auth_kind": auth_kind,
        "auth_kinds": [(k, Connection.AuthKind(k).label) for k in preset.auth_kinds],
    })


@login_required
def connection_edit_view(request: HttpRequest, pk) -> HttpResponse:
    connection = _connection(request, pk)
    preset = get_preset(connection.provider)
    if preset is None or connection.auth_kind not in preset.auth_kinds:
        messages.error(request, f'Connection "{connection.name}" uses a provider this version doesn\'t know.')
        return redirect("cpanel:connection_list")
    scripts = list(
        Script.objects.filter(workspace=request.workspace, archived_at__isnull=True).order_by("name")
    )
    if request.method == "POST":
        form = ConnectionForm(
            request.POST, preset=preset, auth_kind=connection.auth_kind,
            workspace=request.workspace, instance=connection,
        )
        if form.is_valid():
            connection = form.save(request.user)
            svc.set_scripts(connection, request.POST.getlist("granted_script_ids"), scripts)
            messages.success(request, f'Connection "{connection.name}" saved.')
            return redirect("cpanel:connection_edit", pk=connection.pk)
    else:
        form = ConnectionForm(
            preset=preset, auth_kind=connection.auth_kind,
            workspace=request.workspace, instance=connection,
        )
    granted = {
        str(pk) for pk in ConnectionGrant.objects.filter(connection=connection, active=True)
        .values_list("script_id", flat=True)
    }
    wildcard = connection.grant_all_scripts and connection.created_by_id is not None
    return render(request, "cpanel/connections/form.html", {
        "form": form,
        "preset": preset,
        "connection": connection,
        "auth_kind": connection.auth_kind,
        "auth_label": Connection.AuthKind(connection.auth_kind).label,
        "scripts": [
            {
                "script": s,
                "granted": str(s.pk) in granted,
                "by_wildcard": wildcard and s.owner_plugin is None and s.created_by_id == connection.created_by_id,
            }
            for s in scripts
        ],
    })


@login_required
@require_POST
def connection_test_view(request: HttpRequest, pk) -> HttpResponse:
    connection = _connection(request, pk)
    ok, message = svc.check_connection(connection)
    (messages.success if ok else messages.error)(request, f'"{connection.name}": {message}')
    return _back(request, connection)


@login_required
@require_POST
def connection_revoke_view(request: HttpRequest, pk) -> HttpResponse:
    connection = _connection(request, pk)
    svc.revoke(connection)
    messages.success(
        request,
        f'Connection "{connection.name}" revoked: its credentials are deleted and scripts can\'t use it '
        "until you enter them again.",
    )
    return _back(request, connection)


@login_required
@require_POST
def connection_delete_view(request: HttpRequest, pk) -> HttpResponse:
    connection = _connection(request, pk)
    name = connection.name
    connection.delete()
    messages.success(request, f'Connection "{name}" deleted.')
    return redirect("cpanel:connection_list")
