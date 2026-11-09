"""
Workspace management views (tenancy Stage 0 plumbing → Stage 4 RBAC).

Stage 4 makes management role-based: an Owner/Admin of a workspace (or any
superuser) may rename it, manage its members, and (Owner only) delete it.
Plain members may use a workspace's resources but not manage it. Creating a NEW
workspace stays an instance-level act (superuser only) — provisioning a tenant
is not a per-workspace role action; the creator becomes its Owner, and a
superuser can hand ownership to a user via the members UI.

Gating contract (consistent with the rest of tenancy):
- superuser ⇒ may do anything, in any workspace (cross-workspace god);
- a NON-member targeting a workspace ⇒ 404 (no existence disclosure — the URL is
  never trusted), exactly like the active-workspace middleware;
- a member without the required role ⇒ 403 (they can see the workspace but lack
  the privilege).
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.models import User, Workspace, WorkspaceMembership
from core.views.decorators import superuser_required

MAX_NAME_LEN = 100

# Scoped models a workspace "owns" — used by the delete-block so SET_NULL never
# silently orphans tenant data (reverse accessor of the workspace FK, then the
# singular and plural label for the refusal message). Every SET_NULL workspace
# FK except Environment (shared infra, listed instance-wide) belongs here: an
# orphaned row is invisible under strict scoping, an orphaned Database keeps its
# schema and role on the data server, and an orphaned API token is refused
# (fail closed, never the default workspace).
_OWNED_RELATIONS = (
    ("scripts", "script", "scripts"),
    ("runs", "run", "runs"),
    ("schedules", "schedule", "schedules"),
    ("secrets", "secret", "secrets"),
    ("datastores", "data store", "data stores"),
    ("libraries", "library", "libraries"),
    ("databases", "database", "databases"),
    ("vector_collections", "vector collection", "vector collections"),
    ("brains", "brain", "brains"),
    ("channels", "channel", "channels"),
    ("api_tokens", "API token", "API tokens"),
)


def _membership(user, workspace):
    return WorkspaceMembership.objects.filter(user=user, workspace=workspace).first()


def _require_manage(request, workspace):
    """Owner/Admin of ``workspace`` (or superuser) may manage it.

    Returns the caller's membership (None for a superuser who isn't a member).
    Raises Http404 for a non-member (no disclosure), PermissionDenied for a
    member who lacks a manage role.
    """
    if request.user.is_superuser:
        return None
    membership = _membership(request.user, workspace)
    if membership is None:
        raise Http404("Workspace not found")
    if not membership.can_manage:
        raise PermissionDenied("You do not have permission to manage this workspace.")
    return membership


def _require_owner(request, workspace):
    """Only an Owner (or superuser) may delete a workspace."""
    if request.user.is_superuser:
        return None
    membership = _membership(request.user, workspace)
    if membership is None:
        raise Http404("Workspace not found")
    if membership.role != WorkspaceMembership.ROLE_OWNER:
        raise PermissionDenied("Only an owner can delete a workspace.")
    return membership


def _owned_row_counts(workspace) -> dict:
    """Count the scoped rows a workspace owns, keyed by the message label."""
    counts = {}
    for rel, singular, plural in _OWNED_RELATIONS:
        n = getattr(workspace, rel).count()
        if n:
            counts[singular if n == 1 else plural] = n
    return counts


@login_required
def workspace_list_view(request: HttpRequest) -> HttpResponse:
    """List the workspaces the caller belongs to (superuser: all), with roles.

    Opened up from Stage 0's superuser-only gate: any logged-in user sees their
    own workspaces. Management actions are shown per-row only where the caller
    may manage (Owner/Admin or superuser); creating is superuser-only.
    """
    # Filter by id rather than annotating for_user() directly: for a
    # non-superuser for_user() joins memberships on the caller, and Count()
    # would reuse that join and count only the caller's own row.
    workspaces = (
        Workspace.objects.filter(pk__in=Workspace.for_user(request.user).values("pk"))
        .annotate(member_count=Count("memberships", distinct=True))
        .order_by("-is_default", "name")
    )
    role_map = {
        m.workspace_id: m.role
        for m in WorkspaceMembership.objects.filter(user=request.user)
    }
    is_super = request.user.is_superuser
    for ws in workspaces:
        ws.user_role = role_map.get(ws.id)
        ws.user_can_manage = is_super or ws.user_role in WorkspaceMembership.MANAGE_ROLES
        ws.user_is_owner = is_super or ws.user_role == WorkspaceMembership.ROLE_OWNER
    return render(
        request,
        "cpanel/workspaces/list.html",
        {"workspaces": workspaces, "can_create": is_super},
    )


# Creating a workspace is instance-level tenant provisioning, so it is
# superuser-only (unlike the role-based rename/members/delete views below).
@login_required
@superuser_required
@require_POST
def workspace_create_view(request: HttpRequest) -> HttpResponse:
    """Create a workspace; the creator becomes its owner (superuser only)."""
    name = (request.POST.get("name") or "").strip()[:MAX_NAME_LEN]
    if not name:
        messages.error(request, "Workspace name is required.")
        return redirect("cpanel:workspace_list")

    workspace = Workspace.objects.create(name=name)
    WorkspaceMembership.ensure(
        request.user, workspace, role=WorkspaceMembership.ROLE_OWNER
    )
    messages.success(request, f"Workspace '{name}' created.")
    return redirect("cpanel:workspace_list")


@login_required
@require_POST
def workspace_rename_view(request: HttpRequest, pk) -> HttpResponse:
    """Rename a workspace (Owner/Admin or superuser)."""
    workspace = get_object_or_404(Workspace, pk=pk)
    _require_manage(request, workspace)

    name = (request.POST.get("name") or "").strip()[:MAX_NAME_LEN]
    if not name:
        messages.error(request, "Workspace name is required.")
        return redirect("cpanel:workspace_list")

    workspace.name = name
    workspace.save(update_fields=["name", "updated_at"])
    messages.success(request, "Workspace renamed.")
    return redirect("cpanel:workspace_list")


@login_required
@require_POST
def workspace_delete_view(request: HttpRequest, pk) -> HttpResponse:
    """Delete a workspace (Owner or superuser) — blocked if it still owns data.

    SET_NULL on the scoped FKs means deleting a non-empty workspace would orphan
    its rows to ``workspace IS NULL``, where the Stage 3 strict scoping makes them
    invisible (leak matrix row 16). So we BLOCK deletion until the workspace is
    empty; the default workspace can never be deleted.
    """
    workspace = get_object_or_404(Workspace, pk=pk)
    _require_owner(request, workspace)

    if workspace.is_default:
        messages.error(request, "The default workspace cannot be deleted.")
        return redirect("cpanel:workspace_list")

    owned = _owned_row_counts(workspace)
    if owned:
        summary = ", ".join(f"{n} {label}" for label, n in owned.items())
        messages.error(
            request,
            f"Cannot delete '{workspace.name}' — it still owns {summary}. "
            "Move or delete its resources first.",
        )
        return redirect("cpanel:workspace_list")

    name = workspace.name
    workspace.delete()  # memberships CASCADE; no scoped rows remain to orphan
    messages.success(request, f"Workspace '{name}' deleted.")
    return redirect("cpanel:workspace_list")


@login_required
@require_POST
def workspace_sandbox_policy_view(request: HttpRequest, pk) -> HttpResponse:
    """Set a workspace's execution-isolation policy (Owner/Admin or superuser).

    Blank ⇒ inherit the instance default. A workspace policy can only TIGHTEN the
    effective isolation toward 'required' (resolve_isolation enforces the floor),
    so an Owner/Admin can mandate isolation but can't weaken an instance default.
    """
    workspace = get_object_or_404(Workspace, pk=pk)
    _require_manage(request, workspace)

    policy = (request.POST.get("sandbox_policy") or "").strip()
    valid = {c[0] for c in Workspace.SandboxPolicy.choices}
    if policy == "":
        workspace.sandbox_policy = None  # inherit the instance default
    elif policy in valid:
        workspace.sandbox_policy = policy
    else:
        messages.error(request, "Invalid isolation policy.")
        return redirect("cpanel:workspace_list")

    workspace.save(update_fields=["sandbox_policy", "updated_at"])
    messages.success(request, f"Isolation policy for '{workspace.name}' updated.")
    return redirect("cpanel:workspace_list")


@login_required
def workspace_members_view(request: HttpRequest, pk) -> HttpResponse:
    """List/manage the members of a workspace (Owner/Admin or superuser)."""
    workspace = get_object_or_404(Workspace, pk=pk)
    caller = _require_manage(request, workspace)

    # Owner first, then admins, then members (U78): the stored values sort
    # alphabetically (admin, member, owner), which listed the Owner last.
    rank = {
        WorkspaceMembership.ROLE_OWNER: 0,
        WorkspaceMembership.ROLE_ADMIN: 1,
        WorkspaceMembership.ROLE_MEMBER: 2,
    }
    memberships = sorted(
        workspace.memberships.select_related("user"),
        key=lambda m: (rank.get(m.role, 3), m.user.email.lower()),
    )
    owner_count = sum(1 for m in memberships if m.role == WorkspaceMembership.ROLE_OWNER)
    return render(
        request,
        "cpanel/workspaces/members.html",
        {
            "workspace": workspace,
            "memberships": memberships,
            "role_choices": WorkspaceMembership.ROLE_CHOICES,
            # A workspace must keep at least one owner, so the last owner's
            # role/removal controls are disabled in the UI (and refused below).
            "owner_count": owner_count,
            # Admins manage members but never the Owner role (grant, change, remove).
            "can_manage_owners": _may_manage_owners(request, caller),
        },
    )


@login_required
@require_POST
def workspace_member_add_view(request: HttpRequest, pk) -> HttpResponse:
    """Add an EXISTING user to the workspace with a role (Owner/Admin/superuser).

    Inviting a brand-new (account-less) user with a workspace + role is the
    deferred invite-flow extension; here the target must already have an account.
    """
    workspace = get_object_or_404(Workspace, pk=pk)
    caller = _require_manage(request, workspace)

    email = (request.POST.get("email") or "").strip().lower()
    role = request.POST.get("role") or WorkspaceMembership.ROLE_MEMBER
    if role not in dict(WorkspaceMembership.ROLE_CHOICES):
        role = WorkspaceMembership.ROLE_MEMBER
    if role == WorkspaceMembership.ROLE_OWNER and not _may_manage_owners(request, caller):
        messages.error(request, "Only an owner can add another owner.")
        return redirect("cpanel:workspace_members", pk=pk)

    if not email:
        messages.error(request, "Email is required.")
        return redirect("cpanel:workspace_members", pk=pk)

    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        messages.error(
            request,
            f"No user with email '{email}' exists. They must have an account first.",
        )
        return redirect("cpanel:workspace_members", pk=pk)

    membership, created = WorkspaceMembership.objects.get_or_create(
        user=user, workspace=workspace, defaults={"role": role}
    )
    if created:
        messages.success(request, f"Added {email} as {dict(WorkspaceMembership.ROLE_CHOICES)[role]}.")
    else:
        messages.info(request, f"{email} is already a member of this workspace.")
    return redirect("cpanel:workspace_members", pk=pk)


@login_required
@require_POST
def workspace_member_role_view(request: HttpRequest, pk, membership_id) -> HttpResponse:
    """Change a member's role (Owner/Admin or superuser)."""
    workspace = get_object_or_404(Workspace, pk=pk)
    caller = _require_manage(request, workspace)

    membership = get_object_or_404(
        WorkspaceMembership, pk=membership_id, workspace=workspace
    )
    role = request.POST.get("role")
    if role not in dict(WorkspaceMembership.ROLE_CHOICES):
        messages.error(request, "Invalid role.")
        return redirect("cpanel:workspace_members", pk=pk)

    if WorkspaceMembership.ROLE_OWNER in (role, membership.role) and not _may_manage_owners(
        request, caller
    ):
        messages.error(request, "Only an owner can grant or change the Owner role.")
        return redirect("cpanel:workspace_members", pk=pk)

    # Never demote the workspace's last owner — it must always have one.
    if (
        membership.role == WorkspaceMembership.ROLE_OWNER
        and role != WorkspaceMembership.ROLE_OWNER
        and not _has_other_owner(workspace, membership)
    ):
        messages.error(request, "A workspace must keep at least one owner.")
        return redirect("cpanel:workspace_members", pk=pk)

    membership.role = role
    membership.save(update_fields=["role", "updated_at"])
    messages.success(request, f"Updated {membership.user.email} to {membership.get_role_display()}.")
    return redirect("cpanel:workspace_members", pk=pk)


