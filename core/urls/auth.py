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
]
