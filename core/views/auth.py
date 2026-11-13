"""
Authentication views. Password-based login plus email-based password reset;
invite onboarding uses a set-password flow (no passwordless login surface).
"""
import hashlib
import logging

from axes.utils import reset as reset_login_attempts
from django.contrib.auth.hashers import make_password
from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_http_methods, require_POST
from django.views.decorators.csrf import csrf_protect
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django_q.tasks import async_task

from core.ratelimit import client_ip, rate_limit_exceeded
from core.models import User, UserInvite, PasswordResetToken
from core.models.settings import GlobalSettings
from core.services import security_events, two_factor
from core.forms import ChangePasswordForm, PasswordLoginForm, SetPasswordForm
from core.services import RecaptchaService, EncryptionService, EncryptionError

logger = logging.getLogger(__name__)

# Forgot-password throttle (fixed windows, core.ratelimit): requests per client
# IP and per email address. A throttled request gets the same answer as any
# other; only the email isn't sent.
FORGOT_IP_LIMIT = 10
FORGOT_EMAIL_LIMIT = 3
FORGOT_WINDOW_SECONDS = 3600

# The one answer to every forgot-password request, whether or not the account
# exists, has a password, or was throttled.
FORGOT_SENT_MESSAGE = "If an account exists with that email, a password reset link has been sent."

# A reset link's token moves into the session on its first GET; the form then
# lives at reset-password/<RESET_URL_TOKEN>/ (the token itself never is that).
RESET_SESSION_KEY = "pyrun_reset_token"
RESET_URL_TOKEN = "set-password"

# Login and forgot password refuse a failed reCAPTCHA check with this.
RECAPTCHA_FAILED_MESSAGE = "reCAPTCHA verification failed. Please try again."

# Where a correct password leads when two-factor sign-in applies (2.5 T46).
TWO_FACTOR_STEPS = {"verify": "auth:two_factor_verify", "enroll": "auth:two_factor_enroll"}


@csrf_protect
@require_http_methods(["GET", "POST"])
def login_view(request: HttpRequest) -> HttpResponse:
    """
    Display the login form and handle password authentication.
    GET: Show the password login form.
    POST: Authenticate the submitted credentials.
    """
    if request.user.is_authenticated:
        return redirect("cpanel:dashboard")

    settings = GlobalSettings.get_settings()
    email_enabled = settings.email_backend != GlobalSettings.EmailBackend.DISABLED
    recaptcha_active = settings.recaptcha_active()

    password_form = PasswordLoginForm()
    error_message = None

    if request.method == "POST":
        action = request.POST.get("action", "password")

        if recaptcha_active and not _verify_recaptcha(request, settings):
            # Bot protection: reject before processing any login action.
            error_message = RECAPTCHA_FAILED_MESSAGE
        elif action == "password":
            # Password authentication
            password_form = PasswordLoginForm(request.POST)
            if password_form.is_valid():
                email = password_form.cleaned_data["email"].lower()
                password = password_form.cleaned_data["password"]

                # The keyword must be ``email`` (the USERNAME_FIELD): django-axes
                # reads the account from that credential key. Passed as
                # ``username`` it recorded every failure against no account, so
                # the lockout never blocked a correct password.
                user = authenticate(request, email=email, password=password)
                if user is not None:
                    # Two-factor (2.5 T46): the password alone doesn't start
                    # the session when the account has an authenticator, or
                    # must set one up; the code step does.
                    step = two_factor.sign_in_step(user)
                    if step is not None:
                        two_factor.stash(request, user, "login")
                        return redirect(TWO_FACTOR_STEPS[step])
                    login(request, user)
                    security_events.record("login_ok", request=request, user=user)
                    _queue_login_notification(request, user)
                    messages.success(request, f"Welcome back, {user.email}!")
                    return redirect("cpanel:dashboard")
                # One message for every failure: a wrong password, an unknown
                # email and an account without a password look the same (the
                # forgot-password page says how to set a first password).
                error_message = "Invalid email or password."
                account = User.objects.filter(email=email).first()
                if account is not None and not account.has_usable_password():
                    # Django hashes the password for an unknown email but not
                    # for an unusable one; hash once so the answer takes as long.
                    make_password(password)
                # A refused attempt on a locked account is recorded as a
                # lockout by the django-axes signal instead.
                if not getattr(request, "axes_locked_out", False):
                    security_events.record("login_fail", request=request, user=account, email=email)

    return render(request, "auth/login.html", {
        "password_form": password_form,
        "email_enabled": email_enabled,
        "error_message": error_message,
        "recaptcha_active": recaptcha_active,
        "recaptcha_site_key": settings.recaptcha_site_key,
    })


