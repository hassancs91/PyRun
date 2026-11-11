"""
Delivery ledger for outbound channel messages (2.5 Stage 8, ticket T34).

What this pins, roughly in order of how much it would hurt to get wrong:

- Every outbound message (run notification, inbound reply, Py AI answer,
  ``pyrunner_notify`` send, PyRun's own notices) is one ``ChannelMessage`` row
  with its kind, run, attempts, status, provider message id and error.
- A temporary failure (network, 5xx, Telegram flood control) is retried by the
  heartbeat with backoff, up to MAX_ATTEMPTS; a rejected bot token (401) or any
  other provider error is never retried. A send in progress holds a lease, so
  a row whose sender died is picked up again.
- The reply cap counts replies only (notifications don't use it); the daily
  send cap covers notifications, replies and script sends, so a failing
  schedule can't send past it; PyRun's own notices sit outside both caps.
- ``pyrunner_notify`` answers 200 sent / 202 retrying / 429 cap / 502 failed.
- The run page's Deliveries strip and the channel page's message log show the
  rows, with a run's secrets masked; rows past 30 days are purged daily.
- The webhook notifier fails on a non-2xx status and reports it.

Every Telegram call goes to ``FakeTelegram``, a stand-in for the Bot API
patched over ``requests.post``: nothing reaches api.telegram.org.
"""

import importlib
import json
from datetime import timedelta
from unittest import mock

import requests
from cryptography.fernet import Fernet
from django.apps import apps
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Channel,
    ChannelMessage,
    Environment,
    Run,
    Script,
    User,
    Workspace,
    WorkspaceMembership,
)
from core.services import EncryptionService, NotificationService
from core.services.channels import ChannelError, OutboundMessage, get_provider, ledger
from core.services.channels.inbound import CAP_NOTICE, dispatch_inbound
from core.services.datastore_token import mint_datastore_token
from core.services.secret_backends import SecretResolutionError
from core.tasks import daily_housekeeping_task, worker_heartbeat_task

_TEST_KEY = Fernet.generate_key().decode()
_TOKEN = "111:LEDGER"
_UPDATE = {
    "update_id": 10,
    "message": {
        "message_id": 1,
        "from": {"id": 7, "username": "ann", "first_name": "Ann"},
        "chat": {"id": 7, "type": "private"},
        "text": "hello bot",
    },
}


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeTelegram:
    """A stand-in for the Telegram Bot API, patched over ``requests.post``.

    ``reply(...)`` queues answers in order; with the queue empty every
    sendMessage succeeds with the next message id. ``calls`` keeps
    ``(method, payload)``.
    """

    def __init__(self, test):
        self.calls = []
        self._queue = []
        self._next_id = 100
        patcher = mock.patch("core.services.channels.telegram.requests.post", side_effect=self._post)
        patcher.start()
        test.addCleanup(patcher.stop)

    def ok(self):
        return self.reply(None)

    def reply(self, answer):
        self._queue.append(answer)
        return self

    def network_error(self):
        return self.reply(requests.ConnectionError(f"boom https://api.telegram.org/bot{_TOKEN}/sendMessage"))

    def error(self, status, description, **extra):
        return self.reply(_Resp(status, {"ok": False, "error_code": status, "description": description, **extra}))

    def _post(self, url, json=None, timeout=None):
        assert url.startswith("https://api.telegram.org/bot"), url
        self.calls.append((url.rsplit("/", 1)[1], json))
        answer = self._queue.pop(0) if self._queue else None
        if isinstance(answer, Exception):
            raise answer
        if answer is not None:
            return answer
        self._next_id += 1
        return _Resp(200, {"ok": True, "result": {"message_id": self._next_id, "chat": {"id": json.get("chat_id")}}})

    @property
    def sends(self):
        return [payload for method, payload in self.calls if method == "sendMessage"]


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class _Base(TestCase):
    def setUp(self):
        EncryptionService.reset()
        self.addCleanup(EncryptionService.reset)
        cache.clear()
        for target in (
            "core.services.setup_service.SetupService.is_setup_needed",
            "core.services.setup_service.SetupService.needs_admin_setup",
        ):
            p = mock.patch(target, return_value=False)
            p.start()
            self.addCleanup(p.stop)
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="e", path="p")
        self.script = Script.objects.create(
            name="nightly", code="x", environment=self.env, workspace=self.ws,
            notify_on=Script.NotifyOn.BOTH, is_enabled=True,
        )
        self.run = Run.objects.create(script=self.script, workspace=self.ws, status=Run.Status.FAILED)
        self.ch = self._channel()
        self.tg = FakeTelegram(self)

    def _channel(self, name="Ops", token=_TOKEN, **fields):
        ch = Channel(workspace=self.ws, provider="telegram", name=name, **fields)
        ch.set_credentials({"bot_token": token}, identity=token)
        ch.config = {"default_target": "555"}
        ch.save()
        return ch

    def _out(self, **filters):
        return ChannelMessage.objects.filter(direction="out", **filters)


