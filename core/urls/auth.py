"""
URL patterns for authentication.
"""
from django.urls import path
from core.views.auth import (
    login_view,
    logout_view,
    accept_invite_view,
    change_password_view,
    forgot_password_view,
    reset_password_view,
    login_notifications_view,
)
from core.views import two_factor

app_name = "auth"

urlpatterns = [
    path("login/", login_view, name="login"),
    path("logout/", logout_view, name="logout"),
    path("invite/<str:token>/", accept_invite_view, name="accept_invite"),
    # Password management
    path("change-password/", change_password_view, name="change_password"),
    path("sign-in-alerts/", login_notifications_view, name="login_notifications"),
    path("forgot-password/", forgot_password_view, name="forgot_password"),
    path("reset-password/<str:token>/", reset_password_view, name="reset_password"),
    # Two-factor sign-in (2.5 T46): the steps before the session exists...
    path("login/code/", two_factor.verify_view, name="two_factor_verify"),
    path("login/two-factor-setup/", two_factor.enroll_view, name="two_factor_enroll"),
    # ...and the account page.
    path("two-factor/", two_factor.manage_view, name="two_factor"),
    path("two-factor/start/", two_factor.start_view, name="two_factor_start"),
    path("two-factor/confirm/", two_factor.confirm_view, name="two_factor_confirm"),
    path("two-factor/disable/", two_factor.disable_view, name="two_factor_disable"),
    path("two-factor/recovery-codes/", two_factor.recovery_codes_view, name="two_factor_codes"),
]
