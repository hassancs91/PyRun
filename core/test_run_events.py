"""
One event envelope for every trigger (2.5 Stage 0, ticket T03).

What this pins, roughly in order of how much it would hurt to get wrong:

- Scripts using WEBHOOK_* / INBOUND_* behave byte-for-byte as before: the
  task still gets the same webhook data (a body over the event cap included)
  and a real subprocess sees the same variables.
- Every trigger kind (manual, re-run, schedule, webhook, chat channel, plugin
  SDK) stamps ``Run.event`` with ``kind`` + ``received_at`` + its own keys,
  and ``queue_script_run`` fills in a minimal one for a caller that didn't.
- An event over the cap is cut with an explicit flag, never fails the run, and
  anything JSON or PostgreSQL can't hold (NUL, lone surrogates, NaN, nesting
  too deep) is stored on both engines instead of failing the INSERT.
- A real script reads it with ``pyrunner_run.event()``; the run page shows an
  Event card (secrets masked, hidden when they can't be checked) and a
  download; backups round-trip it.
"""

import json
import sys
import uuid
from datetime import datetime
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.executor import execute_run, run_file_payload
from core.models import (
    Channel,
    Environment,
    Run,
    Script,
    ScriptSchedule,
    Workspace,
    WorkspaceMembership,
)
from core.plugins.api import ScriptAPI
from core.services.backup_service import BackupService
from core.services import EncryptionService
from core.services.run_events import (
    EVENT_MAX_BYTES,
    EVENT_PREVIEW_CHARS,
    build_event,
    cap_event,
    default_event,
    webhook_event,
)
from core.services.run_reports import json_size
from core.services.secret_backends import SecretResolutionError
from core.tasks import execute_scheduled_run, queue_script_run

User = get_user_model()

SECRETS = {"API_KEY": "abc123xyz"}
# Prints what a webhook / chat script reads today (its WEBHOOK_* and INBOUND_*
# variables) next to what pyrunner_run.event() returns.
PRINT_EVENT = (
    "import json, os, pyrunner_run\n"
    "env = {k: v for k, v in os.environ.items() if k.startswith(('WEBHOOK_', 'INBOUND_'))}\n"
    "print(json.dumps({'env': env, 'event': pyrunner_run.event()}))\n"
)
_TG_UPDATE = {
    "update_id": 10,
    "message": {
        "message_id": 1,
        "from": {"id": 7, "username": "ann", "first_name": "Ann"},
        "chat": {"id": 7, "type": "private"},
        "text": "hello bot",
    },
}


def _setup_wizard_off(test):
    for target in (
        "core.services.setup_service.SetupService.is_setup_needed",
        "core.services.setup_service.SetupService.needs_admin_setup",
    ):
        p = mock.patch(target, return_value=False)
        p.start()
        test.addCleanup(p.stop)


def _ai_off():
    return mock.patch("core.executor.ClaudeService.get_script_env", return_value={})


def _queued():
    return mock.patch("core.tasks.async_task", return_value="t")


class _Fixture:
    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="eenv", path=f"eenv{uuid.uuid4().hex[:6]}")

    def _script(self, **kwargs):
        kwargs.setdefault("code", "pass")
        return Script.objects.create(
            name=f"s{uuid.uuid4().hex[:6]}", environment=self.env, workspace=self.ws, **kwargs
        )

    def _run(self, script=None, status=Run.Status.PENDING, **kwargs):
        return Run.objects.create(
            script=script or self._script(), workspace=self.ws, status=status, **kwargs
        )

    def _webhook_script(self, **kwargs):
        script = self._script(webhook_token="tok_" + uuid.uuid4().hex, **kwargs)
        return script

    def _call_webhook(self, script, method="post", path_suffix="", **kwargs):
        url = reverse("webhook_trigger", args=[script.webhook_token]) + path_suffix
        with _queued() as task:
            resp = getattr(self.client, method)(url, **kwargs)
        self.assertEqual(resp.status_code, 200, resp.content)
        return Run.objects.get(script=script), task.call_args.args[2]