class DeliverTests(_Base):
    def test_a_sent_message_is_one_ledger_row(self):
        row = ledger.deliver(self.ch, "done", kind="notification", run=self.run)
        row.refresh_from_db()
        self.assertEqual(
            (row.status, row.kind, row.attempts, row.provider_message_id, row.delivery_state, row.error),
            ("sent", "notification", 1, "101", "sent", ""),
        )
        self.assertEqual(row.run_id, self.run.id)
        self.assertIsNone(row.next_attempt_at)
        self.assertEqual(self.tg.sends, [{"chat_id": "555", "text": "done"}])

    def test_a_network_error_is_retried_later(self):
        self.tg.network_error()
        before = timezone.now()
        row = ledger.deliver(self.ch, "done", kind="notification", run=self.run)
        row.refresh_from_db()
        self.assertEqual((row.status, row.attempts), ("retrying", 1))
        self.assertNotIn(_TOKEN, row.error)  # the provider scrubs the token from the URL
        self.assertGreaterEqual(row.next_attempt_at, before + timedelta(seconds=60))
        self.assertLess(row.next_attempt_at, before + timedelta(seconds=120))
        self.ch.refresh_from_db()
        self.assertTrue(self.ch.last_error)

    def test_flood_control_waits_as_long_as_telegram_asks(self):
        self.tg.error(429, "Too Many Requests: retry after 600", parameters={"retry_after": 600})
        before = timezone.now()
        row = ledger.deliver(self.ch, "done", kind="script")
        self.assertEqual(row.status, "retrying")
        self.assertGreaterEqual(row.next_attempt_at, before + timedelta(seconds=600))

    def test_a_server_error_is_retried(self):
        self.tg.error(502, "Bad Gateway")
        self.assertEqual(ledger.deliver(self.ch, "x", kind="script").status, "retrying")

    def test_a_rejected_token_is_never_retried(self):
        self.tg.error(401, "Unauthorized")
        row = ledger.deliver(self.ch, "done", kind="notification")
        row.refresh_from_db()
        self.assertEqual((row.status, row.attempts), ("failed", 1))
        self.assertIn("rejected this bot token", row.error)
        self.assertIsNone(row.next_attempt_at)
        # Even if it looked due, the drain doesn't pick a failed row up.
        self.assertEqual(ledger.drain_due(timezone.now() + timedelta(days=1)), {"sent": 0, "retrying": 0, "failed": 0})
        self.assertEqual(len(self.tg.sends), 1)

    def test_other_provider_errors_fail_at_once(self):
        self.tg.error(400, "Bad Request: chat not found")
        row = ledger.deliver(self.ch, "x", kind="script")
        self.assertEqual((row.status, row.error), ("failed", "Bad Request: chat not found"))

    def test_an_unexpected_exception_fails_the_row_without_raising(self):
        with mock.patch("core.services.ChannelService.send", side_effect=RuntimeError("bug")):
            row = ledger.deliver(self.ch, "x", kind="script")
        self.assertEqual((row.status, row.error), ("failed", "bug"))

    def test_telegram_errors_are_classified(self):
        provider = get_provider("telegram")
        cases = [
            (lambda: self.tg.network_error(), (True, False)),
            (lambda: self.tg.error(401, "Unauthorized"), (False, True)),
            (lambda: self.tg.error(429, "Too Many Requests"), (True, False)),
            (lambda: self.tg.error(500, "Internal"), (True, False)),
            (lambda: self.tg.error(403, "Forbidden: bot was blocked by the user"), (False, False)),
            (lambda: self.tg.reply(_Resp(502, ValueError("not json"))), (True, False)),
            (lambda: self.tg.reply(_Resp(200, ["not", "a", "dict"])), (False, False)),
        ]
        for queue, expected in cases:
            queue()
            with self.assertRaises(ChannelError) as cm:
                provider.send(self.ch, OutboundMessage(text="x"))
            self.assertEqual((cm.exception.retryable, cm.exception.auth), expected, cm.exception)