@login_required
@require_POST
def workspace_member_remove_view(request: HttpRequest, pk, membership_id) -> HttpResponse:
    """Remove a member from the workspace (Owner/Admin or superuser)."""
    workspace = get_object_or_404(Workspace, pk=pk)
    caller = _require_manage(request, workspace)

    membership = get_object_or_404(
        WorkspaceMembership, pk=membership_id, workspace=workspace
    )

    if membership.role == WorkspaceMembership.ROLE_OWNER and not _may_manage_owners(request, caller):
        messages.error(request, "Only an owner can remove an owner.")
        return redirect("cpanel:workspace_members", pk=pk)

    # Never remove the last owner — it would leave the workspace unmanageable.
    if membership.role == WorkspaceMembership.ROLE_OWNER and not _has_other_owner(
        workspace, membership
    ):
        messages.error(request, "A workspace must keep at least one owner.")
        return redirect("cpanel:workspace_members", pk=pk)

    email = membership.user.email
    membership.delete()
    messages.success(request, f"Removed {email} from the workspace.")
    return redirect("cpanel:workspace_members", pk=pk)


def _has_other_owner(workspace, membership) -> bool:
    """Whether the workspace has an owner OTHER than ``membership``."""
    return (
        WorkspaceMembership.objects.filter(
            workspace=workspace, role=WorkspaceMembership.ROLE_OWNER
        )
        .exclude(pk=membership.pk)
        .exists()
    )


def _may_manage_owners(request, caller) -> bool:
    """Only an Owner (or a superuser) may grant, change or remove the Owner role.

    ``caller`` is the membership ``_require_manage`` returned (None for a
    superuser who isn't a member). Without this, an Admin could promote
    themselves to Owner and then delete the workspace.
    """
    return request.user.is_superuser or (
        caller is not None and caller.role == WorkspaceMembership.ROLE_OWNER
    )
