"""
Run results and progress from ``pyrunner_run`` (2.5 Stage 0, ticket T02).

What this pins, roughly in order of how much it would hurt to get wrong:

- A secret value in a result or progress message is masked BEFORE it is
  stored, with the same set execution masks output with (AI credential
  included); when a secret can't be resolved nothing is stored.
- The loopback endpoints take the run from the signed token, never from the
  body, and refuse writes to a run that isn't running, even one that finishes
  between the check and the write.
- Every run gets PYRUNNER_RUN_FILE (run, script, trigger type, inputs: JSON
  ones too) for manual, scheduled and webhook runs, and its ids with AI off.
- A real script reports a result and progress end to end (live loopback).
- The run page shows the result and live progress; RunView and notifications
  carry them (capped); backups round-trip both fields.

The helper on its own, without Django, is in core/test_pyrunner_run_helper.py.
"""

import json
import os
import sys
import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import LiveServerTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.executor import _build_script_environment, execute_run, run_file_payload
from core.models import (
    Environment,
    Run,
    Script,
    ScriptSchedule,
    Workspace,
    WorkspaceMembership,
)
from core.plugins.api import ScriptAPI
from core.services.backup_service import BackupService
from core.services.datastore_token import mint_datastore_token
from core.services.notification_service import NotificationService
from core.services.run_reports import (
    CHANNEL_RESULT_MAX_CHARS,
    NOTIFY_RESULT_MAX_BYTES,
    PROGRESS_MESSAGE_MAX_CHARS,
    RESULT_MAX_BYTES,
)
from core.services.secret_backends import SecretResolutionError
from core.tasks import execute_scheduled_run

User = get_user_model()

SECRETS = {"API_KEY": "abc123xyz"}
SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string", "default": "Beirut"},
        "options": {"type": "object", "default": {"units": "metric", "days": [1, 2]}},
    },
}
# Prints the run file and the run's ids, so a test can read what a real
# subprocess was given.
PRINT_CONTEXT = (
    "import json, os\n"
    "with open(os.environ['PYRUNNER_RUN_FILE'], encoding='utf-8') as fh:\n"
    "    data = json.load(fh)\n"
    "print(json.dumps({'file': data, 'path': os.environ['PYRUNNER_RUN_FILE'],\n"
    "    'ids': [os.environ.get(k) for k in ('PYRUNNER_RUN_ID', 'PYRUNNER_SCRIPT_ID', 'PYRUNNER_SCRIPT_NAME')]}))\n"
)


def _setup_wizard_off(test):
    for target in (
        "core.services.setup_service.SetupService.is_setup_needed",
        "core.services.setup_service.SetupService.needs_admin_setup",
    ):
        p = mock.patch(target, return_value=False)
        p.start()
        test.addCleanup(p.stop)


def _secrets(value=SECRETS):
    return mock.patch("core.executor.resolve_secrets_for_run", return_value=dict(value))


def _ai_off():
    return mock.patch("core.executor.ClaudeService.get_script_env", return_value={})


class _Fixture:
    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="renv", path=f"renv{uuid.uuid4().hex[:6]}")

    def _script(self, **kwargs):
        kwargs.setdefault("code", "pass")
        return Script.objects.create(
            name=f"s{uuid.uuid4().hex[:6]}", environment=self.env, workspace=self.ws, **kwargs
        )

    def _run(self, script=None, status=Run.Status.RUNNING, **kwargs):
        return Run.objects.create(
            script=script or self._script(), workspace=self.ws, status=status, **kwargs
        )


# --------------------------------------------------------------------------- #
# The loopback endpoints
# --------------------------------------------------------------------------- #


class _EndpointFixture(_Fixture):
    def _post(self, name, body, run=None, token=None):
        token = token if token is not None else mint_datastore_token((run or self.run).id)
        data = body if isinstance(body, str) else json.dumps(body)
        return self.client.post(
            reverse(f"internal:{name}"),
            data=data,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )


class ResultEndpointTests(_EndpointFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.run = self._run()

    def test_the_result_is_stored_with_secret_values_masked(self):
        with _secrets():
            resp = self._post("runs_result", {"result": {
                "note": "token abc123xyz", "abc123xyz": [1, "abc123xyz"], "n": 3,
            }})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.run.refresh_from_db()
        self.assertEqual(self.run.result, {
            "note": "token [API_KEY:MASKED]", "[API_KEY:MASKED]": [1, "[API_KEY:MASKED]"], "n": 3,
        })
        self.assertEqual(resp.json()["bytes"], len(json.dumps(self.run.result, separators=(",", ":"))))

    def test_the_injected_ai_credential_is_masked_too(self):
        with _secrets({}), mock.patch(
            "core.executor.ClaudeService.get_script_env", return_value={"ANTHROPIC_API_KEY": "sk-ant-xyz987"}
        ):
            self._post("runs_result", {"result": {"key": "sk-ant-xyz987"}})
        self.run.refresh_from_db()
        self.assertEqual(self.run.result, {"key": "[ANTHROPIC_API_KEY:MASKED]"})

    def test_nothing_is_stored_when_a_secret_cant_be_resolved(self):
        with mock.patch(
            "core.executor.resolve_secrets_for_run", side_effect=SecretResolutionError("vault down")
        ):
            resp = self._post("runs_result", {"result": {"key": "abc123xyz"}})
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["error"]["code"], "SECRETS_UNAVAILABLE")
        self.run.refresh_from_db()
        self.assertIsNone(self.run.result)

    def test_the_run_comes_from_the_token_not_the_body(self):
        other = self._run()
        with _secrets({}):
            resp = self._post("runs_result", {"result": {"a": 1}, "run_id": str(other.id)})
        self.assertEqual(resp.status_code, 200)
        self.run.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.run.result, {"a": 1})
        self.assertIsNone(other.result)

    def test_a_missing_or_forged_token_is_refused(self):
        self.assertEqual(self._post("runs_result", {"result": {}}, token="").status_code, 401)
        self.assertEqual(self._post("runs_result", {"result": {}}, token="forged").status_code, 401)
        self.run.refresh_from_db()
        self.assertIsNone(self.run.result)

    def test_writes_to_a_run_that_isnt_running_are_refused(self):
        for status in (Run.Status.SUCCESS, Run.Status.FAILED, Run.Status.TIMEOUT,
                       Run.Status.CANCELLED, Run.Status.PENDING):
            run = self._run(status=status, result={"kept": True})
            with _secrets({}):
                resp = self._post("runs_result", {"result": {"late": True}}, run=run)
            self.assertEqual(resp.status_code, 409, status)
            self.assertEqual(resp.json()["error"]["code"], "RUN_NOT_RUNNING")
            run.refresh_from_db()
            self.assertEqual(run.result, {"kept": True}, status)

    def test_a_run_that_finishes_before_the_write_is_not_changed(self):
        # The run was RUNNING when checked and finished before the UPDATE: the
        # conditional write must still refuse.
        Run.objects.filter(pk=self.run.pk).update(status=Run.Status.SUCCESS)
        stale = Run.objects.select_related("script").get(pk=self.run.pk)
        stale.status = Run.Status.RUNNING
        with _secrets({}), mock.patch("core.services.run_reports._running_run", return_value=stale):
            resp = self._post("runs_result", {"result": {"late": True}})
        self.assertEqual(resp.status_code, 409)
        self.run.refresh_from_db()
        self.assertIsNone(self.run.result)

    def test_the_last_result_wins(self):
        with _secrets({}):
            self._post("runs_result", {"result": {"step": 1}})
            self._post("runs_result", {"result": {"step": 2}})
        self.run.refresh_from_db()
        self.assertEqual(self.run.result, {"step": 2})

    def test_bad_bodies_are_refused(self):
        with _secrets({}):
            for body in ('{"result": [1, 2]}', '{"result": "text"}', '{"result": {"x": NaN}}',
                         "not json", "[]", "{}"):
                resp = self._post("runs_result", body)
                self.assertEqual(resp.status_code, 400, body)
                self.assertEqual(resp.json()["error"]["code"], "BAD_REQUEST", body)
        self.run.refresh_from_db()
        self.assertIsNone(self.run.result)

    def test_a_result_over_the_cap_is_refused(self):
        with _secrets({}):
            resp = self._post("runs_result", {"result": {"x": "a" * RESULT_MAX_BYTES}})
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(resp.json()["error"]["code"], "RESULT_TOO_LARGE")
        self.run.refresh_from_db()
        self.assertIsNone(self.run.result)

    def test_only_post(self):
        resp = self.client.get(
            reverse("internal:runs_result"), HTTP_AUTHORIZATION=f"Bearer {mint_datastore_token(self.run.id)}"
        )
        self.assertEqual(resp.status_code, 405)

    @override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=64)
    def test_an_oversized_request_is_refused_as_such(self):
        with _secrets({}):
            resp = self._post("runs_progress", {"message": "x" * 100})
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(resp.json()["error"]["code"], "REQUEST_TOO_LARGE")

    def test_the_helper_checks_the_same_limits(self):
        # The helper is stdlib-only, so it can't import these; keep them equal.
        from django.conf import settings

        sys.path.insert(0, str(settings.BASE_DIR / "core" / "script_helpers"))
        import pyrunner_run

        self.assertEqual(pyrunner_run.RESULT_MAX_BYTES, RESULT_MAX_BYTES)
        self.assertEqual(pyrunner_run.PROGRESS_MESSAGE_MAX_CHARS, PROGRESS_MESSAGE_MAX_CHARS)