class DrainTests(_Base):
    def _failing_row(self):
        self.tg.network_error()
        return ledger.deliver(self.ch, "report", kind="notification", run=self.run)

    def test_a_due_retry_is_sent_by_the_drain(self):
        row = self._failing_row()
        self.assertEqual(ledger.drain_due(), {"sent": 0, "retrying": 0, "failed": 0})  # not due yet
        later = timezone.now() + timedelta(minutes=2)
        self.assertEqual(ledger.drain_due(later), {"sent": 1, "retrying": 0, "failed": 0})
        row.refresh_from_db()
        self.assertEqual((row.status, row.attempts, row.error, row.provider_message_id), ("sent", 2, "", "101"))
        self.assertEqual(len(self.tg.sends), 2)

    def test_backoff_then_failed_after_max_attempts(self):
        row = self._failing_row()
        delays = []
        for _ in range(ledger.MAX_ATTEMPTS - 1):
            row.refresh_from_db()
            now = row.next_attempt_at
            self.tg.network_error()
            ledger.drain_due(now)
            row.refresh_from_db()
            if row.next_attempt_at:
                delays.append(round((row.next_attempt_at - now).total_seconds()))
        self.assertEqual(delays, [300, 900, 3600])
        self.assertEqual((row.status, row.attempts), ("failed", ledger.MAX_ATTEMPTS))
        self.assertEqual(len(self.tg.sends), ledger.MAX_ATTEMPTS)

    def test_a_send_whose_process_died_is_picked_up_after_its_lease(self):
        row = ChannelMessage.objects.create(
            channel=self.ch, direction="out", kind="notification", text="stuck", status="pending",
            attempts=1, next_attempt_at=timezone.now() + ledger.LEASE,
        )
        self.assertEqual(ledger.drain_due()["sent"], 0)  # still leased: someone may be sending it
        self.assertEqual(ledger.drain_due(timezone.now() + ledger.LEASE + timedelta(seconds=1))["sent"], 1)
        row.refresh_from_db()
        self.assertEqual((row.status, row.attempts), ("sent", 2))

    def test_a_row_claimed_by_another_worker_is_not_sent_twice(self):
        row = self._failing_row()
        later = timezone.now() + timedelta(minutes=2)
        stale = ChannelMessage.objects.get(pk=row.pk)  # what this worker read
        # Another worker claimed it in the meantime.
        ChannelMessage.objects.filter(pk=row.pk).update(
            status="pending", attempts=2, next_attempt_at=later + ledger.LEASE)
        with mock.patch.object(ChannelMessage.objects, "select_related") as select:
            select.return_value.filter.return_value.first.return_value = stale
            self.assertIsNone(ledger._retry(stale.pk, later))
        self.assertEqual(len(self.tg.sends), 1)
        row.refresh_from_db()
        self.assertEqual((row.status, row.attempts), ("pending", 2))

    def test_a_disabled_channel_fails_its_retries(self):
        row = self._failing_row()
        Channel.objects.filter(pk=self.ch.pk).update(enabled=False)
        self.assertEqual(ledger.drain_due(timezone.now() + timedelta(minutes=2))["failed"], 1)
        row.refresh_from_db()
        self.assertEqual(row.status, "failed")
        self.assertIn("disabled", row.error)

    def test_the_heartbeat_drains_and_never_fails_over_it(self):
        with mock.patch.object(ledger, "drain_due", return_value={"sent": 1, "retrying": 0, "failed": 0}) as drain:
            self.assertTrue(worker_heartbeat_task()["success"])
        drain.assert_called_once()
        with mock.patch.object(ledger, "drain_due", side_effect=RuntimeError("db gone")):
            self.assertTrue(worker_heartbeat_task()["success"])


