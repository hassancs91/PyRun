"""
Two-factor sign-in pages (2.5 Stage 11, T46).

Two groups:

- The sign-in steps, before the session exists: the code step after a correct
  password, and the forced set-up when ``require_2fa`` is on and the account
  has no authenticator. Both read the pre-2FA stash
  (``core.services.two_factor.stashed``) and start the session only when the
  code is right.
- The account page (signed in): set up, confirm, turn off, and replace the
  recovery codes. Turning it off and replacing the codes need a current code.

Every code attempt, on either side, counts against the per-account and per-IP
budget. Messages here are public text: no commands, paths or setting names.
"""

import logging

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods, require_POST

from core.services import security_events
from core.services import two_factor
from core.services.two_factor import TwoFactorUnavailable
from core.views import auth as auth_views

logger = logging.getLogger(__name__)

THROTTLED_MESSAGE = "Too many attempts. Wait a few minutes, then try again."
WRONG_CODE_MESSAGE = "That code didn't work. Check the code and try again."
EXPIRED_MESSAGE = "Your sign-in took too long. Sign in again."
UNAVAILABLE_MESSAGE = "Two-factor sign-in can't be set up right now. Contact your administrator."
CANT_CHECK_MESSAGE = "Your code can't be checked right now. Contact your administrator."
# A few left: say so after a recovery code is used.
LOW_RECOVERY_CODES = 3


def _setup_context(user, secret):
    uri = two_factor.provisioning_uri(secret, user.email, two_factor.issuer())
    return {
        "qr_svg": two_factor.qr_svg(uri),
        "secret_grouped": two_factor.group_secret(secret),
        "digits": two_factor.DIGITS,
    }


def _refuse(request, user, detail):
    security_events.record("2fa_fail", request=request, user=user, detail=detail)


def _check_attempt(request, user):
    """Count one attempt; False (and an event) when over budget."""
    if two_factor.attempt_allowed(request, user.pk):
        return True
    _refuse(request, user, "too many attempts")
    return False


# ---------------------------------------------------------------------------
# Sign-in steps (no session yet)
# ---------------------------------------------------------------------------


def _start_session(request, user, stash, how):
    """The code was right: start the session the password step held back."""
    two_factor.clear_stash(request)
    login(request, user, backend=stash["backend"])
    left = None
    if how == "recovery":
        left = two_factor.recovery_codes_left(user)
        detail = f"recovery code, {left} left"
    else:
        detail = {"totp": "two-factor code", "enroll": "two-factor set up"}.get(how, "")
    if stash.get("purpose") == "reset":
        detail = f"after password reset, {detail}" if detail else "after password reset"
    # One sign-in event per sign-in, recorded once the second step passed.
    security_events.record("login_ok", request=request, user=user, detail=detail)
    # A reset already said "Password has been reset" before the code step.
    if stash.get("purpose") != "reset":
        auth_views._queue_login_notification(request, user)
        messages.success(request, f"Welcome back, {user.email}!")
    if left is not None and left <= LOW_RECOVERY_CODES:
        messages.warning(
            request,
            f"You have {left} recovery code{'' if left == 1 else 's'} left. "
            "Make a new set on the Two-factor sign-in page.",
        )


@never_cache
@csrf_protect
@require_http_methods(["GET", "POST"])
def verify_view(request: HttpRequest) -> HttpResponse:
    """The code step after a correct password."""
    if request.user.is_authenticated:
        return redirect("cpanel:dashboard")
    user, stash = two_factor.stashed(request)
    if user is None:
        messages.info(request, EXPIRED_MESSAGE)
        return redirect("auth:login")
    step = two_factor.sign_in_step(user)
    if step == "enroll":
        return redirect("auth:two_factor_enroll")
    if step is None:
        # Turned off from the command line in the meantime: the password
        # was right and nothing else is asked for.
        _start_session(request, user, stash, "password")
        return redirect("cpanel:dashboard")

    error = None
    if request.method == "POST":
        if not _check_attempt(request, user):
            error = THROTTLED_MESSAGE
        else:
            try:
                how = two_factor.check_code(user, request.POST.get("code", ""))
            except TwoFactorUnavailable:
                how = None
                error = CANT_CHECK_MESSAGE
            if how:
                _start_session(request, user, stash, how)
                return redirect("cpanel:dashboard")
            if error is None:
                error = WRONG_CODE_MESSAGE
                _refuse(request, user, "wrong code")
    return render(request, "auth/two_factor_verify.html", {"error": error, "digits": two_factor.DIGITS})


@never_cache
@csrf_protect
@require_http_methods(["GET", "POST"])
def enroll_view(request: HttpRequest) -> HttpResponse:
    """Set up an authenticator before the first session (``require_2fa`` is on)."""
    if request.user.is_authenticated:
        return redirect("cpanel:dashboard")
    user, stash = two_factor.stashed(request)
    if user is None:
        messages.info(request, EXPIRED_MESSAGE)
        return redirect("auth:login")
    if two_factor.is_enabled(user):
        return redirect("auth:two_factor_verify")

    try:
        secret = two_factor.begin_enrollment(user)
    except TwoFactorUnavailable:
        return render(request, "auth/two_factor_enroll.html", {"unavailable": UNAVAILABLE_MESSAGE})

    error = None
    if request.method == "POST":
        if not _check_attempt(request, user):
            error = THROTTLED_MESSAGE
        else:
            codes = two_factor.confirm_enrollment(user, request.POST.get("code", ""))
            if codes:
                security_events.record("2fa_enroll", request=request, user=user, detail="required at sign-in")
                _start_session(request, user, stash, "enroll")
                request.session[two_factor.NEW_CODES_KEY] = codes
                return redirect("auth:two_factor_codes")
            error = WRONG_CODE_MESSAGE
            _refuse(request, user, "wrong code while setting up")
    return render(request, "auth/two_factor_enroll.html", {"error": error, **_setup_context(user, secret)})