def _execute(test, run, webhook_data=None):
    """Run ``run`` in a real subprocess; return what PRINT_EVENT printed."""
    with mock.patch("core.executor._validate_environment", return_value=sys.executable), _ai_off():
        execute_run(run, webhook_data=webhook_data)
    run.refresh_from_db()
    test.assertEqual(run.status, Run.Status.SUCCESS, run.stderr)
    return json.loads(run.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------------- #
# WEBHOOK_* / INBOUND_* are unchanged
# --------------------------------------------------------------------------- #


class WebhookEnvRegressionTests(_Fixture, TestCase):
    """The variables pre-2.5 webhook scripts read, pinned value for value."""

    def setUp(self):
        super().setUp()
        _setup_wizard_off(self)

    def test_a_json_post_gives_the_same_variables_and_the_same_task_data(self):
        script = self._webhook_script(code=PRINT_EVENT)
        body = '{"hello": "wörld", "n": [1, 2]}'
        run, webhook_data = self._call_webhook(
            script, path_suffix="?a=1&a=2&b=x", data=body, content_type="application/json"
        )
        self.assertEqual(webhook_data, {
            "method": "POST",
            "query": {"a": ["1", "2"], "b": ["x"]},
            "content_type": "application/json",
            "body": body,
            "body_json": {"hello": "wörld", "n": [1, 2]},
        })
        seen = _execute(self, run, webhook_data)
        self.assertEqual(seen["env"], {
            "WEBHOOK_METHOD": "POST",
            "WEBHOOK_QUERY": '{"a": ["1", "2"], "b": ["x"]}',
            "WEBHOOK_CONTENT_TYPE": "application/json",
            "WEBHOOK_BODY": body,
            "WEBHOOK_BODY_JSON": '{"hello": "w\\u00f6rld", "n": [1, 2]}',
        })
        self.assertEqual(seen["event"], run.event)

    def test_a_get_has_no_body_variables(self):
        script = self._webhook_script(code=PRINT_EVENT)
        run, webhook_data = self._call_webhook(script, method="get", path_suffix="?q=1")
        self.assertEqual(webhook_data, {"method": "GET", "query": {"q": ["1"]}, "content_type": ""})
        seen = _execute(self, run, webhook_data)
        self.assertEqual(seen["env"], {
            "WEBHOOK_METHOD": "GET",
            "WEBHOOK_QUERY": '{"q": ["1"]}',
            "WEBHOOK_CONTENT_TYPE": "",
        })

    def test_a_text_post_has_the_raw_body_and_no_json(self):
        script = self._webhook_script(code=PRINT_EVENT)
        run, webhook_data = self._call_webhook(script, data="plain {text}", content_type="text/plain")
        seen = _execute(self, run, webhook_data)
        self.assertEqual(seen["env"]["WEBHOOK_BODY"], "plain {text}")
        self.assertNotIn("WEBHOOK_BODY_JSON", seen["env"])
        self.assertNotIn("body_json", run.event)

    def test_a_body_over_the_event_cap_still_reaches_the_task_whole(self):
        script = self._webhook_script()
        body = json.dumps({"blob": "x" * (EVENT_MAX_BYTES + 10)})
        run, webhook_data = self._call_webhook(script, data=body, content_type="application/json")
        self.assertEqual(webhook_data["body"], body)
        self.assertEqual(webhook_data["body_json"], json.loads(body))
        self.assertTrue(run.event["body_truncated"])
        self.assertLessEqual(json_size(run.event), EVENT_MAX_BYTES)


@override_settings(ENCRYPTION_KEY=Fernet.generate_key().decode())
class ChannelEventTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        EncryptionService.reset()
        self.addCleanup(EncryptionService.reset)
        self.script = self._script(code=PRINT_EVENT, is_enabled=True)
        self.channel = Channel(
            workspace=self.ws, provider="telegram", name="Bot",
            inbound_enabled=True, inbound_handler="script", inbound_target_id=self.script.id,
        )
        self.channel.set_credentials({"bot_token": "1:2"}, identity="1:2")
        self.channel.save()

    def _dispatch(self):
        from core.services.channels.inbound import dispatch_inbound

        with mock.patch("core.tasks.queue_script_run") as queue:
            dispatch_inbound(str(self.channel.id), _TG_UPDATE)
        self.assertEqual(queue.call_count, 1)
        return queue.call_args.args[0], queue.call_args.kwargs["webhook_data"]

    def test_the_event_carries_the_inbound_message(self):
        run, _ = self._dispatch()
        run.refresh_from_db()
        event = run.event
        self.assertEqual(event["kind"], "channel")
        self.assertIsInstance(datetime.fromisoformat(event["received_at"]), datetime)
        self.assertEqual(event["channel"], "Bot")
        self.assertEqual(event["channel_id"], self.channel.id.hex)
        self.assertEqual(event["provider"], "telegram")
        self.assertEqual(event["text"], "hello bot")
        self.assertEqual(event["reply_ref"], {"chat_id": 7})
        self.assertEqual(event["sender"]["username"], "ann")
        self.assertEqual(event["raw"], _TG_UPDATE)

    def test_inbound_variables_are_unchanged_and_the_script_reads_the_event(self):
        run, webhook_data = self._dispatch()
        seen = _execute(self, Run.objects.get(pk=run.pk), webhook_data)
        sender = webhook_data["inbound"]["sender"]
        self.assertEqual(seen["env"], {
            "INBOUND_CHANNEL": "Bot",
            "INBOUND_TEXT": "hello bot",
            "INBOUND_REPLY_REF": '{"chat_id": 7}',
            "INBOUND_SENDER": json.dumps(sender),
            "WEBHOOK_METHOD": "INBOUND",
            "WEBHOOK_QUERY": "{}",
            "WEBHOOK_CONTENT_TYPE": "",
        })
        self.assertEqual(seen["event"]["text"], "hello bot")
        self.assertEqual(seen["event"]["sender"], sender)


# --------------------------------------------------------------------------- #
# Every trigger stamps its event
# --------------------------------------------------------------------------- #


class TriggerEventTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        _setup_wizard_off(self)
        self.user = User.objects.create(email="events@example.com")
        WorkspaceMembership.objects.update_or_create(
            user=self.user, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_OWNER}
        )

    def _assert_common(self, event, kind):
        self.assertEqual(event["kind"], kind)
        self.assertIsInstance(datetime.fromisoformat(event["received_at"]), datetime)

    def test_webhook(self):
        script = self._webhook_script()
        run, _ = self._call_webhook(
            script, path_suffix="?src=w", data='{"a": 1}', content_type="application/json"
        )
        self._assert_common(run.event, "webhook")
        self.assertEqual(run.event["method"], "POST")
        self.assertEqual(run.event["query"], {"src": ["w"]})
        self.assertEqual(run.event["content_type"], "application/json")
        self.assertEqual(run.event["body"], '{"a": 1}')
        self.assertEqual(run.event["body_json"], {"a": 1})
        self.assertNotIn("truncated", run.event)

    def test_schedule(self):
        script = self._script()
        schedule = ScriptSchedule.objects.create(
            script=script, workspace=self.ws, run_mode="interval", interval_minutes=60, is_active=True
        )
        with _queued():
            self.assertTrue(execute_scheduled_run(str(script.id))["success"])
        run = Run.objects.get(script=script)
        self._assert_common(run.event, "schedule")
        self.assertEqual(run.event["schedule_id"], schedule.id.hex)
        self.assertEqual(run.event["run_mode"], "interval")
        self.assertEqual(run.event["fired_at"], run.event["received_at"])

    def test_manual_run_now_and_rerun(self):
        script = self._script()
        self.client.force_login(self.user)
        with _queued():
            self.client.post(reverse("cpanel:script_run", args=[script.pk]))
        first = Run.objects.get(script=script)
        self._assert_common(first.event, "manual")
        self.assertEqual(first.event["user"], {"id": self.user.pk, "email": self.user.email})
        self.assertNotIn("rerun_of", first.event)

        with _queued():
            self.client.post(reverse("cpanel:run_rerun", args=[first.pk]))
        rerun = Run.objects.exclude(pk=first.pk).get(script=script)
        self._assert_common(rerun.event, "manual")
        self.assertEqual(rerun.event["rerun_of"], first.pk.hex)

    def test_plugin_sdk(self):
        api = ScriptAPI("evplugin")
        api.upsert(key="job", name=f"job{uuid.uuid4().hex[:6]}", code="pass", environment=self.env)
        with _queued():
            run = api.queue_run("job", triggered_by=self.user)
        run.refresh_from_db()
        self._assert_common(run.event, "plugin")
        self.assertEqual(run.event["plugin"], "evplugin")
        self.assertEqual(run.event["script_key"], "job")
        self.assertEqual(run.event["user"]["email"], self.user.email)

    def test_queue_fills_in_a_minimal_event_for_a_run_created_without_one(self):
        for trigger, kind in (("manual", "manual"), ("scheduled", "schedule"), ("api", "api")):
            run = self._run(trigger_type=trigger)
            with _queued():
                queue_script_run(run)
            run.refresh_from_db()
            self.assertEqual(run.event, {"kind": kind, "received_at": run.created_at.isoformat()})

    def test_queue_keeps_an_event_already_set(self):
        run = self._run(event={"kind": "webhook", "received_at": "2026-11-08T10:00:00+00:00"})
        with _queued():
            queue_script_run(run)
        run.refresh_from_db()
        self.assertEqual(run.event["kind"], "webhook")


