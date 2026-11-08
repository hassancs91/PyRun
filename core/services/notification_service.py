"""
Notification service for sending email and webhook notifications.

This module handles sending notifications when script runs complete.
Supports email via SMTP or Resend, and webhook POST notifications.
"""

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import requests
from django.core.mail import EmailMultiAlternatives
from django.core.mail.backends.smtp import EmailBackend as SMTPBackend
from django.template.loader import render_to_string
from django.utils import timezone

from core.netguard import is_safe_endpoint_url

from .encryption_service import EncryptionService, EncryptionError
from .run_reports import (
    CHANNEL_RESULT_MAX_CHARS,
    EMAIL_RESULT_MAX_CHARS,
    notification_result,
    result_excerpt,
)

if TYPE_CHECKING:
    from core.models import Run, GlobalSettings

logger = logging.getLogger(__name__)


class NotificationService:
    """
    Handles sending notifications via email and webhooks.
    """

    WEBHOOK_TIMEOUT = 10  # seconds
    # A wrong port (465 implicit TLS, a filtered port) otherwise blocks the
    # sending thread with no limit: a web request on Test, a worker on notify.
    SMTP_TIMEOUT = 20  # seconds

    @staticmethod
    def _subject(text: str) -> str:
        """Email subject prefixed with the instance name (Settings → General)."""
        from core.models import GlobalSettings

        try:
            name = (GlobalSettings.get_settings().instance_name or "").strip()
        except Exception:
            name = ""
        return f"[{name or 'PyRun'}] {text}"

    @classmethod
    def _webhook_url_problem(cls, url: str) -> str:
        """
        Why a webhook URL may not be used, or "" when it is safe (SSRF guard).

        Enforces http/https, then delegates to the shared ``core.netguard`` guard,
        which DNS-resolves the hostname (a public name pointing at a private IP is
        caught too), blocks loopback/private hosts unless the operator set
        ``PYRUNNER_ALLOW_PRIVATE_NETWORK``, and never lets the cloud-metadata range
        or the unspecified addresses through. The guard's reason is kept so the
        logged refusal names the cause (and the flag, when the flag would help).
        """
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                return "it must be an http:// or https:// URL with a host name"
            is_safe, why = is_safe_endpoint_url(url)
            return "" if is_safe else (why or "the address is not allowed")
        except Exception as e:  # noqa: BLE001 — a malformed URL is simply refused
            return f"it could not be checked ({e})"

    @classmethod
    def _is_safe_webhook_url(cls, url: str) -> bool:
        """True when ``url`` passes the webhook SSRF guard (see ``_webhook_url_problem``)."""
        return not cls._webhook_url_problem(url)

    @classmethod
    def should_notify(cls, run: "Run") -> bool:
        """
        Determine if a notification should be sent for this run.

        Args:
            run: The completed Run instance

        Returns:
            bool: True if notification should be sent
        """
        from core.models import Run, Script

        script = run.script
        notify_on = script.notify_on

        if notify_on == Script.NotifyOn.NEVER:
            return False
        elif notify_on == Script.NotifyOn.SUCCESS:
            return run.status == Run.Status.SUCCESS
        elif notify_on == Script.NotifyOn.FAILURE:
            wanted = run.status in (Run.Status.FAILED, Run.Status.TIMEOUT)
        elif notify_on == Script.NotifyOn.BOTH:
            wanted = run.is_finished
        else:
            return False
        if wanted and run.status in (Run.Status.FAILED, Run.Status.TIMEOUT):
            # Retry on failure: only the last failed attempt reports.
            from core.services.run_retries import retry_follows

            return not retry_follows(run)
        return wanted

    @classmethod
    def send_notification(cls, run: "Run") -> dict:
        """
        Send all applicable notifications for a completed run.

        Args:
            run: The completed Run instance

        Returns:
            dict: Results of notification attempts
        """
        results = {
            "email_sent": False,
            "email_error": None,
            "webhook_sent": False,
            "webhook_error": None,
            "channels_sent": 0,
            "channel_errors": [],
        }

        if not cls.should_notify(run):
            logger.debug(f"Skipping notification for run {run.id} - notify_on={run.script.notify_on}")
            return results

        # Send email notification
        if cls._should_send_email(run):
            try:
                cls._send_email_notification(run)
                results["email_sent"] = True
                logger.info(f"Email notification sent for run {run.id}")
            except Exception as e:
                logger.error(f"Failed to send email notification for run {run.id}: {e}")
                results["email_error"] = str(e)

        # Send webhook notification
        if run.script.notify_webhook_enabled and run.script.notify_webhook_url:
            try:
                cls._send_webhook_notification(run)
                results["webhook_sent"] = True
                logger.info(f"Webhook notification sent for run {run.id}")
            except Exception as e:
                logger.error(f"Failed to send webhook notification for run {run.id}: {e}")
                results["webhook_error"] = str(e)

        # Send chat-channel notifications (Channels subsystem)
        channel_outcome = cls._send_channel_notifications(run)
        results["channels_sent"] = channel_outcome["sent"]
        results["channel_errors"] = channel_outcome["errors"]

        return results

    @classmethod
    def _send_channel_notifications(cls, run: "Run") -> dict:
        """Deliver a run summary to each enabled chat channel on the script."""
        sent = 0
        errors = []
        channels = list(run.script.notify_channels.filter(enabled=True))
        if not channels:
            return {"sent": 0, "errors": errors}

        from core.services import ChannelService

        text = cls._build_channel_text(run)
        for channel in channels:
            try:
                ChannelService.send(channel, text)
                sent += 1
                logger.info(f"Channel notification sent for run {run.id} via {channel.name}")
            except Exception as e:
                logger.error(
                    f"Failed to send channel notification for run {run.id} "
                    f"via {channel.name}: {e}"
                )
                errors.append({"channel": channel.name, "error": str(e)})
        return {"sent": sent, "errors": errors}

    @classmethod
    def _build_channel_text(cls, run: "Run") -> str:
        """Build a short plain-text run summary for chat channels."""
        emoji = "✅" if run.status == "success" else "❌"
        lines = [f"{emoji} {run.script.name} — {run.status.upper()}"]

        if run.duration:
            secs = run.duration
            if secs < 60:
                lines.append(f"Duration: {secs:.1f}s")
            else:
                lines.append(f"Duration: {int(secs // 60)}m {int(secs % 60)}s")

        result = result_excerpt(run, CHANNEL_RESULT_MAX_CHARS)
        if result:
            lines.append(f"Result: {result}")

        if run.status in ("failed", "timeout") and run.stderr:
            excerpt = run.stderr.strip()[:300]
            lines.append(f"\n{excerpt}")

        return "\n".join(lines)

    @classmethod
    def _should_send_email(cls, run: "Run") -> bool:
        """Check if email notification should be sent."""
        from core.models import GlobalSettings

        settings = GlobalSettings.get_settings()
        if settings.email_backend == GlobalSettings.EmailBackend.DISABLED:
            return False

        # Check if there's a recipient email
        recipient = run.script.notify_email or settings.default_notification_email
        return bool(recipient)

    @classmethod
    def _get_email_backend(cls, settings: "GlobalSettings") -> SMTPBackend | None:
        """
        Get the appropriate email backend based on settings.

        Returns:
            Email backend instance or None if disabled
        """
        from core.models import GlobalSettings

        if settings.email_backend == GlobalSettings.EmailBackend.SMTP:
            password = ""
            if settings.smtp_password_encrypted:
                try:
                    password = EncryptionService.decrypt(settings.smtp_password_encrypted)
                except EncryptionError as e:
                    logger.warning(
                        f"Failed to decrypt SMTP password: {e}. "
                        "Email notifications may fail due to missing credentials."
                    )

            return SMTPBackend(
                host=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_username,
                password=password,
                use_tls=settings.smtp_use_tls,
                fail_silently=False,
                timeout=cls.SMTP_TIMEOUT,
            )
        elif settings.email_backend == GlobalSettings.EmailBackend.RESEND:
            # Use Resend's SMTP gateway
            api_key = ""
            if settings.resend_api_key_encrypted:
                try:
                    api_key = EncryptionService.decrypt(settings.resend_api_key_encrypted)
                except EncryptionError as e:
                    logger.warning(
                        f"Failed to decrypt Resend API key: {e}. "
                        "Email notifications may fail due to missing credentials."
                    )

            return SMTPBackend(
                host="smtp.resend.com",
                port=587,
                username="resend",
                password=api_key,
                use_tls=True,
                fail_silently=False,
                timeout=cls.SMTP_TIMEOUT,
            )
        return None

    @classmethod
    def _send_email_notification(cls, run: "Run") -> None:
        """Send email notification for a run."""
        from core.models import GlobalSettings

        settings = GlobalSettings.get_settings()
        backend = cls._get_email_backend(settings)

        if not backend:
            raise ValueError("Email backend not configured")

        recipient = run.script.notify_email or settings.default_notification_email
        if settings.email_backend == GlobalSettings.EmailBackend.SMTP:
            from_email = settings.smtp_from_email
        else:
            from_email = settings.resend_from_email

        if not recipient or not from_email:
            raise ValueError("Missing email configuration (recipient or from address)")

        # Render templates
        context = cls._build_email_context(run)
        status_display = run.status.upper()
        subject = cls._subject(f"{run.script.name} - {status_display}")

        text_content = render_to_string("notifications/email/run_completed.txt", context)
        html_content = render_to_string("notifications/email/run_completed.html", context)

        # Send email
        email = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=from_email,
            to=[recipient],
            connection=backend,
        )
        email.attach_alternative(html_content, "text/html")
        email.send()

        logger.info(f"Email notification sent for run {run.id} to {recipient}")

    @classmethod
    def _send_webhook_notification(cls, run: "Run") -> None:
        """Send webhook notification for a run."""
        url = run.script.notify_webhook_url

        # Validate URL to prevent SSRF attacks
        problem = cls._webhook_url_problem(url)
        if problem:
            raise ValueError(f"Webhook URL rejected: {problem}")

        payload = cls._build_webhook_payload(run)

        response = requests.post(
            url,
            json=payload,
            timeout=cls.WEBHOOK_TIMEOUT,
            headers={"Content-Type": "application/json", "User-Agent": "PyRun/1.0"},
        )

        logger.info(
            f"Webhook notification sent for run {run.id} to {url} "
            f"(status: {response.status_code})"
        )

    @classmethod
    def _build_email_context(cls, run: "Run") -> dict:
        """Build context for email templates."""
        # Calculate duration display
        duration_display = "N/A"
        if run.duration:
            duration_secs = run.duration
            if duration_secs < 60:
                duration_display = f"{duration_secs:.1f}s"
            else:
                minutes = int(duration_secs // 60)
                seconds = int(duration_secs % 60)
                duration_display = f"{minutes}m {seconds}s"

        return {
            "run": run,
            "script": run.script,
            "status": run.status,
            "duration": duration_display,
            "result_excerpt": result_excerpt(run, EMAIL_RESULT_MAX_CHARS, indent=2),
            "error_excerpt": run.stderr[:500] if run.stderr else None,
        }

    @classmethod
    def _build_webhook_payload(cls, run: "Run") -> dict:
        """Build JSON payload for webhook notification.

        ``result`` is what the script set with ``pyrunner_run.set_result()``
        (secrets masked), or None. A result over ``NOTIFY_RESULT_MAX_BYTES`` is
        left out and ``result_too_large`` is True.
        """
        result, result_too_large = notification_result(run)
        return {
            "event_type": "run_completed",
            "script": {
                "id": str(run.script.id),
                "name": run.script.name,
            },
            "run": {
                "id": str(run.id),
                "status": run.status,
                "exit_code": run.exit_code,
                "duration_seconds": run.duration,
                "trigger_type": run.trigger_type,
            },
            "timestamps": {
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "ended_at": run.ended_at.isoformat() if run.ended_at else None,
            },
            "result": result,
            "result_too_large": result_too_large,
            "error": run.stderr[:1000] if run.stderr and run.status in ["failed", "timeout"] else None,
        }

    @classmethod
    def send_email(cls, subject: str, body: str, to: str | None = None, html: str | None = None) -> bool:
        """Send a one-off email through the configured core email backend.

        This is the delegate behind ``pyrunner_notify.email()`` — Channels do not
        re-implement email; they reuse PyRun core's single email config. Falls
        back to ``default_notification_email`` when ``to`` is omitted.
        """
        from core.models import GlobalSettings

        settings = GlobalSettings.get_settings()
        backend = cls._get_email_backend(settings)
        if not backend:
            raise ValueError("Email backend not configured or disabled")

        if settings.email_backend == GlobalSettings.EmailBackend.SMTP:
            from_email = settings.smtp_from_email
        else:
            from_email = settings.resend_from_email

        recipient = to or settings.default_notification_email
        if not recipient or not from_email:
            raise ValueError("Missing email configuration (recipient or from address)")

        email = EmailMultiAlternatives(
            subject=subject,
            body=body,
            from_email=from_email,
            to=[recipient],
            connection=backend,
        )
        if html:
            email.attach_alternative(html, "text/html")
        email.send()
        logger.info(f"Email sent to {recipient} via channels send API")
        return True

    @classmethod
    def send_test_email(cls, recipient_email: str) -> bool:
        """
        Send a test email to verify configuration.

        Args:
            recipient_email: Email address to send test to

        Returns:
            bool: True if successful

        Raises:
            Exception: If sending fails
        """
        from core.models import GlobalSettings

        settings = GlobalSettings.get_settings()
        backend = cls._get_email_backend(settings)

        if not backend:
            raise ValueError("Email backend not configured or disabled")

        if settings.email_backend == GlobalSettings.EmailBackend.SMTP:
            from_email = settings.smtp_from_email
        else:
            from_email = settings.resend_from_email

        if not from_email:
            raise ValueError("From email address not configured")

        subject = cls._subject("Test Email")
        text_content = (
            "This is a test email from PyRun.\n\n"
            "If you receive this, your email configuration is working correctly."
        )
        html_content = render_to_string(
            "notifications/email/test_email.html",
            {"timestamp": timezone.now()},
        )

        email = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=from_email,
            to=[recipient_email],
            connection=backend,
        )
        email.attach_alternative(html_content, "text/html")
        email.send()

        logger.info(f"Test email sent to {recipient_email}")
        return True