class ProgressEndpointTests(_EndpointFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.run = self._run()

    def test_updates_merge_and_the_message_is_masked(self):
        with _secrets():
            first = self._post("runs_progress", {"pct": 10, "message": "using abc123xyz"})
            self._post("runs_progress", {"pct": 55.5})
        self.assertEqual(first.status_code, 200, first.content)
        self.run.refresh_from_db()
        self.assertEqual(self.run.progress["pct"], 55.5)
        self.assertEqual(self.run.progress["message"], "using [API_KEY:MASKED]")
        self.assertTrue(self.run.progress["updated_at"])
        with _secrets():
            self._post("runs_progress", {"message": ""})
        self.run.refresh_from_db()
        self.assertEqual((self.run.progress["pct"], self.run.progress["message"]), (55.5, ""))

    def test_bad_updates_are_refused(self):
        too_long = "x" * (PROGRESS_MESSAGE_MAX_CHARS + 1)
        with _secrets({}):
            for body in ({}, {"pct": 101}, {"pct": -1}, {"pct": True}, {"pct": "5"},
                         {"message": 5}, {"message": too_long}):
                resp = self._post("runs_progress", body)
                self.assertEqual(resp.status_code, 400, body)
        self.run.refresh_from_db()
        self.assertIsNone(self.run.progress)

    def test_a_finished_run_is_refused(self):
        run = self._run(status=Run.Status.SUCCESS)
        with _secrets({}):
            resp = self._post("runs_progress", {"pct": 50}, run=run)
        self.assertEqual(resp.status_code, 409)
        run.refresh_from_db()
        self.assertIsNone(run.progress)


# --------------------------------------------------------------------------- #
# What every run gets: run.json and its ids
# --------------------------------------------------------------------------- #


class RunEnvironmentTests(_Fixture, TestCase):
    def test_ids_are_set_with_ai_off_and_a_secret_cant_spoof_them(self):
        run = self._run(status=Run.Status.PENDING)
        with _ai_off():
            env = _build_script_environment(
                run=run, secrets={"PYRUNNER_RUN_ID": "spoof"}, run_file="/tmp/run.json"
            )
        self.assertEqual(env["PYRUNNER_RUN_ID"], run.id.hex)
        self.assertEqual(env["PYRUNNER_SCRIPT_ID"], run.script.id.hex)
        self.assertEqual(env["PYRUNNER_SCRIPT_NAME"], run.script.name)
        self.assertEqual(env["PYRUNNER_RUN_FILE"], "/tmp/run.json")

    def test_payload(self):
        script = self._script(inputs_schema=SCHEMA)
        run = self._run(script, status=Run.Status.PENDING, inputs={"city": "Paris", "options": {"a": 1}})
        payload = run_file_payload(run)
        self.assertEqual(payload["run"]["id"], run.id.hex)
        self.assertEqual(payload["script"], {"id": script.id.hex, "name": script.name})
        self.assertEqual(payload["trigger"], {"type": "manual"})
        self.assertEqual(payload["inputs"], {"city": "Paris", "options": {"a": 1}})
        json.dumps(payload)

    def test_a_run_without_inputs_gets_an_empty_object(self):
        self.assertEqual(run_file_payload(self._run(status=Run.Status.PENDING))["inputs"], {})


@mock.patch("core.executor._validate_environment", return_value=sys.executable)
class RunFileTriggerTests(_Fixture, TestCase):
    """A real subprocess reads its run file for each trigger kind."""

    def setUp(self):
        super().setUp()
        _setup_wizard_off(self)
        self.script = self._script(code=PRINT_CONTEXT, inputs_schema=SCHEMA)

    def _execute(self, run, webhook_data=None):
        with _ai_off():
            execute_run(run, webhook_data=webhook_data)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.SUCCESS, run.stderr)
        seen = json.loads(run.stdout.strip().splitlines()[-1])
        self.assertEqual(seen["file"]["run"]["id"], run.id.hex)
        self.assertEqual(seen["file"]["script"], {"id": self.script.id.hex, "name": self.script.name})
        self.assertEqual(seen["file"]["inputs"], run.inputs)
        self.assertEqual(seen["ids"], [run.id.hex, self.script.id.hex, self.script.name])
        self.assertFalse(os.path.exists(seen["path"]), "the run file outlived the run")
        return seen["file"]

    def test_manual_run(self, _val):
        run = self._run(self.script, status=Run.Status.PENDING,
                        inputs={"city": "Paris", "options": {"units": "imperial", "days": [3]}})
        data = self._execute(run)
        self.assertEqual(data["trigger"], {"type": "manual"})
        self.assertEqual(data["inputs"]["options"], {"units": "imperial", "days": [3]})

    def test_scheduled_run(self, _val):
        ScriptSchedule.objects.create(
            script=self.script, workspace=self.ws, run_mode="interval", interval_minutes=60, is_active=True
        )
        with mock.patch("core.tasks.async_task", return_value="t"):
            self.assertTrue(execute_scheduled_run(str(self.script.id))["success"])
        data = self._execute(Run.objects.get(script=self.script))
        self.assertEqual(data["trigger"], {"type": "scheduled"})
        self.assertEqual(data["inputs"], {"city": "Beirut", "options": {"units": "metric", "days": [1, 2]}})

    def test_webhook_run(self, _val):
        self.script.webhook_token = "tok_" + uuid.uuid4().hex
        self.script.save(update_fields=["webhook_token"])
        with mock.patch("core.tasks.async_task", return_value="t") as task:
            resp = self.client.post(
                reverse("webhook_trigger", args=[self.script.webhook_token]),
                data='{"hello": 1}', content_type="application/json",
            )
        self.assertEqual(resp.status_code, 200, resp.content)
        webhook_data = task.call_args.args[2]
        data = self._execute(Run.objects.get(script=self.script), webhook_data=webhook_data)
        self.assertEqual(data["trigger"], {"type": "api"})
        self.assertEqual(data["inputs"]["city"], "Beirut")