def _verify_recaptcha(request: HttpRequest, settings: GlobalSettings) -> bool:
    """Verify the reCAPTCHA token submitted with the login or forgot-password form."""
    token = request.POST.get("g-recaptcha-response", "")
    try:
        secret = EncryptionService.decrypt(settings.recaptcha_secret_key_encrypted)
    except EncryptionError:
        logger.error("Could not decrypt reCAPTCHA secret key for login verification")
        return False
    # The left-most X-Forwarded-For entry is caller-controlled; use the same
    # proxy-aware address as the rate limits and lockout.
    return RecaptchaService.verify(secret, token, client_ip(request))


def _queue_login_notification(request: HttpRequest, user: User) -> None:
    """Queue the sign-in alert for a user who turned it on (sent by a task)."""
    if not user.notify_on_login:
        return
    ip, user_agent = security_events.request_meta(request)
    try:
        async_task(
            "core.services.security_events.send_login_notification",
            user.pk,
            ip,
            user_agent[:255],
            timezone.now().strftime("%Y-%m-%d %H:%M:%S UTC"),
            request.build_absolute_uri("/"),
            task_name="login-notification",
        )
    except Exception:
        # The alert is best effort: the sign-in itself already succeeded.
        logger.exception("Could not queue the sign-in alert for user %s", user.pk)


@require_POST
@csrf_protect
def logout_view(request: HttpRequest) -> HttpResponse:
    """
    Log user out and redirect to login page.
    """
    logout(request)
    messages.info(request, "You have been logged out.")
    return redirect("auth:login")


@csrf_protect
@require_http_methods(["GET", "POST"])
def accept_invite_view(request: HttpRequest, token: str) -> HttpResponse:
    """
    Handle an invite link: validate the invite, then let the invited user set a
    password. The account is created (with a usable password) only when the form
    is submitted, so no passwordless login surface is ever created.
    """
    try:
        invite = UserInvite.objects.get(token=token)
    except UserInvite.DoesNotExist:
        return render(request, "auth/invite_invalid.html", {
            "error": "Invalid invite link",
            "message": "This invite link is invalid or has already been used."
        })

    if not invite.is_valid():
        if invite.used_at:
            error_title = "Invite already used"
            error_message = "This invite has already been used."
        else:
            error_title = "Invite expired"
            error_message = "This invite has expired. Please request a new one from an administrator."

        return render(request, "auth/invite_invalid.html", {
            "error": error_title,
            "message": error_message
        })

    if request.method == "POST":
        # A transient User carries the invite email into the similarity
        # validator even when the account doesn't exist yet.
        form = SetPasswordForm(
            request.POST,
            user=User.objects.filter(email=invite.email).first()
            or User(email=invite.email),
        )
        if form.is_valid():
            # Create/activate the invited account WITH a password. get_or_create
            # keeps the post-save membership signal in play (invitee -> member).
            user, _ = User.objects.get_or_create(
                email=invite.email,
                defaults={"is_verified": True},
            )
            user.is_verified = True
            user.set_password(form.cleaned_data["password"])
            user.save()

            invite.mark_used(user)

            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            messages.success(request, f"Welcome to PyRun, {user.email}!")
            return redirect("cpanel:dashboard")
    else:
        form = SetPasswordForm()

    return render(request, "auth/accept_invite.html", {
        "form": form,
        "email": invite.email,
        "token": token,
    })


# =============================================================================
# Password Management Views
# =============================================================================

@login_required
@csrf_protect
@require_http_methods(["GET", "POST"])
def change_password_view(request: HttpRequest) -> HttpResponse:
    """Allow users to set or change their password."""
    # Settings (where superusers manage the account) is superuser-only.
    done_url = "cpanel:settings" if request.user.is_superuser else "cpanel:dashboard"
    if request.method == "POST":
        form = ChangePasswordForm(request.POST, user=request.user)
        if form.is_valid():
            password = form.cleaned_data["password"]
            request.user.set_password(password)
            request.user.save()
            security_events.record("password_change", request=request, user=request.user)

            # Keep the session valid after the hash change. login() without a
            # backend argument would crash here — with multiple auth backends
            # configured (axes + ModelBackend), Django requires an explicit
            # backend for session-loaded users.
            update_session_auth_hash(request, request.user)
            messages.success(request, "Password updated successfully.")
            return redirect(done_url)
    else:
        form = ChangePasswordForm(user=request.user)

    return render(request, "auth/change_password.html", {
        "form": form,
        "has_password": request.user.has_usable_password(),
        "done_url": done_url,
        "notify_on_login": request.user.notify_on_login,
        "two_factor_enabled": two_factor.is_enabled(request.user),
    })


@login_required
@require_POST
def login_notifications_view(request: HttpRequest) -> HttpResponse:
    """Turn the signed-in user's sign-in alert emails on or off."""
    enabled = request.POST.get("enabled") == "1"
    request.user.notify_on_login = enabled
    request.user.save(update_fields=["notify_on_login"])
    if enabled:
        messages.success(request, "Sign-in alerts are on: you get an email after every sign-in.")
    else:
        messages.success(request, "Sign-in alerts are off.")
    return redirect("auth:change_password")