# ---------------------------------------------------------------------------
# Account page (signed in)
# ---------------------------------------------------------------------------


@never_cache
@login_required
def manage_view(request: HttpRequest) -> HttpResponse:
    """Two-factor sign-in: status, set up / confirm, turn off, recovery codes."""
    user = request.user
    row = two_factor.authenticator(user)
    context = {
        "enabled": bool(row and row.confirmed_at),
        "confirmed_at": row.confirmed_at if row else None,
        "pending": bool(row and not row.confirmed_at),
        "required": two_factor.is_required(),
        "codes_left": two_factor.recovery_codes_left(user),
        "code_count": two_factor.RECOVERY_CODE_COUNT,
        "digits": two_factor.DIGITS,
    }
    if context["pending"]:
        try:
            context.update(_setup_context(user, two_factor.pending_secret(user)))
        except TwoFactorUnavailable:
            context["unavailable"] = UNAVAILABLE_MESSAGE
    return render(request, "auth/two_factor.html", context)


@login_required
@require_POST
def start_view(request: HttpRequest) -> HttpResponse:
    """Start setting up an authenticator (shows the QR code on the account page)."""
    try:
        two_factor.begin_enrollment(request.user)
    except ValueError:
        messages.info(request, "Two-factor sign-in is already on.")
    except TwoFactorUnavailable:
        messages.error(request, UNAVAILABLE_MESSAGE)
    return redirect("auth:two_factor")


@login_required
@require_POST
def confirm_view(request: HttpRequest) -> HttpResponse:
    """Confirm the pending authenticator with a code; shows the recovery codes once."""
    user = request.user
    if not _check_attempt(request, user):
        messages.error(request, THROTTLED_MESSAGE)
        return redirect("auth:two_factor")
    try:
        codes = two_factor.confirm_enrollment(user, request.POST.get("code", ""))
    except TwoFactorUnavailable:
        messages.error(request, UNAVAILABLE_MESSAGE)
        return redirect("auth:two_factor")
    if not codes:
        _refuse(request, user, "wrong code while setting up")
        messages.error(request, WRONG_CODE_MESSAGE)
        return redirect("auth:two_factor")
    security_events.record("2fa_enroll", request=request, user=user)
    request.session[two_factor.NEW_CODES_KEY] = codes
    messages.success(request, "Two-factor sign-in is on.")
    return redirect("auth:two_factor_codes")


def _code_checked(request, user) -> bool:
    """A current code (authenticator or recovery) for a sensitive change."""
    if not _check_attempt(request, user):
        messages.error(request, THROTTLED_MESSAGE)
        return False
    try:
        how = two_factor.check_code(user, request.POST.get("code", ""))
    except TwoFactorUnavailable:
        # Not the user's mistake: the secret can't be read on this server.
        messages.error(request, CANT_CHECK_MESSAGE)
        return False
    if how is None:
        _refuse(request, user, "wrong code on the account page")
        messages.error(request, WRONG_CODE_MESSAGE)
        return False
    return True


@login_required
@require_POST
def disable_view(request: HttpRequest) -> HttpResponse:
    """Turn two-factor sign-in off (needs a current code), or cancel a set-up."""
    user = request.user
    row = two_factor.authenticator(user)
    if row is None:
        return redirect("auth:two_factor")
    if not row.confirmed_at:
        # Cancelling a set-up that was never confirmed guards nothing yet.
        two_factor.disable(user)
        messages.info(request, "Set-up cancelled.")
        return redirect("auth:two_factor")
    if not _code_checked(request, user):
        return redirect("auth:two_factor")
    two_factor.disable(user)
    security_events.record("2fa_disable", request=request, user=user)
    note = " This instance requires it, so you'll set it up again at your next sign-in." if two_factor.is_required() else ""
    messages.success(request, "Two-factor sign-in is off." + note)
    return redirect("auth:two_factor")


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def recovery_codes_view(request: HttpRequest) -> HttpResponse:
    """GET: the codes just issued, shown once. POST: replace them (needs a current code)."""
    user = request.user
    if request.method == "POST":
        if not two_factor.is_enabled(user) or not _code_checked(request, user):
            return redirect("auth:two_factor")
        request.session[two_factor.NEW_CODES_KEY] = two_factor.replace_recovery_codes(user)
        security_events.record("2fa_codes", request=request, user=user)
        messages.success(request, "New recovery codes made. The old ones no longer work.")
        return redirect("auth:two_factor_codes")

    codes = request.session.pop(two_factor.NEW_CODES_KEY, None)
    if not codes:
        return redirect("auth:two_factor")
    return render(request, "auth/two_factor_codes.html", {"codes": codes})