# --------------------------------------------------------------------------- #
# The cap, and data either engine can't store
# --------------------------------------------------------------------------- #


class CapTests(_Fixture, TestCase):
    def _webhook(self, body, body_json=None):
        data = {"method": "POST", "query": {}, "content_type": "application/json", "body": body}
        if body_json is not None:
            data["body_json"] = body_json
        return webhook_event(data)

    def test_a_small_event_is_kept_as_is(self):
        event = self._webhook('{"a": 1}', {"a": 1})
        self.assertEqual(event["body"], '{"a": 1}')
        self.assertEqual(event["body_json"], {"a": 1})
        self.assertNotIn("truncated", event)
        self.assertNotIn("body_truncated", event)

    def test_a_body_over_the_cap_is_cut_and_flagged(self):
        body = "a" * (EVENT_MAX_BYTES * 2)
        event = self._webhook(body, {"x": body})
        self.assertTrue(event["truncated"])
        self.assertTrue(event["body_truncated"])
        self.assertNotIn("body_json", event)
        self.assertTrue(body.startswith(event["body"]))
        self.assertLessEqual(json_size(event), EVENT_MAX_BYTES)
        self.assertGreater(json_size(event), EVENT_MAX_BYTES - 64)  # cut to fit, not emptied
        self.assertEqual((event["kind"], event["method"]), ("webhook", "POST"))

    def test_multibyte_and_escaped_bodies_are_cut_at_a_character_and_fit(self):
        for unit in ("é", "日本", "😀", '"\\', "\x01\n"):
            body = unit * (EVENT_MAX_BYTES // len(unit.encode("utf-8")) + 50)
            event = self._webhook(body)
            self.assertTrue(event["body_truncated"], repr(unit))
            self.assertTrue(body.startswith(event["body"]), repr(unit))
            self.assertLessEqual(json_size(event), EVENT_MAX_BYTES, repr(unit))

    def test_when_only_the_parsed_copy_pushes_it_over_the_body_stays_whole(self):
        body = json.dumps({"x": "y" * (EVENT_MAX_BYTES // 2 + 1000)})
        event = self._webhook(body, json.loads(body))
        self.assertTrue(event["truncated"])
        self.assertNotIn("body_json", event)
        self.assertNotIn("body_truncated", event)
        self.assertEqual(event["body"], body)

    def test_an_event_with_nothing_to_cut_keeps_only_its_head(self):
        event = build_event("channel", None, text="t" * (EVENT_MAX_BYTES + 1))
        self.assertEqual(set(event), {"kind", "received_at", "truncated"})
        self.assertEqual(event["kind"], "channel")

    def test_json_nested_too_deep_is_left_out_not_raised(self):
        deep = []
        for _ in range(100_000):
            deep = [deep]
        with self.assertLogs("core.services.run_events", "WARNING"):
            event = self._webhook("[[[...]]]", deep)
        # Not a size cut: the body stays, as text only (2.5 test pass, F1).
        self.assertNotIn("truncated", event)
        self.assertNotIn("body_json", event)
        self.assertEqual(event["body"], "[[[...]]]")

    def test_values_neither_engine_can_hold_are_replaced_and_stored(self):
        event = self._webhook(
            "nul\x00 here", {"nan": float("nan"), "inf": float("-inf"), "s": "lone \ud800", "k\x00": 1}
        )
        self.assertEqual(event["body"], "nul� here")
        self.assertEqual(event["body_json"], {"nan": None, "inf": None, "s": "lone ?", "k�": 1})
        run = self._run(event=event)
        run.refresh_from_db()
        self.assertEqual(run.event, event)

    def test_cap_event_does_not_change_its_argument(self):
        body_json = {"x": "y" * EVENT_MAX_BYTES}
        data = {"kind": "webhook", "received_at": "t", "body": "b", "body_json": body_json}
        cap_event(data)
        self.assertIs(data["body_json"], body_json)
        self.assertEqual(set(data), {"kind", "received_at", "body", "body_json"})

    def test_a_truncated_event_still_runs(self):
        script = self._script(code=(
            "import json, pyrunner_run\n"
            "ev = pyrunner_run.event()\n"
            "print(json.dumps({'event': {'body_truncated': ev['body_truncated'], 'size': len(ev['body'])}}))\n"
        ))
        event = self._webhook("z" * (EVENT_MAX_BYTES + 5))
        seen = _execute(self, self._run(script, event=event))
        self.assertEqual(seen["event"], {"body_truncated": True, "size": len(event["body"])})


# --------------------------------------------------------------------------- #
# The script side
# --------------------------------------------------------------------------- #


class RunFileEventTests(_Fixture, TestCase):
    def test_the_run_file_carries_the_event(self):
        event = build_event("manual", None, user={"id": 1, "email": "a@b.c"})
        run = self._run(event=event)
        self.assertEqual(run_file_payload(run)["event"], event)

    def test_a_run_from_before_2_5_gets_the_minimal_event_for_its_trigger(self):
        run = self._run(trigger_type=Run.TriggerType.SCHEDULED)
        self.assertIsNone(run.event)
        self.assertEqual(run_file_payload(run)["event"], default_event(run))
        self.assertEqual(run_file_payload(run)["event"]["kind"], "schedule")

    def test_a_scheduled_script_reads_its_event(self):
        script = self._script(code=PRINT_EVENT)
        ScriptSchedule.objects.create(
            script=script, workspace=self.ws, run_mode="interval", interval_minutes=60, is_active=True
        )
        with _queued():
            execute_scheduled_run(str(script.id))
        run = Run.objects.get(script=script)
        seen = _execute(self, run)
        self.assertEqual(seen["event"], run.event)
        self.assertEqual(seen["env"], {})


# --------------------------------------------------------------------------- #
# Console and backups
# --------------------------------------------------------------------------- #


class RunPageTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        _setup_wizard_off(self)
        self.user = User.objects.create(email="eventpage@example.com")
        WorkspaceMembership.objects.update_or_create(
            user=self.user, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_OWNER}
        )
        self.client.force_login(self.user)

    def _secrets(self):
        return mock.patch("core.executor.resolve_secrets_for_run", return_value=dict(SECRETS))

    def _webhook_run(self, body="token abc123xyz", **kwargs):
        event = webhook_event({"method": "POST", "query": {}, "content_type": "text/plain", "body": body})
        return self._run(status=Run.Status.SUCCESS, event=event, **kwargs)

    def test_event_card_with_secrets_masked(self):
        run = self._webhook_run()
        with self._secrets(), _ai_off():
            resp = self.client.get(reverse("cpanel:run_detail", args=[run.pk]))
        self.assertContains(resp, 'id="run-event-heading"')
        self.assertContains(resp, "Webhook")
        self.assertContains(resp, "token [API_KEY:MASKED]")
        self.assertNotContains(resp, "abc123xyz")
        self.assertContains(resp, reverse("cpanel:run_event_download", args=[run.pk]))

    def test_download_is_masked(self):
        run = self._webhook_run()
        with self._secrets(), _ai_off():
            resp = self.client.get(reverse("cpanel:run_event_download", args=[run.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment;", resp["Content-Disposition"])
        self.assertEqual(json.loads(resp.content)["body"], "token [API_KEY:MASKED]")

    def test_hidden_when_a_secret_cant_be_resolved(self):
        run = self._webhook_run()
        with mock.patch(
            "core.executor.resolve_secrets_for_run", side_effect=SecretResolutionError("vault down")
        ):
            resp = self.client.get(reverse("cpanel:run_detail", args=[run.pk]))
            download = self.client.get(reverse("cpanel:run_event_download", args=[run.pk]))
        self.assertContains(resp, 'id="run-event-heading"')
        self.assertContains(resp, "this event can't be checked for secrets")
        self.assertNotContains(resp, "abc123xyz")
        self.assertEqual(download.status_code, 503)
        self.assertNotIn(b"abc123xyz", download.content)

    def test_inputs_and_event_share_one_secret_lookup(self):
        script = self._script(inputs_schema={"type": "object", "properties": {"q": {"type": "string"}}})
        run = self._webhook_run(script=script, inputs={"q": "x"})
        with self._secrets() as resolve, _ai_off():
            self.client.get(reverse("cpanel:run_detail", args=[run.pk]))
        self.assertEqual(resolve.call_count, 1)

    def test_a_big_event_is_previewed_and_flagged(self):
        run = self._webhook_run(body="q" * (EVENT_MAX_BYTES + 100))
        with self._secrets(), _ai_off():
            resp = self.client.get(reverse("cpanel:run_detail", args=[run.pk]))
        self.assertContains(resp, "body_truncated")
        self.assertContains(resp, "the download has all of it")
        self.assertContains(resp, "q" * (EVENT_PREVIEW_CHARS // 2))
        self.assertNotContains(resp, "q" * EVENT_PREVIEW_CHARS)

    def test_a_run_without_an_event_has_no_card(self):
        run = self._run(status=Run.Status.SUCCESS)
        resp = self.client.get(reverse("cpanel:run_detail", args=[run.pk]))
        self.assertNotContains(resp, 'id="run-event-heading"')
        self.assertEqual(
            self.client.get(reverse("cpanel:run_event_download", args=[run.pk])).status_code, 404
        )

    def test_download_is_workspace_scoped(self):
        other_ws = Workspace.objects.create(name="Other events workspace")
        script = Script.objects.create(name="theirs", code="x", environment=self.env, workspace=other_ws)
        run = Run.objects.create(script=script, workspace=other_ws, event={"kind": "manual"})
        self.assertEqual(
            self.client.get(reverse("cpanel:run_event_download", args=[run.pk])).status_code, 404
        )

    def test_runs_list_still_renders(self):
        self._webhook_run()
        resp = self.client.get(reverse("cpanel:run_list"))
        self.assertEqual(resp.status_code, 200)


class BackupTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create(email="admin-events@example.com", is_superuser=True)

    def test_backup_round_trips_the_event(self):
        event = build_event("webhook", None, method="POST", body="hi")
        run = self._run(status=Run.Status.SUCCESS, event=event)
        plain = self._run(status=Run.Status.SUCCESS)
        data = BackupService.create_backup(include_runs=True)
        result = BackupService.restore_backup(data, restore_runs=True, current_user=self.user)
        self.assertTrue(result["success"], result)
        self.assertEqual(Run.objects.get(pk=run.pk).event, event)
        self.assertIsNone(Run.objects.get(pk=plain.pk).event)

    def test_a_backup_without_the_field_restores_it_as_null(self):
        run = self._run(status=Run.Status.SUCCESS, event={"kind": "manual", "received_at": "t"})
        data = BackupService.create_backup(include_runs=True)
        for row in data["runs"]:
            row.pop("event")
        result = BackupService.restore_backup(data, restore_runs=True, current_user=self.user)
        self.assertTrue(result["success"], result)
        self.assertIsNone(Run.objects.get(pk=run.pk).event)
