"""
Channel management views (Channels subsystem — Phase 1: outbound chat).

DB-backed CRUD (like Secrets), workspace-scoped, plus two AJAX actions on a saved
channel: test connection and the Telegram getUpdates chat-ID helper.
"""

import json
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.forms import ChannelForm, ChannelInboundForm
from core.models import Channel, ChannelMember
from core.services import ChannelService, ChannelServiceError, EncryptionService
from core.services.channels import ChannelError, ledger
from core.views.decorators import ENCRYPTION_MISSING_MESSAGE, operator_message

logger = logging.getLogger(__name__)

# Rows of the channel page's message log (newest first; 30 days are kept).
MESSAGE_LOG_ROWS = 50


@login_required
def channel_list_view(request: HttpRequest) -> HttpResponse:
    """List configured channels in the active workspace."""
    channels = Channel.objects.for_workspace(request.workspace).order_by("name")
    return render(
        request,
        "cpanel/channels/list.html",
        {
            "channels": channels,
            "encryption_configured": EncryptionService.is_configured(),
        },
    )


@login_required
def channel_create_view(request: HttpRequest) -> HttpResponse:
    """Create a new channel."""
    if not EncryptionService.is_configured():
        messages.error(
            request,
            operator_message(
                request.user,
                "Encryption is not configured. Set ENCRYPTION_KEY in your environment.",
                ENCRYPTION_MISSING_MESSAGE,
            ),
        )
        return redirect("cpanel:channel_list")

    if request.method == "POST":
        form = ChannelForm(request.POST, workspace=request.workspace)
        if form.is_valid():
            channel = form.save(created_by=request.user)
            messages.success(
                request,
                f'Channel "{channel.name}" created. Test it and set a default chat below.',
            )
            return redirect("cpanel:channel_edit", pk=channel.pk)
    else:
        # ?provider= picks whose credential fields the form asks for (the
        # provider select reloads the page with it, keeping a typed ?name=).
        form = ChannelForm(
            workspace=request.workspace,
            provider=request.GET.get("provider"),
            initial={"name": request.GET.get("name", "")[:120]},
        )

    return render(
        request,
        "cpanel/channels/create.html",
        {
            "form": form,
            "setup_template": getattr(form.provider_obj, "setup_template", ""),
            "provider_count": len(form.fields["provider"].choices),
        },
    )


@login_required
def channel_edit_view(request: HttpRequest, pk) -> HttpResponse:
    """Edit an existing channel."""
    channel = get_object_or_404(Channel, pk=pk, workspace=request.workspace)

    if request.method == "POST":
        form = ChannelForm(request.POST, instance=channel, workspace=request.workspace)
        if form.is_valid():
            form.save()
            messages.success(request, f'Channel "{channel.name}" updated.')
            return redirect("cpanel:channel_edit", pk=channel.pk)
    else:
        form = ChannelForm(instance=channel, workspace=request.workspace)

    provider = ChannelService.provider_for(channel)
    webhook_url = None
    if channel.inbound_token:
        webhook_url = request.build_absolute_uri(
            reverse("channel_webhook", args=[channel.inbound_token])
        )

    return render(
        request,
        "cpanel/channels/edit.html",
        {
            "form": form,
            "channel": channel,
            "inbound_form": ChannelInboundForm(channel=channel, workspace=request.workspace),
            "supports_inbound": getattr(provider, "supports_inbound", False),
            "supports_discovery": callable(getattr(provider, "discover_chat_ids", None)),
            "registers_webhook": getattr(provider, "registers_webhook", True),
            "supports_media_download": getattr(provider, "supports_media_download", False),
            "webhook_url": webhook_url,
            "members": channel.members.all(),
            "message_log": ledger.display_rows(
                channel.messages.select_related("run", "run__script").order_by("-created_at", "-id")[:MESSAGE_LOG_ROWS]
            ),
            "message_log_rows": MESSAGE_LOG_ROWS,
        },
    )