@csrf_protect
@require_http_methods(["GET", "POST"])
def forgot_password_view(request: HttpRequest) -> HttpResponse:
    """Request a password reset email."""
    settings = GlobalSettings.get_settings()
    email_enabled = settings.email_backend != GlobalSettings.EmailBackend.DISABLED

    if not email_enabled:
        messages.error(request, "Password reset is not available. Please contact an administrator.")
        return redirect("auth:login")

    recaptcha_active = settings.recaptcha_active()
    context = {"recaptcha_active": recaptcha_active, "recaptcha_site_key": settings.recaptcha_site_key}

    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()

        if recaptcha_active and not _verify_recaptcha(request, settings):
            # Same check and answer as the login form; nothing is sent.
            messages.error(request, RECAPTCHA_FAILED_MESSAGE)
            return render(request, "auth/forgot_password.html", context)

        if not email:
            messages.error(request, "Please enter your email address.")
            return render(request, "auth/forgot_password.html", context)

        # Same answer for every address, known or not. The lookup, the token
        # and the email happen in a task, so the response time doesn't depend
        # on whether the account exists either.
        messages.success(request, FORGOT_SENT_MESSAGE)

        email_key = hashlib.sha256(email.encode("utf-8")).hexdigest()
        # Count both budgets on every request (no short-circuit).
        over_ip = rate_limit_exceeded(
            f"pwreset:ip:{client_ip(request)}", FORGOT_IP_LIMIT, FORGOT_WINDOW_SECONDS
        )
        over_email = rate_limit_exceeded(
            f"pwreset:email:{email_key}", FORGOT_EMAIL_LIMIT, FORGOT_WINDOW_SECONDS
        )
        throttled = over_ip or over_email
        security_events.record(
            "reset_request",
            request=request,
            user=User.objects.filter(email=email).first(),
            email=email,
            detail="throttled, no email sent" if throttled else "",
        )
        if throttled:
            logger.warning("Forgot-password request throttled (ip=%s, email=%s)", over_ip, over_email)
        else:
            async_task(
                "core.services.security_events.send_password_reset",
                email,
                request.build_absolute_uri("/"),
                task_name="password-reset",
            )

        return redirect("auth:login")

    return render(request, "auth/forgot_password.html", context)


@csrf_protect
@require_http_methods(["GET", "POST"])
def reset_password_view(request: HttpRequest, token: str) -> HttpResponse:
    """Reset password using a token from email.

    The first GET of an emailed link moves the token into the session and
    redirects to a token-free address (Django's PasswordResetConfirmView
    pattern), so the form page's URL, the server log of later requests and
    any Referer never carry it. A POST to the tokened address still works.
    """
    if token == RESET_URL_TOKEN:
        token = request.session.get(RESET_SESSION_KEY, "")
    elif request.method == "GET" and PasswordResetToken.objects.filter(token=token).exists():
        # Checked again (used, expired) on the token-free page.
        request.session[RESET_SESSION_KEY] = token
        return redirect("auth:reset_password", token=RESET_URL_TOKEN)
    try:
        reset_token = PasswordResetToken.objects.get(token=token)
    except PasswordResetToken.DoesNotExist:
        return render(request, "auth/reset_password.html", {
            "error": "Invalid link",
            "message": "This password reset link is invalid. Please request a new one."
        })

    if not reset_token.is_valid():
        if reset_token.used_at:
            error_title = "Link already used"
            error_message = "This password reset link has already been used."
        else:
            error_title = "Link expired"
            error_message = "This password reset link has expired. Please request a new one."

        return render(request, "auth/reset_password.html", {
            "error": error_title,
            "message": error_message
        })

    if request.method == "POST":
        form = SetPasswordForm(request.POST, user=reset_token.user)
        if form.is_valid():
            request.session.pop(RESET_SESSION_KEY, None)
            user = reset_token.consume()
            user.set_password(form.cleaned_data["password"])
            user.save()
            # The link proves the mailbox (or an admin issued it): a lockout
            # from the forgotten password shouldn't outlive the reset.
            reset_login_attempts(username=user.email.lower())
            security_events.record("reset", request=request, user=user)

            # The reset proves the mailbox, not the second factor: an account
            # with two-factor (or one that must set it up) goes through the
            # same code step as a sign-in.
            step = two_factor.sign_in_step(user)
            if step is not None:
                two_factor.stash(request, user, "reset")
                messages.success(request, "Password has been reset. Finish signing in.")
                return redirect(TWO_FACTOR_STEPS[step])
            login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            messages.success(request, "Password has been reset successfully.")
            return redirect("cpanel:dashboard")
    else:
        form = SetPasswordForm()

    return render(request, "auth/reset_password.html", {"form": form})


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    """Console-styled CSRF failure page (settings.CSRF_FAILURE_VIEW).

    A stale form (left open while the session changed) otherwise shows
    Django's plain debug-style 403 page.
    """
    return render(request, "403_csrf.html", status=403)
