"""
Secret management views for the control panel.
"""

import json
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.forms import SecretCreateForm, SecretEditForm
from core.models import Secret, SecretProvider
from core.services import EncryptionService
from core.services.secret_backends import SecretResolutionError, resolve_secret_ref
from core.views.decorators import ENCRYPTION_MISSING_MESSAGE, operator_message
from core.views.ownership import owned_block_message, owned_delete_blocked

logger = logging.getLogger(__name__)


def _mask_preview(value: str) -> str:
    """The same masked preview shape as ``Secret.get_masked_value`` (first 3 / last 3)."""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}...{value[-3:]}"


def _provider_hints() -> dict:
    """Map provider id → {placeholder, help, type} so the secret form can show the
    right reference hint per selected provider (rendered client-side)."""
    from core.services.secret_backends import get_backend

    hints = {}
    for p in SecretProvider.objects.all():
        try:
            backend = get_backend(p.provider_type)
        except SecretResolutionError:
            continue
        hints[str(p.id)] = {
            "placeholder": backend.ref_placeholder,
            "help": backend.ref_help,
            "type": p.provider_type,
        }
    return hints


@login_required
def secret_picker_view(request: HttpRequest) -> HttpResponse:
    """Autocomplete for the script secret-attach UI: workspace secrets matching ``q``,
    each tagged with its owner (System for unowned) so results group cleanly."""
    q = (request.GET.get("q") or "").strip()
    qs = Secret.objects.for_workspace(request.workspace)
    if q:
        qs = qs.filter(key__icontains=q)
    qs = qs.order_by("owner_plugin", "key")[:50]
    secrets = [
        {
            "id": str(s.id),
            "key": s.key,
            "owner_plugin": s.owner_plugin or "",
            "description": s.description,
        }
        for s in qs
    ]
    return JsonResponse({"secrets": secrets})


@login_required
def secret_list_view(request: HttpRequest) -> HttpResponse:
    """List all secrets with masked values."""
    secrets = (
        Secret.objects.for_workspace(request.workspace)
        .select_related("provider")
        .order_by("key")
    )

    # Owner filter (Plugin Platform v2): owners computed before filtering so the
    # dropdown always lists every owner present in the workspace.
    owners = list(
        Secret.objects.for_workspace(request.workspace)
        .exclude(owner_plugin__isnull=True)
        .exclude(owner_plugin="")
        .order_by("owner_plugin")
        .values_list("owner_plugin", flat=True)
        .distinct()
    )
    owner_filter = request.GET.get("owner_plugin")
    if owner_filter:
        secrets = secrets.filter(owner_plugin=owner_filter)

    # Check if encryption is configured
    encryption_configured = EncryptionService.is_configured()

    return render(
        request,
        "cpanel/secrets/list.html",
        {
            "secrets": secrets,
            "encryption_configured": encryption_configured,
            "owners": owners,
            "selected_owner": owner_filter or "",
        },
    )


@login_required
def secret_create_view(request: HttpRequest) -> HttpResponse:
    """Create a new secret."""
    # Check encryption configuration first
    if not EncryptionService.is_configured():
        messages.error(
            request,
            operator_message(
                request.user,
                "Encryption is not configured. Set ENCRYPTION_KEY in your environment.",
                ENCRYPTION_MISSING_MESSAGE,
            ),
        )
        return redirect("cpanel:secret_list")

    if request.method == "POST":
        form = SecretCreateForm(request.POST, workspace=request.workspace)
        if form.is_valid():
            key = form.cleaned_data["key"]
            source = form.cleaned_data["source"]
            description = form.cleaned_data.get("description", "")

            # Create the secret stamped with the active workspace (tenancy Stage 3)
            # so its key is unique per workspace. Local rows store an encrypted
            # value; external rows store a provider reference resolved at run time.
            secret = Secret(
                key=key,
                description=description,
                created_by=request.user,
                workspace=request.workspace,
                source=source,
            )
            if source == Secret.Source.EXTERNAL:
                secret.provider = form.cleaned_data["provider"]
                secret.external_ref = form.cleaned_data["external_ref"]
            else:
                secret.set_value(form.cleaned_data["value"])
            secret.save()

            messages.success(request, f'Secret "{key}" created successfully.')
            return redirect("cpanel:secret_list")
    else:
        form = SecretCreateForm(workspace=request.workspace)

    return render(
        request,
        "cpanel/secrets/create.html",
        {
            "form": form,
            "has_secret_providers": SecretProvider.objects.exists(),
            "provider_hints_json": _provider_hints(),
        },
    )