class CapTests(_Base):
    def test_notifications_do_not_use_the_reply_cap(self):
        self.ch.daily_reply_cap = 1
        self.ch.save()
        for _ in range(3):
            ledger.deliver(self.ch, "n", kind="notification", run=self.run)
        self.assertEqual(self.ch.replies_today(), 0)
        self.assertFalse(self.ch.daily_cap_reached())
        ledger.deliver(self.ch, "r", kind="reply", reply_ref={"chat_id": 7})
        self.assertEqual(self.ch.replies_today(), 1)
        self.assertTrue(self.ch.daily_cap_reached())

    def test_a_failing_schedule_cannot_send_past_the_daily_cap(self):
        self.ch.daily_send_cap = 3
        self.ch.save()
        self.script.notify_channels.add(self.ch)
        outcomes = [NotificationService._send_channel_notifications(self.run) for _ in range(5)]
        self.assertEqual([o["sent"] for o in outcomes], [1, 1, 1, 0, 0])
        self.assertEqual(len(self.tg.sends), 3)
        capped = self._out(status="failed")
        self.assertEqual(capped.count(), 2)
        self.assertIn("Daily send cap reached", capped.first().error)
        self.assertEqual(capped.first().attempts, 0)
        self.assertEqual(outcomes[-1]["errors"][0]["channel"], "Ops")

    def test_the_send_cap_covers_every_kind_but_pyrun_notices(self):
        self.ch.daily_send_cap = 3
        self.ch.save()
        ledger.deliver(self.ch, "n", kind="notification")
        ledger.deliver(self.ch, "s", kind="script")
        ledger.deliver(self.ch, "notice", kind="system", reply_ref={"chat_id": 9})
        ledger.deliver(self.ch, "r", kind="reply", reply_ref={"chat_id": 7})
        self.assertEqual(self.ch.sends_today(), 3)
        self.assertTrue(self.ch.send_cap_reached())
        self.assertEqual(ledger.deliver(self.ch, "s2", kind="script").status, "failed")
        self.assertEqual(ledger.deliver(self.ch, "notice2", kind="system", reply_ref={"chat_id": 8}).status, "sent")

    def test_a_retry_over_the_cap_fails_instead_of_sending(self):
        self.ch.daily_send_cap = 1
        self.ch.save()
        self.tg.network_error()
        row = ledger.deliver(self.ch, "a", kind="script")
        ledger.deliver(self.ch, "b", kind="script")  # sent: uses the cap up
        ledger.drain_due(timezone.now() + timedelta(minutes=2))
        row.refresh_from_db()
        self.assertEqual(row.status, "failed")
        self.assertIn("Daily send cap reached", row.error)
        self.assertEqual(len(self.tg.sends), 2)

    def test_yesterday_does_not_count(self):
        self.ch.daily_send_cap = 1
        self.ch.save()
        ledger.deliver(self.ch, "old", kind="script")
        self._out().update(created_at=Channel.day_start() - timedelta(minutes=1))
        self.assertFalse(self.ch.send_cap_reached())


class InboundTests(_Base):
    def setUp(self):
        super().setUp()
        self.ch.inbound_enabled = True
        self.ch.inbound_handler = "script"
        self.ch.inbound_target_id = self.script.id
        self.ch.save()

    def test_a_sync_reply_goes_through_the_ledger(self):
        self.script.is_enabled = False
        self.script.save()  # the handler answers "unavailable" synchronously
        dispatch_inbound(str(self.ch.id), _UPDATE)
        row = self._out().get()
        self.assertEqual((row.kind, row.status, row.reply_ref_json), ("reply", "sent", {"chat_id": 7}))
        self.assertEqual(self.tg.sends[0]["chat_id"], 7)
        self.assertEqual(ChannelMessage.objects.get(direction="in").status, "received")

    def test_over_the_send_cap_the_handler_does_not_run_and_the_notice_goes_once(self):
        self.ch.daily_send_cap = 1
        self.ch.save()
        ledger.deliver(self.ch, "report", kind="notification")
        with mock.patch("core.tasks.queue_script_run") as queued:
            dispatch_inbound(str(self.ch.id), _UPDATE)
            dispatch_inbound(str(self.ch.id), dict(_UPDATE, update_id=11))
        queued.assert_not_called()
        notices = self._out(kind="system")
        self.assertEqual(notices.count(), 1)
        self.assertEqual((notices.get().text, notices.get().status), (CAP_NOTICE, "sent"))

    def test_py_ai_history_leaves_notifications_out(self):
        from core.services.channels.handlers import _channel_history
        from core.services.channels.base import InboundMessage

        ChannelMessage.objects.create(channel=self.ch, direction="in", text="hi", reply_ref_json={"chat_id": 7})
        ledger.deliver(self.ch, "nightly FAILED", kind="notification", run=self.run)
        ledger.deliver(self.ch, "hello!", kind="reply", reply_ref={"chat_id": 7})
        msg = InboundMessage(channel_id=str(self.ch.id), provider="telegram", text="now?",
                             sender={"id": "7"}, reply_ref={"chat_id": 7}, raw={})
        self.assertEqual([h["text"] for h in _channel_history(self.ch, msg)], ["hi", "hello!"])

    def test_webhook_notices_are_ledger_rows_outside_the_caps(self):
        self.ch.daily_send_cap = 1
        self.ch.ensure_inbound_token()
        secret = self.ch.ensure_inbound_secret()
        self.ch.save()
        ledger.deliver(self.ch, "report", kind="notification")  # the cap is used up
        resp = self.client.post(
            reverse("channel_webhook", args=[self.ch.inbound_token]), data=json.dumps(_UPDATE),
            content_type="application/json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=secret,
        )
        self.assertEqual(resp.status_code, 200)
        notice = self._out(kind="system").get()
        self.assertEqual(notice.status, "sent")
        self.assertIn("not approved", notice.text)