@login_required
@require_POST
def channel_inbound_view(request: HttpRequest, pk) -> HttpResponse:
    """Save inbound config and register/unregister the provider webhook."""
    channel = get_object_or_404(Channel, pk=pk, workspace=request.workspace)
    form = ChannelInboundForm(request.POST, channel=channel, workspace=request.workspace)
    if not form.is_valid():
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, f"{field}: {error}")
        return redirect("cpanel:channel_edit", pk=channel.pk)

    cd = form.cleaned_data
    channel.inbound_enabled = cd["inbound_enabled"]
    channel.inbound_handler = cd.get("inbound_handler") or ""
    channel.inbound_target_id = cd.get("inbound_target_id") or None
    channel.inbound_access = cd["inbound_access"]
    channel.daily_reply_cap = cd.get("daily_reply_cap") or 0

    provider = ChannelService.provider_for(channel)
    if getattr(provider, "supports_media_download", False):
        channel.download_media = cd.get("download_media", False)
        channel.media_max_mb = cd.get("media_max_mb") or channel.media_max_mb

    if channel.inbound_enabled:
        channel.ensure_inbound_token()
        channel.ensure_inbound_secret()  # persisted on the save() below
    channel.save()

    if channel.inbound_enabled and getattr(provider, "supports_inbound", False):
        public_url = request.build_absolute_uri(
            reverse("channel_webhook", args=[channel.inbound_token])
        )
        try:
            # A provider whose webhook is set in its own dashboard (WhatsApp)
            # says where instead.
            note = provider.set_inbound_webhook(channel, public_url)
            messages.success(request, note or "Inbound enabled and webhook registered.")
        except Exception as e:
            logger.warning("set_inbound_webhook failed for channel %s: %s", channel.id, e)
            messages.error(
                request,
                f"Saved, but registering the webhook failed: {e} "
                "Inbound needs a publicly reachable HTTPS instance.",
            )
    elif not channel.inbound_enabled and getattr(provider, "supports_inbound", False):
        try:
            provider.clear_inbound_webhook(channel)
        except Exception as e:
            logger.warning("clear_inbound_webhook failed for channel %s: %s", channel.id, e)
        messages.success(request, "Inbound disabled.")
    else:
        messages.success(request, "Inbound settings saved.")

    return redirect("cpanel:channel_edit", pk=channel.pk)


@login_required
@require_POST
def channel_member_action_view(request: HttpRequest, pk, member_pk, action) -> HttpResponse:
    """Approve / block / delete a channel member (the approval inbox)."""
    channel = get_object_or_404(Channel, pk=pk, workspace=request.workspace)
    member = get_object_or_404(ChannelMember, pk=member_pk, channel=channel)

    if action == "approve":
        member.status = ChannelMember.Status.APPROVED
        member.approved_at = timezone.now()
        member.added_by = request.user
        member.save(update_fields=["status", "approved_at", "added_by"])
        messages.success(request, f"Approved {member.display_name or member.sender_id}.")
    elif action == "block":
        member.status = ChannelMember.Status.BLOCKED
        member.save(update_fields=["status"])
        messages.success(request, f"Blocked {member.display_name or member.sender_id}.")
    elif action == "delete":
        member.delete()
        messages.success(request, "Member removed.")
    else:
        messages.error(request, "Unknown action.")

    return redirect("cpanel:channel_edit", pk=channel.pk)


@login_required
@require_POST
def channel_delete_view(request: HttpRequest, pk) -> HttpResponse:
    """Delete a channel."""
    channel = get_object_or_404(Channel, pk=pk, workspace=request.workspace)
    name = channel.name
    channel.delete()
    messages.success(request, f'Channel "{name}" deleted.')
    return redirect("cpanel:channel_list")


@login_required
@require_POST
def channel_test_view(request: HttpRequest, pk) -> JsonResponse:
    """Test connectivity for a saved channel (AJAX)."""
    channel = get_object_or_404(Channel, pk=pk, workspace=request.workspace)
    try:
        ok, message = ChannelService.test(channel)
    except Exception as e:
        logger.exception("Channel test failed")
        return JsonResponse({"success": False, "error": str(e)})
    return JsonResponse(
        {
            "success": ok,
            "message": message if ok else None,
            "error": message if not ok else None,
        }
    )


@login_required
@require_POST
def channel_discover_chat_ids_view(request: HttpRequest, pk) -> JsonResponse:
    """Telegram getUpdates helper: recent chats that messaged the bot (AJAX)."""
    channel = get_object_or_404(Channel, pk=pk, workspace=request.workspace)
    try:
        chats = ChannelService.discover_chat_ids(channel)
    except (ChannelError, ChannelServiceError) as e:
        return JsonResponse({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception("Channel chat-ID discovery failed")
        return JsonResponse({"success": False, "error": str(e)})
    return JsonResponse({"success": True, "chats": chats})