@login_required
def secret_edit_view(request: HttpRequest, pk) -> HttpResponse:
    """Edit an existing secret."""
    secret = get_object_or_404(Secret, pk=pk, workspace=request.workspace)

    if request.method == "POST":
        form = SecretEditForm(request.POST, instance=secret)
        if form.is_valid():
            source = form.cleaned_data["source"]
            secret.source = source
            if source == Secret.Source.EXTERNAL:
                # External rows hold no local value — clear any stored one on switch.
                secret.provider = form.cleaned_data["provider"]
                secret.external_ref = form.cleaned_data["external_ref"]
                secret.encrypted_value = ""
            else:
                secret.provider = None
                secret.external_ref = ""
                new_value = form.cleaned_data.get("value")
                if new_value:
                    secret.set_value(new_value)

            # Always update description
            secret.description = form.cleaned_data.get("description", "")
            secret.save()

            messages.success(request, f'Secret "{secret.key}" updated successfully.')
            return redirect("cpanel:secret_list")
    else:
        form = SecretEditForm(instance=secret)

    return render(
        request,
        "cpanel/secrets/edit.html",
        {
            "form": form,
            "secret": secret,
            "has_secret_providers": SecretProvider.objects.exists(),
            "provider_hints_json": _provider_hints(),
        },
    )


@login_required
@require_POST
def secret_delete_view(request: HttpRequest, pk) -> HttpResponse:
    """Delete a secret."""
    secret = get_object_or_404(Secret, pk=pk, workspace=request.workspace)

    # Plugin Platform v2: refuse to delete a plugin-owned secret out from under
    # its plugin (superuser force=1 is the escape hatch; cascade drops grants).
    if owned_delete_blocked(request, secret):
        messages.error(request, owned_block_message(secret, "secret"))
        return redirect("cpanel:secret_list")

    key = secret.key
    secret.delete()

    messages.success(request, f'Secret "{key}" deleted successfully.')
    return redirect("cpanel:secret_list")


@login_required
@require_POST
def secret_test_resolve_view(request: HttpRequest) -> JsonResponse:
    """Resolve a (provider, external_ref) live and return the MASKED value.

    Lets the secret form prove an external reference resolves before saving,
    without exposing the plaintext (same first-3/last-3 masking as the list). Same
    access level as secret creation — provider profiles are instance-global by
    design, so any operator who can create a secret can point one at any profile.
    """
    data = {}
    if request.body:
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse(
                {"success": False, "error": "Invalid JSON in request body"}, status=400
            )

    provider = None
    provider_id = data.get("provider_id")
    if provider_id:
        provider = SecretProvider.objects.filter(pk=provider_id).first()
    if provider is None:
        return JsonResponse({"success": False, "error": "Select a provider first."})

    ref = (data.get("external_ref") or "").strip()
    if not ref:
        return JsonResponse({"success": False, "error": "Enter a reference to resolve."})

    try:
        value = resolve_secret_ref(provider, ref)
    except SecretResolutionError as e:
        return JsonResponse({"success": False, "error": str(e)})
    except Exception as e:  # pragma: no cover - defensive
        logger.exception("Secret test-resolve failed")
        return JsonResponse({"success": False, "error": str(e)})

    return JsonResponse(
        {"success": True, "message": f"Resolved OK — value {_mask_preview(value)}"}
    )