class InternalSendTests(_Base):
    def setUp(self):
        super().setUp()
        self.token = mint_datastore_token(self.run.id)

    def _post(self, body):
        return self.client.post(
            reverse("internal:channels_send"), data=json.dumps(body), content_type="application/json",
            REMOTE_ADDR="127.0.0.1", HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )

    def test_sent(self):
        resp = self._post({"target": "Ops", "text": "hi"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        row = self._out().get()
        self.assertEqual(body, {"ok": True, "status": "sent", "id": str(row.id),
                                "result": {"message_id": 101, "chat_id": "555"}})
        self.assertEqual((row.kind, row.run_id), ("script", self.run.id))

    def test_retrying_is_202_and_not_an_error(self):
        self.tg.network_error()
        resp = self._post({"target": "Ops", "text": "hi", "reply_ref": {"chat_id": 7}})
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertEqual((body["ok"], body["status"]), (True, "retrying"))
        self.assertTrue(body["next_attempt_at"])
        self.assertEqual(self._out().get().kind, "reply")

    def test_cap_is_429_and_rejected_is_502(self):
        self.ch.daily_send_cap = 1
        self.ch.save()
        self.tg.error(401, "Unauthorized")
        resp = self._post({"target": "Ops", "text": "a"})
        self.assertEqual((resp.status_code, resp.json()["error"]["code"]), (502, "SEND_FAILED"))
        self._post({"target": "Ops", "text": "b"})  # sent
        resp = self._post({"target": "Ops", "text": "c"})
        self.assertEqual((resp.status_code, resp.json()["error"]["code"]), (429, "SEND_CAP_REACHED"))
        self.assertEqual(len(self.tg.sends), 2)


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class PageTests(_Base):
    def setUp(self):
        super().setUp()
        user = User.objects.create(email="owner@example.com")
        WorkspaceMembership.ensure(user, self.ws, role=WorkspaceMembership.ROLE_OWNER)
        self.client.force_login(user)

    def test_run_page_shows_deliveries_with_secrets_masked(self):
        self.tg.error(400, "Bad Request: chat not found")
        ledger.deliver(self.ch, "token is s3cr3t-value", kind="script", run=self.run)
        with mock.patch("core.executor.mask_set_for_display", return_value={"API_KEY": "s3cr3t-value"}):
            body = self.client.get(reverse("cpanel:run_detail", args=[self.run.pk])).content.decode()
        self.assertIn("Deliveries", body)
        self.assertIn("Script send", body)
        self.assertIn("chat not found", body)
        self.assertNotIn("s3cr3t-value", body)

    def test_run_page_withholds_text_when_a_secret_cannot_be_resolved(self):
        ledger.deliver(self.ch, "maybe secret", kind="script", run=self.run)
        with mock.patch("core.executor.mask_set_for_display", side_effect=SecretResolutionError("vault down")):
            body = self.client.get(reverse("cpanel:run_detail", args=[self.run.pk])).content.decode()
        self.assertIn("Text hidden", body)
        self.assertNotIn("maybe secret", body)

    def test_run_page_without_deliveries_has_no_strip(self):
        body = self.client.get(reverse("cpanel:run_detail", args=[self.run.pk])).content.decode()
        self.assertNotIn("run-deliveries-heading", body)

    def test_channel_page_message_log(self):
        ChannelMessage.objects.create(channel=self.ch, direction="in", text="<b>hi</b>",
                                      sender_json={"display_name": "Ann"}, status="received")
        self.tg.network_error()
        ledger.deliver(self.ch, "nightly FAILED", kind="notification", run=self.run)
        body = self.client.get(reverse("cpanel:channel_edit", args=[self.ch.pk])).content.decode()
        self.assertIn("Message log", body)
        self.assertIn("&lt;b&gt;hi&lt;/b&gt;", body)  # inbound text is escaped
        self.assertIn("from Ann", body)
        self.assertIn("retrying", body)
        self.assertIn("Next attempt at", body)
        self.assertIn(reverse("cpanel:run_detail", args=[self.run.pk]), body)

    def test_the_form_saves_the_send_cap(self):
        resp = self.client.post(reverse("cpanel:channel_edit", args=[self.ch.pk]), {
            "name": "Ops", "default_target": "555", "daily_send_cap": "40", "enabled": "on",
        })
        self.assertEqual(resp.status_code, 302)
        self.ch.refresh_from_db()
        self.assertEqual(self.ch.daily_send_cap, 40)
        body = self.client.get(reverse("cpanel:channel_edit", args=[self.ch.pk])).content.decode()
        self.assertIn('name="daily_send_cap" value="40"', body)


class WebhookNotifierTests(TestCase):
    def setUp(self):
        ws = Workspace.get_default()
        env = Environment.objects.create(name="e", path="p")
        script = Script.objects.create(
            name="s", code="x", environment=env, workspace=ws, notify_on=Script.NotifyOn.BOTH,
            notify_webhook_enabled=True, notify_webhook_url="https://hooks.example.com/x",
        )
        self.run = Run.objects.create(script=script, workspace=ws, status=Run.Status.SUCCESS)
        p = mock.patch.object(NotificationService, "_webhook_url_problem", return_value="")
        p.start()
        self.addCleanup(p.stop)

    def _notify(self, status):
        with mock.patch("core.services.notification_service.requests.post",
                        return_value=mock.Mock(status_code=status)):
            return NotificationService.send_notification(self.run)

    def test_a_2xx_is_sent_with_its_status(self):
        results = self._notify(204)
        self.assertEqual((results["webhook_sent"], results["webhook_status"], results["webhook_error"]),
                         (True, 204, None))

    def test_a_non_2xx_is_a_failure_with_its_status(self):
        results = self._notify(500)
        self.assertEqual((results["webhook_sent"], results["webhook_status"]), (False, 500))
        self.assertIn("HTTP 500", results["webhook_error"])


class RetentionAndMigrationTests(_Base):
    def test_rows_older_than_30_days_are_purged_daily(self):
        for days in (31, 29):
            row = ledger.deliver(self.ch, f"{days}", kind="script")
            ChannelMessage.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(days=days))
        result = daily_housekeeping_task()
        self.assertEqual(result["channel_messages_deleted"], 1)
        self.assertTrue(result["success"])
        self.assertEqual(list(ChannelMessage.objects.values_list("text", flat=True)), ["29"])

    def test_one_purge_failing_does_not_stop_the_other(self):
        with mock.patch("core.services.api_audit.purge", side_effect=RuntimeError("x")):
            result = daily_housekeeping_task()
        self.assertFalse(result["success"])
        self.assertEqual(result["channel_messages_deleted"], 0)

    def test_the_migration_maps_old_rows(self):
        migration = importlib.import_module("core.migrations.0089_channel_message_ledger")
        old = [
            ("in", "ok", {"chat_id": 7}),
            ("out", "ok", {"chat_id": 7}),
            ("out", "ok", {}),
            ("out", "error", {}),
            ("out", "rejected", {"chat_id": 7}),
        ]
        for i, (direction, status, ref) in enumerate(old):
            ChannelMessage.objects.create(channel=self.ch, direction=direction, text=str(i),
                                          status=status, reply_ref_json=ref)
        migration.map_existing_rows(apps, None)
        got = {m.text: (m.status, m.kind) for m in ChannelMessage.objects.all()}
        self.assertEqual(got, {
            "0": ("received", ""),
            "1": ("sent", "reply"),
            "2": ("sent", "script"),
            "3": ("failed", "script"),
            "4": ("sent", "system"),
        })