class EndToEndTests(_Fixture, LiveServerTestCase):
    """A real script reports through the real loopback endpoints."""

    host = "127.0.0.1"  # the internal API answers loopback only

    @mock.patch("core.executor._validate_environment", return_value=sys.executable)
    def test_a_script_reports_its_result_and_progress(self, _val):
        code = (
            "import os, pyrunner_run\n"
            "pyrunner_run.progress(40, 'half way with ' + os.environ['API_KEY'])\n"
            "pyrunner_run.set_result({'token': os.environ['API_KEY'], 'inputs': pyrunner_run.inputs(),\n"
            "                         'run_id': pyrunner_run.run_id()})\n"
        )
        script = self._script(code=code, inputs_schema=SCHEMA)
        run = self._run(script, status=Run.Status.PENDING,
                        inputs={"city": "Paris", "options": {"units": "metric", "days": [1]}})
        with override_settings(PYRUNNER_INTERNAL_BASE_URL=self.live_server_url), _secrets(), _ai_off():
            execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.SUCCESS, run.stderr)
        self.assertEqual(run.result, {
            "token": "[API_KEY:MASKED]",
            "inputs": {"city": "Paris", "options": {"units": "metric", "days": [1]}},
            "run_id": run.id.hex,
        })
        self.assertEqual(run.progress["pct"], 40)
        self.assertEqual(run.progress["message"], "half way with [API_KEY:MASKED]")


