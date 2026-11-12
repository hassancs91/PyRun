"""
Connections in the console (2.5 Stage 5, T23 and T24).

A workspace's members add a connection by picking a provider preset and filling
in its fields, then Test it, grant it to scripts, revoke or delete it. Like
Secrets, every page is scoped to the active workspace and open to its members;
secret fields are write-only (a blank field on edit keeps the saved value).

An OAuth provider's connection is made with Connect (the provider's consent
screen, back through the one ``/oauth/callback/``) or by pasting a token. Its
app (client id and secret) is set under OAuth apps: the workspace's own, or the
instance default a superuser set.
"""

import logging
import re

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from core import netguard
from core.forms import CHECK_CLASS, INPUT_CLASS
from core.integrations.presets import OAUTH2, PASTE_FIELDS, SMTP, get_preset, presets
from core.models import Connection, ConnectionGrant, OAuthClient, Script, Workspace
from core.services import EncryptionService
from core.services import connections as svc
from core.services import oauth
from core.views.decorators import ENCRYPTION_MISSING_MESSAGE, operator_message

logger = logging.getLogger(__name__)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


REVOKED_HELP = "Nothing is saved: the connection was revoked. Enter it to reconnect."


def _form_field(field, editing, revoked=False):
    """The Django form field for one preset field (``revoked``: nothing saved to keep)."""
    attrs = {"class": INPUT_CLASS}
    if field.placeholder:
        attrs["placeholder"] = field.placeholder
    common = {"label": field.label, "help_text": field.help, "required": field.required}
    if field.secret:
        if editing:
            common["required"] = False
            keep = REVOKED_HELP if revoked else "Leave blank to keep the saved value."
            common["help_text"] = " ".join(p for p in (field.help, keep) if p)
        widget = forms.PasswordInput(attrs={**attrs, "autocomplete": "new-password"})
        return forms.CharField(widget=widget, max_length=4096, **common)
    if field.kind == "int":
        return forms.IntegerField(
            min_value=1, max_value=65535, initial=field.default,
            widget=forms.NumberInput(attrs=attrs), **common,
        )
    if field.kind == "multichoice":
        return forms.MultipleChoiceField(
            choices=field.choices, initial=list(field.default or ()),
            widget=forms.CheckboxSelectMultiple(attrs={"class": CHECK_CLASS}), **common,
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


def _name_usage(preset, auth_kind) -> str:
    """How a script (or a watcher) uses a connection of this kind, for the Name help."""
    if preset.protocol == "imap":
        return ('How IMAP watchers and scripts name it: pick it in an IMAP watcher, or '
                'pyrunner_connections.credentials("name")["imap"] in a script.')
    if auth_kind == SMTP:
        return 'How scripts name it: pyrunner_connections.smtp("name").'
    return 'How scripts name it: pyrunner_connections.client("name").'


class ConnectionForm(forms.Form):
    """Name, the preset's fields for one auth kind, and the wildcard grant."""

    name = forms.CharField(
        max_length=100,
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
        self.fields["name"].help_text = f"{_name_usage(preset, auth_kind)} {svc.NAME_HELP}"
        revoked = instance is not None and instance.status == Connection.Status.REVOKED
        for field in self.preset_fields:
            self.fields[field.name] = _form_field(field, editing=instance is not None, revoked=revoked)
        if instance is not None and not self.is_bound:
            self.initial.update(instance.config or {})
            self.initial["name"] = instance.name
            self.initial["grant_all_scripts"] = instance.grant_all_scripts
        elif instance is None and not self.is_bound:
            self.initial.setdefault("name", preset.key)

    def preset_bound_fields(self):
        return [self[f.name] for f in self.preset_fields]

    def main_fields(self):
        """The fields the form shows in its main column (the wildcard has its own place)."""
        return [f for f in self if f.name != "grant_all_scripts"]

    def config_values(self) -> dict:
        data = self.cleaned_data
        return {
            f.name: (data.get(f.name) if data.get(f.name) is not None else "")
            for f in self.preset.config_fields(self.auth_kind)
        }

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
            if isinstance(value, str) and _CONTROL_CHARS.search(value):
                # No key, token, password or header holds one; a pasted line break
                # would only break the request (and come back escaped in its error).
                self.add_error(field.name, "Remove the line breaks and other control characters.")
                continue
            if field.kind != "url" or not value:
                continue
            if not value.lower().startswith(("https://", "http://")):
                self.add_error(field.name, "Start the URL with https:// (or http://).")
                continue
            ok, why = netguard.is_safe_endpoint_url(value)
            if not ok:
                self.add_error(field.name, why)
        host = data.get("host")
        mail = "imap" if self.preset.protocol == "imap" else "smtp" if self.auth_kind == SMTP else None
        if mail and host:
            if any(c in host for c in "/:@ "):
                self.add_error("host", f"Enter a host name only, e.g. {mail}.example.com.")
            else:
                ok, why = netguard.is_safe_endpoint_url(f"{mail}://{host}")
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
        connection.config = self.config_values()
        self.apply_credentials(connection)
        connection.save()
        return connection

    def apply_credentials(self, connection):
        """The typed secret fields onto ``connection`` (a blank one keeps the saved value)."""
        data = self.cleaned_data
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


class OAuthConnectionForm(ConnectionForm):
    """An OAuth connection: what to ask for, then Connect or a pasted token.

    ``action`` is "connect" (validate, then the view starts the redirect flow)
    or anything else (save; a pasted token is checked with the provider first).
    """

    def __init__(self, *args, action="", client=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.action = action
        self.client = client
        self.tokens = None
        self.label = ""

    def main_fields(self):
        return [f for f in super().main_fields() if f.name not in PASTE_FIELDS]

    def paste_fields(self):
        return [self[name] for name in PASTE_FIELDS]

    def scopes(self) -> list:
        data = self.cleaned_data
        return self.preset.oauth.scopes_for(data.get("scope_sets") or (), data.get("extra_scopes", ""))

    def clean(self):
        data = super().clean()
        if self.errors:
            return data
        label = self.preset.label
        if self.action == "connect":
            if self.client is None:
                self.add_error(None, f"Add a {label} OAuth app first (OAuth apps, on the Connections page).")
            return data
        refresh_token = (data.get("refresh_token") or "").strip()
        access_token = (data.get("access_token") or "").strip()
        if not refresh_token and not access_token:
            if self.instance is None:
                self.add_error(None, f"Use Connect with {label}, or paste a refresh token or an access token.")
            return data
        try:
            self.tokens, self.label = oauth.validate_pasted(
                self.preset, self.client, refresh_token=refresh_token,
                access_token="" if refresh_token else access_token, config=self.config_values(),
            )
        except oauth.OAuthError as e:
            from core.executor import mask_secrets_in_value

            pasted = {"refresh_token": refresh_token, "access_token": access_token}
            message = mask_secrets_in_value(e.message, pasted)
            self.add_error(None, f"{label} didn't accept this token: {message}" if e.refused else message)
        return data

    def apply_credentials(self, connection):
        if self.tokens is None:
            return  # nothing pasted: the saved tokens stay
        oauth.apply_tokens(connection, self.tokens, keep_refresh_token=False)
        connection.oauth_client = self.client if self.tokens["refresh_token"] else None
        connection.account_label = (self.label or "")[:255]
        connection.status = Connection.Status.OK
        connection.last_error = ""
        connection.last_tested_at = timezone.now()


def _form_for(request, **kwargs):
    """The connection form for its auth kind: the OAuth one takes Connect and pasted tokens."""
    if kwargs["auth_kind"] != OAUTH2:
        return ConnectionForm(**kwargs)
    action = request.POST.get("action", "") if request.method == "POST" else ""
    return OAuthConnectionForm(
        action=action, client=oauth.client_for(request.workspace, kwargs["preset"].key), **kwargs
    )


def _oauth_context(request, form):
    if not isinstance(form, OAuthConnectionForm):
        return {}
    return {"oauth_client": form.client, "callback_url": oauth.callback_url(request)}


def _start_connect(request, form, connection=None):
    url = oauth.start(
        request,
        preset=form.preset,
        client=form.client,
        name=form.cleaned_data["name"],
        config=form.config_values(),
        scopes=form.scopes(),
        connection=connection,
        grant_all_scripts=form.cleaned_data.get("grant_all_scripts", False),
    )
    return redirect(url)


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
        messages.error(request, operator_message(
            request.user,
            "Set ENCRYPTION_KEY before adding connections: their credentials are encrypted.",
            ENCRYPTION_MISSING_MESSAGE,
        ))
        return redirect("cpanel:connection_list")
    params = request.POST if request.method == "POST" else request.GET
    preset = get_preset(params.get("provider", ""))
    if preset is None:
        return render(request, "cpanel/connections/picker.html", {"presets": presets().values()})
    auth_kind = params.get("auth_kind") or preset.auth_kinds[0]
    if auth_kind not in preset.auth_kinds:
        auth_kind = preset.auth_kinds[0]
    common = {"preset": preset, "auth_kind": auth_kind, "workspace": request.workspace}
    if request.method == "POST":
        form = _form_for(request, data=request.POST, **common)
        if form.is_valid():
            if getattr(form, "action", "") == "connect":
                return _start_connect(request, form)
            connection = form.save(request.user)
            if auth_kind == OAUTH2:
                messages.success(request, f'Connection "{connection.name}" saved and checked with {preset.label}.')
            else:
                messages.success(request, f'Connection "{connection.name}" saved. Test it to check the credentials.')
            return redirect("cpanel:connection_edit", pk=connection.pk)
    else:
        form = _form_for(request, **common)
    return render(request, "cpanel/connections/form.html", {
        "form": form,
        "preset": preset,
        "auth_kind": auth_kind,
        "auth_kinds": [(k, Connection.AuthKind(k).label) for k in preset.auth_kinds],
        **_oauth_context(request, form),
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
    common = {
        "preset": preset, "auth_kind": connection.auth_kind,
        "workspace": request.workspace, "instance": connection,
    }
    if request.method == "POST":
        form = _form_for(request, data=request.POST, **common)
        if form.is_valid():
            connection = form.save(request.user)
            svc.set_scripts(connection, request.POST.getlist("granted_script_ids"), scripts)
            if getattr(form, "action", "") == "connect":
                return _start_connect(request, form, connection)
            messages.success(request, f'Connection "{connection.name}" saved.')
            return redirect("cpanel:connection_edit", pk=connection.pk)
    else:
        form = _form_for(request, **common)
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
        **_oauth_context(request, form),
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


# --------------------------------------------------------------------------- #
# OAuth: the callback and the apps
# --------------------------------------------------------------------------- #


def _edit_url(user, workspace, connection):
    """The connection's page, in its workspace (prefixed unless it's the user's default)."""
    default, _ok = Workspace.resolve_for(user)
    if default is not None and default.pk == workspace.pk:
        return reverse("cpanel:connection_edit", kwargs={"pk": connection.pk})
    return reverse(
        "cpanel_ws:connection_edit", kwargs={"workspace_id": workspace.pk, "pk": connection.pk}
    )


@login_required
def oauth_callback_view(request: HttpRequest) -> HttpResponse:
    """Where every provider sends the browser back after its consent screen."""
    error = request.GET.get("error", "")
    if error:
        # Shown only when it looks like an OAuth error code: the rest of the
        # query (error_description) is anyone's text, as the link can be forged.
        code = error if error.replace("_", "").isalnum() and len(error) <= 64 else "an error"
        messages.error(request, f"Not connected: the provider answered {code} (nothing was saved).")
        return redirect("cpanel:connection_list")
    try:
        connection, workspace, next_url = oauth.finish(
            request, state_value=request.GET.get("state", ""), code=request.GET.get("code", "")
        )
    except oauth.OAuthError as e:
        logger.warning("OAuth callback refused for user %s: %s", request.user.pk, e.message)
        messages.error(request, f"Not connected: {e.message}")
        return redirect("cpanel:connection_list")
    account = f" as {connection.account_label}" if connection.account_label else ""
    messages.success(request, f'Connection "{connection.name}" connected{account}.')
    if next_url and local_url(request, next_url):
        # A plugin page's Connect button (T25) comes back to that page.
        return redirect(next_url)
    return redirect(_edit_url(request.user, workspace, connection))


def local_url(request, url) -> bool:
    """True for a path on this site (where a ``next`` may send the browser)."""
    return (
        isinstance(url, str)
        and url.startswith("/")
        and not url.startswith("//")
        and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()})
    )


class OAuthClientForm(forms.Form):
    """An OAuth app: client id, client secret (write-only) and the preset's extra fields."""

    client_id = forms.CharField(
        max_length=500, label="Client ID",
        widget=forms.TextInput(attrs={"class": INPUT_CLASS, "autocomplete": "off"}),
    )
    client_secret = forms.CharField(
        max_length=4096, required=False, label="Client secret",
        widget=forms.PasswordInput(attrs={"class": INPUT_CLASS, "autocomplete": "new-password"}),
    )
    instance_default = forms.BooleanField(
        required=False,
        label="For every workspace (instance default)",
        help_text="Workspaces without their own app use this one. Only superusers set it.",
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
    )

    def __init__(self, *args, preset, workspace, user, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.preset = preset
        self.workspace = workspace
        self.instance = instance
        self.extra_fields = preset.oauth.client_fields
        for field in self.extra_fields:
            self.fields[field.name] = _form_field(field, editing=instance is not None)
        if instance is not None:
            self.fields["client_secret"].help_text = "Leave blank to keep the saved secret."
            del self.fields["instance_default"]
            if not self.is_bound:
                self.initial.update(instance.config or {})
                self.initial["client_id"] = instance.client_id
        else:
            self.fields["client_secret"].required = True
            if not user.is_superuser:
                del self.fields["instance_default"]

    def main_fields(self):
        return [f for f in self if f.name != "instance_default"]

    def clean(self):
        data = super().clean()
        for field in self.extra_fields:
            value = str(data.get(field.name) or "")
            # Filled into the provider's URLs, so a plain identifier only.
            if value and not svc.NAME_RE.match(value):
                self.add_error(field.name, "Letters, digits, '.', '_' and '-' only.")
        if self.instance is None:
            target = None if data.get("instance_default") else self.workspace
            if OAuthClient.objects.filter(workspace=target, provider=self.preset.key).exists():
                where = "The instance default" if target is None else "This workspace"
                self.add_error(None, f"{where} already has a {self.preset.label} app: edit it instead.")
        return data

    def save(self, user):
        client = self.instance or OAuthClient(
            workspace=None if self.cleaned_data.get("instance_default") else self.workspace,
            provider=self.preset.key,
            created_by=user,
        )
        client.client_id = self.cleaned_data["client_id"].strip()
        client.config = {f.name: self.cleaned_data.get(f.name) or "" for f in self.extra_fields}
        if self.cleaned_data.get("client_secret"):
            client.set_secret(self.cleaned_data["client_secret"].strip())
        client.save()
        return client


def _oauth_presets():
    return [p for p in presets().values() if p.oauth is not None]


def _oauth_client(request, pk):
    """An app this user may manage: the workspace's, or (superusers) an instance default."""
    client = get_object_or_404(
        OAuthClient, Q(workspace=request.workspace) | Q(workspace__isnull=True), pk=pk
    )
    if client.workspace_id is None and not request.user.is_superuser:
        raise Http404("OAuth app not found")
    return client


@login_required
def oauth_app_list_view(request: HttpRequest) -> HttpResponse:
    clients = OAuthClient.objects.filter(Q(workspace=request.workspace) | Q(workspace__isnull=True))
    by_provider = {}
    for client in clients:
        slot = "default" if client.workspace_id is None else "own"
        by_provider.setdefault(client.provider, {})[slot] = client
    rows = [
        {
            "preset": preset,
            "own": by_provider.get(preset.key, {}).get("own"),
            "default": by_provider.get(preset.key, {}).get("default"),
        }
        for preset in _oauth_presets()
    ]
    return render(request, "cpanel/connections/apps.html", {
        "rows": rows,
        "callback_url": oauth.callback_url(request),
        "encryption_configured": EncryptionService.is_configured(),
    })


@login_required
def oauth_app_form_view(request: HttpRequest, pk=None) -> HttpResponse:
    if not EncryptionService.is_configured():
        messages.error(request, operator_message(
            request.user,
            "Set ENCRYPTION_KEY before adding OAuth apps: their secrets are encrypted.",
            ENCRYPTION_MISSING_MESSAGE,
        ))
        return redirect("cpanel:oauth_app_list")
    client = _oauth_client(request, pk) if pk is not None else None
    params = request.POST if request.method == "POST" else request.GET
    preset = get_preset(client.provider if client else params.get("provider", ""))
    if preset is None or preset.oauth is None:
        messages.error(request, "Pick an OAuth provider.")
        return redirect("cpanel:oauth_app_list")
    kwargs = {"preset": preset, "workspace": request.workspace, "user": request.user, "instance": client}
    if request.method == "POST":
        form = OAuthClientForm(request.POST, **kwargs)
        if form.is_valid():
            form.save(request.user)
            messages.success(request, f"{preset.label} OAuth app saved.")
            return redirect("cpanel:oauth_app_list")
    else:
        form = OAuthClientForm(**kwargs)
    return render(request, "cpanel/connections/app_form.html", {
        "form": form,
        "preset": preset,
        "client": client,
        "callback_url": oauth.callback_url(request),
        "used_by": client.connections.count() if client else 0,
    })


@login_required
@require_POST
def oauth_app_delete_view(request: HttpRequest, pk) -> HttpResponse:
    client = _oauth_client(request, pk)
    preset = get_preset(client.provider)
    client.delete()
    messages.success(
        request,
        f"{preset.label if preset else client.provider} OAuth app deleted. Connections it issued "
        "tokens for stop refreshing: reconnect them with another app.",
    )
    return redirect("cpanel:oauth_app_list")
