"""
User management views for admin users.
"""
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_http_methods, require_POST
from django.urls import reverse
from django.http import HttpRequest, HttpResponse

from core.models import PasswordResetToken, SecurityEvent, User, UserInvite, WorkspaceMembership
from core.services import security_events
from core.views.decorators import superuser_required


def sole_owned_workspaces(user) -> list:
    """Workspaces where ``user`` is the only owner (deleting them would orphan it)."""
    owned = WorkspaceMembership.objects.filter(
        user=user, role=WorkspaceMembership.ROLE_OWNER
    ).select_related("workspace")
    result = []
    for membership in owned:
        others = (
            WorkspaceMembership.objects.filter(
                workspace=membership.workspace, role=WorkspaceMembership.ROLE_OWNER
            )
            .exclude(user=user)
            .exists()
        )
        if not others:
            result.append(membership.workspace)
    return result


@login_required
@superuser_required
def user_list_view(request: HttpRequest) -> HttpResponse:
    """List all users and pending invites."""
    users = list(
        User.objects.all()
        .order_by("-date_joined")
        .prefetch_related("workspace_memberships__workspace__memberships")
    )
    for user in users:
        user.memberships_sorted = sorted(
            user.workspace_memberships.all(), key=lambda m: m.workspace.name.lower()
        )
        user.sole_owned = [
            m.workspace.name
            for m in user.memberships_sorted
            if m.role == WorkspaceMembership.ROLE_OWNER
            and not any(
                other.role == WorkspaceMembership.ROLE_OWNER and other.user_id != user.pk
                for other in m.workspace.memberships.all()
            )
        ]
    pending_invites = UserInvite.objects.filter(used_at__isnull=True).order_by("-created_at")

    # Check for recently created invite URL to display
    last_invite_url = request.session.pop("last_invite_url", None)
    last_invite_email = request.session.pop("last_invite_email", None)
    # A reset link generated a moment ago, shown once (like the invite link).
    last_reset_url = request.session.pop("last_reset_url", None)
    last_reset_email = request.session.pop("last_reset_email", None)

    return render(request, "cpanel/users.html", {
        "users": users,
        "pending_invites": pending_invites,
        "last_invite_url": last_invite_url,
        "last_invite_email": last_invite_email,
        "last_reset_url": last_reset_url,
        "last_reset_email": last_reset_email,
        "reset_link_hours": PasswordResetToken.EXPIRY_HOURS,
    })


@login_required
@superuser_required
@require_http_methods(["GET", "POST"])
def invite_user_view(request: HttpRequest) -> HttpResponse:
    """Create a new user invite."""
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()

        if not email or "@" not in email or "." not in email:
            messages.error(request, "Please enter a valid email address.")
            return redirect("cpanel:user_list")

        # Check if user already exists
        if User.objects.filter(email=email).exists():
            messages.error(request, f"User {email} already exists.")
            return redirect("cpanel:user_list")

        # Check if there's already a pending invite
        existing_invite = UserInvite.objects.filter(
            email=email, used_at__isnull=True
        ).first()
        if existing_invite and existing_invite.is_valid():
            messages.warning(
                request,
                f"An invite for {email} already exists. Creating a new one will invalidate the old link."
            )

        invite = UserInvite.create_invite(email, created_by=request.user)

        # Generate invite URL
        invite_url = request.build_absolute_uri(
            reverse("auth:accept_invite", kwargs={"token": invite.token})
        )

        # Store invite URL for display (since email may not be configured)
        request.session["last_invite_url"] = invite_url
        request.session["last_invite_email"] = email

        messages.success(request, f"Invitation created for {email}.")
        return redirect("cpanel:user_list")

    return redirect("cpanel:user_list")


@login_required
@superuser_required
@require_POST
def revoke_invite_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Revoke/delete an unused invite."""
    invite = get_object_or_404(UserInvite, pk=pk, used_at__isnull=True)
    email = invite.email
    invite.delete()
    messages.success(request, f"Invite for {email} revoked.")
    return redirect("cpanel:user_list")


@login_required
@superuser_required
@require_POST
def delete_user_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Delete a user (cannot delete self)."""
    user = get_object_or_404(User, pk=pk)

    if user.pk == request.user.pk:
        messages.error(request, "You cannot delete your own account.")
        return redirect("cpanel:user_list")

    # Like the member-page guards: a workspace must keep at least one owner.
    orphaned = sole_owned_workspaces(user)
    if orphaned:
        names = ", ".join(ws.name for ws in orphaned)
        messages.error(
            request,
            f"Can't delete {user.email}: they are the only owner of {names}. "
            "Make another member an owner (Workspaces → Members) or delete the "
            "workspace first.",
        )
        return redirect("cpanel:user_list")

    email = user.email
    user.delete()
    messages.success(request, f"User {email} deleted.")
    return redirect("cpanel:user_list")


@login_required
@superuser_required
@require_POST
def user_reset_link_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Generate a password reset link for a user, to hand over directly.

    The same single-use token the forgot-password email carries (valid
    ``PasswordResetToken.EXPIRY_HOURS``), so it works with email disabled.
    Generating one expires any earlier unused link for that user.
    """
    user = get_object_or_404(User, pk=pk)
    token = PasswordResetToken.create_for_user(user)
    request.session["last_reset_url"] = security_events.reset_url(request.build_absolute_uri("/"), token)
    request.session["last_reset_email"] = user.email
    security_events.record(
        "reset_link", request=request, user=user, detail=f"generated by {request.user.email}"
    )
    messages.success(request, f"Reset link generated for {user.email}.")
    return redirect("cpanel:user_list")


# The Security page lists at most this many of the newest matching events.
SECURITY_EVENT_LIMIT = 200


@login_required
@superuser_required
def user_security_view(request: HttpRequest) -> HttpResponse:
    """Users → Security: sign-ins, lockouts, password and token events."""
    kinds = SecurityEvent.Kind
    kind = request.GET.get("kind", "")
    email = request.GET.get("email", "").strip().lower()
    events = SecurityEvent.objects.select_related("user")
    if kind in kinds.values:
        events = events.filter(kind=kind)
    else:
        kind = ""
    if email:
        events = events.filter(email__icontains=email)
    return render(request, "cpanel/users_security.html", {
        "events": list(events.order_by("-created_at", "-id")[:SECURITY_EVENT_LIMIT]),
        "kinds": kinds.choices,
        "kind": kind,
        "email": email,
        "limit": SECURITY_EVENT_LIMIT,
        "retention_days": security_events.RETENTION_DAYS,
        "failed_kinds": (kinds.LOGIN_FAIL, kinds.LOCKOUT),
    })