# --------------------------------------------------------------------------- #
# Console
# --------------------------------------------------------------------------- #


class RunPageTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        _setup_wizard_off(self)
        self.user = User.objects.create(email="results@example.com")
        WorkspaceMembership.objects.update_or_create(
            user=self.user, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_OWNER}
        )
        self.client.force_login(self.user)

    def test_result_card_and_download(self):
        run = self._run(status=Run.Status.SUCCESS, result={"rows": 3, "note": "[API_KEY:MASKED]"})
        resp = self.client.get(reverse("cpanel:run_detail", args=[run.pk]))
        self.assertContains(resp, 'id="run-result-heading"')
        self.assertContains(resp, "&quot;rows&quot;: 3")
        self.assertContains(resp, "[API_KEY:MASKED]")
        download = self.client.get(reverse("cpanel:run_result_download", args=[run.pk]))
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment;", download["Content-Disposition"])
        self.assertEqual(json.loads(download.content), run.result)

    def test_no_result_no_card_and_no_download(self):
        run = self._run(status=Run.Status.SUCCESS)
        resp = self.client.get(reverse("cpanel:run_detail", args=[run.pk]))
        self.assertNotContains(resp, 'id="run-result-heading"')
        self.assertEqual(self.client.get(reverse("cpanel:run_result_download", args=[run.pk])).status_code, 404)

    def test_a_running_run_shows_progress_and_polls_its_status(self):
        run = self._run(progress={"pct": 42.4, "message": "Fetching page 3", "updated_at": "x"})
        resp = self.client.get(reverse("cpanel:run_detail", args=[run.pk]))
        self.assertContains(resp, "Fetching page 3")
        self.assertContains(resp, "42%")
        self.assertContains(resp, reverse("cpanel:run_status", args=[run.pk]))
        self.assertContains(resp, "<noscript><meta http-equiv=\"refresh\"")

        status = self.client.get(reverse("cpanel:run_status", args=[run.pk])).json()
        self.assertEqual(status["status"], "running")
        self.assertFalse(status["is_finished"])
        self.assertEqual(status["progress"]["pct"], 42)
        self.assertEqual(status["progress"]["message"], "Fetching page 3")

        Run.objects.filter(pk=run.pk).update(progress={"pct": 80, "message": "Saving"})
        status = self.client.get(reverse("cpanel:run_status", args=[run.pk])).json()
        self.assertEqual((status["progress"]["pct"], status["progress"]["message"]), (80, "Saving"))

    def test_a_finished_run_shows_its_last_progress(self):
        run = self._run(status=Run.Status.FAILED, progress={"pct": 40, "message": "page 3"})
        resp = self.client.get(reverse("cpanel:run_detail", args=[run.pk]))
        self.assertContains(resp, "Last progress")
        self.assertNotContains(resp, 'id="run-live"')

    def test_status_and_download_are_workspace_scoped(self):
        other_ws = Workspace.objects.create(name="Other results workspace")
        script = Script.objects.create(name="theirs", code="x", environment=self.env, workspace=other_ws)
        run = Run.objects.create(script=script, workspace=other_ws, status=Run.Status.SUCCESS,
                                 result={"a": 1})
        self.assertEqual(self.client.get(reverse("cpanel:run_status", args=[run.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("cpanel:run_result_download", args=[run.pk])).status_code, 404)

    def test_status_needs_a_login(self):
        run = self._run()
        self.client.logout()
        self.assertEqual(self.client.get(reverse("cpanel:run_status", args=[run.pk])).status_code, 302)

    def test_runs_list_marks_runs_with_a_result(self):
        script = self._script()
        self._run(script, status=Run.Status.SUCCESS, result={"a": 1})
        self._run(script, status=Run.Status.SUCCESS)
        resp = self.client.get(reverse("cpanel:run_list"))
        self.assertContains(resp, 'title="The script set a result', count=1)


# --------------------------------------------------------------------------- #
# RunView, notifications, backups
# --------------------------------------------------------------------------- #


class RunViewTests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="rv", path=f"rv{uuid.uuid4().hex[:6]}")
        self.api = ScriptAPI("myplugin")
        self.script = self.api.upsert(key="job", code="x", environment=self.env)

    def _make(self, offset=0, **kwargs):
        run = Run.objects.create(script=self.script, workspace=self.ws, status=Run.Status.SUCCESS, **kwargs)
        Run.objects.filter(pk=run.pk).update(created_at=timezone.now() + timedelta(seconds=offset))
        return run

    def test_inputs_result_and_progress(self):
        self._make(inputs={"q": "abc123xyz", "n": 2}, result={"rows": 2},
                   progress={"pct": 100, "message": "done", "updated_at": "t"})
        with _secrets():
            view = self.api.latest_run("job")
        self.assertEqual(view.inputs, {"q": "[API_KEY:MASKED]", "n": 2})
        self.assertEqual(view.result, {"rows": 2})
        self.assertEqual(view.progress["pct"], 100)
        d = view.as_dict()
        self.assertEqual((d["inputs"], d["result"], d["progress"]), (view.inputs, view.result, view.progress))
        json.dumps(d)

    def test_inputs_are_withheld_when_a_secret_cant_be_resolved(self):
        self._make(inputs={"q": "abc123xyz"}, result={"rows": 2})
        with mock.patch(
            "core.executor.resolve_secrets_for_run", side_effect=SecretResolutionError("vault down")
        ):
            view = self.api.latest_run("job")
        self.assertIsNone(view.inputs)
        self.assertEqual(view.result, {"rows": 2})

    def test_runs_resolve_the_secret_set_once(self):
        for i in range(3):
            self._make(offset=i, inputs={"q": f"v{i}"})
        self._make(offset=3)  # no inputs: never resolves
        with _secrets() as resolve:
            views = self.api.runs("job")
        self.assertEqual(len(views), 4)
        self.assertEqual(resolve.call_count, 1)

    def test_runs_try_an_unresolvable_secret_set_once(self):
        for i in range(3):
            self._make(offset=i, inputs={"q": f"v{i}"}, result={"i": i})
        with mock.patch(
            "core.executor.resolve_secrets_for_run", side_effect=SecretResolutionError("vault down")
        ) as resolve:
            views = self.api.runs("job")
        self.assertEqual([v.inputs for v in views], [None, None, None])
        self.assertEqual(sorted(v.result["i"] for v in views), [0, 1, 2])
        self.assertEqual(resolve.call_count, 1)

    def test_a_run_without_any_has_none(self):
        self._make()
        view = self.api.latest_run("job")
        self.assertEqual((view.inputs, view.result, view.progress), (None, None, None))


class NotificationTests(_Fixture, TestCase):
    def test_webhook_payload_carries_the_result(self):
        run = self._run(status=Run.Status.SUCCESS, result={"rows": 3})
        payload = NotificationService._build_webhook_payload(run)
        self.assertEqual(payload["result"], {"rows": 3})
        self.assertFalse(payload["result_too_large"])

    def test_webhook_payload_leaves_out_a_large_result(self):
        run = self._run(status=Run.Status.SUCCESS, result={"x": "a" * NOTIFY_RESULT_MAX_BYTES})
        payload = NotificationService._build_webhook_payload(run)
        self.assertIsNone(payload["result"])
        self.assertTrue(payload["result_too_large"])
        json.dumps(payload)

    def test_webhook_payload_without_a_result(self):
        payload = NotificationService._build_webhook_payload(self._run(status=Run.Status.SUCCESS))
        self.assertIsNone(payload["result"])
        self.assertFalse(payload["result_too_large"])

    def test_channel_text_has_a_capped_excerpt(self):
        # Keys are sorted, so "a" comes first and "z" is the part that's cut.
        run = self._run(status=Run.Status.SUCCESS, result={"a": 3, "z": "z" * 1000})
        text = NotificationService._build_channel_text(run)
        line = next(line for line in text.splitlines() if line.startswith("Result: "))
        self.assertTrue(line.startswith('Result: {"a": 3, "z": "zzz'), line[:40])
        self.assertEqual(len(line), len("Result: ") + CHANNEL_RESULT_MAX_CHARS)
        self.assertTrue(line.endswith("…"))

    def test_email_shows_the_result_unescaped_in_text(self):
        run = self._run(status=Run.Status.SUCCESS, result={"rows": 3, "name": "O'Neil"})
        context = NotificationService._build_email_context(run)
        text = render_to_string("notifications/email/run_completed.txt", context)
        self.assertIn('"name": "O\'Neil"', text)
        html = render_to_string("notifications/email/run_completed.html", context)
        self.assertIn("&quot;rows&quot;: 3", html)

    def test_no_result_no_section(self):
        context = NotificationService._build_email_context(self._run(status=Run.Status.SUCCESS))
        self.assertNotIn("Result:", render_to_string("notifications/email/run_completed.txt", context))
        self.assertNotIn("Result: ", NotificationService._build_channel_text(self._run(status=Run.Status.SUCCESS)))


class BackupTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create(email="admin-results@example.com", is_superuser=True)

    def test_backup_round_trips_result_and_progress(self):
        progress = {"pct": 40, "message": "page 3", "updated_at": "2026-11-08T10:00:00+00:00"}
        run = self._run(status=Run.Status.SUCCESS, result={"rows": [1, 2]}, progress=progress)
        plain = self._run(status=Run.Status.SUCCESS)
        data = BackupService.create_backup(include_runs=True)
        result = BackupService.restore_backup(data, restore_runs=True, current_user=self.user)
        self.assertTrue(result["success"], result)
        restored = Run.objects.get(pk=run.pk)
        self.assertEqual(restored.result, {"rows": [1, 2]})
        self.assertEqual(restored.progress, progress)
        self.assertIsNone(Run.objects.get(pk=plain.pk).result)

    def test_a_backup_without_the_fields_restores_them_as_null(self):
        run = self._run(status=Run.Status.SUCCESS, result={"a": 1}, progress={"pct": 1})
        data = BackupService.create_backup(include_runs=True)
        for row in data["runs"]:
            row.pop("result")
            row.pop("progress")
        result = BackupService.restore_backup(data, restore_runs=True, current_user=self.user)
        self.assertTrue(result["success"], result)
        restored = Run.objects.get(pk=run.pk)
        self.assertIsNone(restored.result)
        self.assertIsNone(restored.progress)
