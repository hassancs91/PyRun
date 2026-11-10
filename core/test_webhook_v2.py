"""
Webhook v2 envelope: request headers and the raw body file (2.5 Stage 4, ticket T20).

What this pins, roughly in order of how much it would hurt to get wrong:

- A body too big for one environment variable no longer stops the run from
  starting (Linux refuses an environment string over 128 KB): a real
  subprocess gets a 500 KB JSON body intact from PYRUNNER_EVENT_BODY_FILE,
  with WEBHOOK_BODY empty and WEBHOOK_BODY_OMITTED=1. On Linux the limit is
  checked against the kernel itself, byte for byte.
- WEBHOOK_* stay exactly as before for every body that could be a variable
  (core/test_run_events.py pins the values; here the task data and the new
  variables around them).
- The body file holds the bytes as sent, a body that isn't UTF-8 included
  (its event says ``body_binary``), is private and is deleted with the run.
- The event carries method, path (token cut), query, headers (credentials and
  hop-by-hop headers left out), content type, body, body_json, body_size,
  received_at and remote_ip.
- A body over Django's upload limit is refused with 413 before any run.
- Each webhook run records the status its call got, and the script page's
  Webhook card lists the last 20 calls with status, duration and that code.
  Backups round-trip the code.
"""

import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import unittest
import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.executor import execute_run
from core.models import (
    Environment,
    MaintenanceWindow,
    Run,
    Script,
    Workspace,
    WorkspaceMembership,
)
from core.services.backup_service import BackupService
from core.services.run_events import EVENT_MAX_BYTES, api_event, manual_event, webhook_event
from core.services.run_reports import json_size
from core.services.run_retries import schedule_retry
from core.services.webhook_request import (
    BODY_BASE64,
    ENV_ENTRY_MAX_BYTES,
    body_env,
    event_headers,
    fits_env,
    raw_body,
)

User = get_user_model()

# Prints what a webhook script can read: its WEBHOOK_* variables, the body
# file (size, hash, mode, path) and the event.
PRINT_BODY = (
    "import hashlib, json, os, stat, pyrunner_run\n"
    "env = {k: v for k, v in os.environ.items() if k.startswith('WEBHOOK_')}\n"
    "path = os.environ.get('PYRUNNER_EVENT_BODY_FILE')\n"
    "seen = {'env_sizes': {k: len(v) for k, v in env.items()}, 'path': path}\n"
    "seen['flag'] = env.get('WEBHOOK_BODY_OMITTED')\n"
    "seen['small_env'] = {k: v for k, v in env.items() if len(v) < 1000}\n"
    "if path:\n"
    "    data = open(path, 'rb').read()\n"
    "    seen['file'] = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),\n"
    "                    'mode': stat.S_IMODE(os.stat(path).st_mode)}\n"
    "event = pyrunner_run.event()\n"
    "seen['event'] = {k: v for k, v in event.items() if k not in ('body', 'body_json')}\n"
    "seen['event_body_len'] = len(event.get('body', ''))\n"
    "seen['event_has_body_json'] = 'body_json' in event\n"
    "print(json.dumps(seen))\n"
)


def _setup_wizard_off(test):
    for target in (
        "core.services.setup_service.SetupService.is_setup_needed",
        "core.services.setup_service.SetupService.needs_admin_setup",
    ):
        p = mock.patch(target, return_value=False)
        p.start()
        test.addCleanup(p.stop)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Fixture:
    def setUp(self):
        _setup_wizard_off(self)
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="wenv", path=f"wenv{uuid.uuid4().hex[:6]}")

    def _script(self, **kwargs):
        kwargs.setdefault("code", "pass")
        return Script.objects.create(
            name=f"w{uuid.uuid4().hex[:6]}", environment=self.env, workspace=self.ws, **kwargs
        )

    def _webhook_script(self, **kwargs):
        return self._script(webhook_token="tok_" + uuid.uuid4().hex, **kwargs)

    def _post(self, script, path_suffix="", method="post", expect=200, **kwargs):
        """Call the script's webhook; return (response, run or None, the task's webhook_data)."""
        url = reverse("webhook_trigger", args=[script.webhook_token]) + path_suffix
        with mock.patch("core.tasks.async_task", return_value="t") as task:
            resp = getattr(self.client, method)(url, **kwargs)
        self.assertEqual(resp.status_code, expect, resp.content)
        run = Run.objects.filter(script=script).first()
        webhook_data = task.call_args.args[2] if task.called else None
        return resp, run, webhook_data

    def _execute(self, run, webhook_data):
        """Run ``run`` in a real subprocess; return what PRINT_BODY printed."""
        with (
            mock.patch("core.executor._validate_environment", return_value=sys.executable),
            mock.patch("core.executor.ClaudeService.get_script_env", return_value={}),
        ):
            execute_run(run, webhook_data=webhook_data)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.SUCCESS, run.stderr)
        return json.loads(run.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------------- #
# The body file, and WEBHOOK_BODY for bodies that can't be a variable
# --------------------------------------------------------------------------- #


class BodyFileTests(_Fixture, TestCase):
    def test_a_500_kb_json_body_arrives_intact(self):
        script = self._webhook_script(code=PRINT_BODY)
        body = json.dumps(
            {"rows": [{"id": i, "note": "café " * 16} for i in range(4500)]}, ensure_ascii=False
        )
        sent = body.encode("utf-8")
        self.assertTrue(500 * 1024 < len(sent) < 600 * 1024, len(sent))
        _, run, webhook_data = self._post(script, data=body, content_type="application/json")

        seen = self._execute(run, webhook_data)

        self.assertEqual(seen["file"]["size"], len(sent))
        self.assertEqual(seen["file"]["sha256"], _sha(sent))
        # Too big for one environment variable: left out, and the script is told.
        self.assertEqual(seen["small_env"]["WEBHOOK_BODY"], "")
        self.assertEqual(seen["flag"], "1")
        self.assertNotIn("WEBHOOK_BODY_JSON", seen["env_sizes"])
        self.assertEqual(seen["small_env"]["WEBHOOK_METHOD"], "POST")
        # The event has its size and, under its 1 MB cap, the text whole.
        self.assertEqual(seen["event"]["body_size"], len(sent))
        self.assertEqual(seen["event_body_len"], len(body))

    def test_a_small_body_keeps_its_variables_and_gets_the_file_too(self):
        script = self._webhook_script(code=PRINT_BODY)
        body = '{"hello": "wörld"}'
        _, run, webhook_data = self._post(
            script, path_suffix="?a=1", data=body, content_type="application/json"
        )
        # The task gets exactly what it got before 2.5.
        self.assertEqual(webhook_data, {
            "method": "POST",
            "query": {"a": ["1"]},
            "content_type": "application/json",
            "body": body,
            "body_json": {"hello": "wörld"},
        })
        seen = self._execute(run, webhook_data)
        self.assertEqual(seen["small_env"], {
            "WEBHOOK_METHOD": "POST",
            "WEBHOOK_QUERY": '{"a": ["1"]}',
            "WEBHOOK_CONTENT_TYPE": "application/json",
            "WEBHOOK_BODY": body,
            "WEBHOOK_BODY_JSON": '{"hello": "w\\u00f6rld"}',
        })
        self.assertIsNone(seen["flag"])
        self.assertEqual(seen["file"]["sha256"], _sha(body.encode("utf-8")))
        self.assertEqual(seen["event"]["body_size"], len(body.encode("utf-8")))

    def test_a_get_has_no_body_file(self):
        script = self._webhook_script(code=PRINT_BODY)
        _, run, webhook_data = self._post(script, method="get", path_suffix="?q=1")
        seen = self._execute(run, webhook_data)
        self.assertIsNone(seen["path"])
        self.assertNotIn("WEBHOOK_BODY", seen["env_sizes"])
        self.assertNotIn("body_size", seen["event"])

    def test_a_body_that_is_not_utf8_reaches_the_file_byte_for_byte(self):
        script = self._webhook_script(code=PRINT_BODY)
        sent = b"\x89PNG\r\n\x1a\n\x00\xff\xfe binary"
        _, run, webhook_data = self._post(script, data=sent, content_type="application/octet-stream")

        # WEBHOOK_BODY stays "" as it always was for such a body; the bytes
        # travel with the task (a held run or a retry keeps them as JSON).
        self.assertEqual(webhook_data["body"], "")
        self.assertEqual(base64.b64decode(webhook_data[BODY_BASE64]), sent)
        self.assertEqual(raw_body(json.loads(json.dumps(webhook_data))), sent)
        self.assertEqual(run.event["body"], "")
        self.assertIs(run.event["body_binary"], True)
        self.assertEqual(run.event["body_size"], len(sent))

        seen = self._execute(run, webhook_data)
        self.assertEqual(seen["file"]["sha256"], _sha(sent))
        self.assertEqual(seen["small_env"]["WEBHOOK_BODY"], "")
        self.assertEqual(seen["flag"], "1")

    def test_a_text_body_holding_a_nul_still_starts(self):
        # No process can get a NUL in an environment variable.
        script = self._webhook_script(code=PRINT_BODY)
        _, run, webhook_data = self._post(script, data="a\x00b", content_type="text/plain")
        self.assertEqual(webhook_data["body"], "a\x00b")
        seen = self._execute(run, webhook_data)
        self.assertEqual(seen["file"]["sha256"], _sha(b"a\x00b"))
        self.assertEqual(seen["flag"], "1")

    def test_a_body_over_the_event_cap_is_whole_in_the_file(self):
        script = self._webhook_script(code=PRINT_BODY)
        body = json.dumps({"blob": "x" * (EVENT_MAX_BYTES + 500_000)})
        _, run, webhook_data = self._post(script, data=body, content_type="application/json")
        self.assertTrue(run.event["body_truncated"])
        self.assertLessEqual(json_size(run.event), EVENT_MAX_BYTES)
        seen = self._execute(run, webhook_data)
        self.assertEqual(seen["file"]["sha256"], _sha(body.encode("utf-8")))
        self.assertEqual(seen["event"]["body_size"], len(body))
        self.assertLess(seen["event_body_len"], len(body))

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_the_file_is_private_and_deleted_when_the_run_ends(self):
        script = self._webhook_script(code=PRINT_BODY)
        _, run, webhook_data = self._post(script, data="secret-ish", content_type="text/plain")
        seen = self._execute(run, webhook_data)
        self.assertEqual(seen["file"]["mode"], 0o600)
        self.assertFalse(os.path.exists(seen["path"]))

    def test_the_file_is_deleted_when_the_run_ends(self):
        script = self._webhook_script(code=PRINT_BODY)
        _, run, webhook_data = self._post(script, data="x", content_type="text/plain")
        seen = self._execute(run, webhook_data)
        self.assertTrue(os.path.basename(seen["path"]).startswith(".pyrun-body-"))
        self.assertFalse(os.path.exists(seen["path"]))

    def test_a_retry_gets_the_same_body(self):
        script = self._webhook_script(code=PRINT_BODY, retry_max_attempts=1)
        sent = b"\xff\x00 not text"
        _, run, webhook_data = self._post(script, data=sent, content_type="application/octet-stream")
        Run.objects.filter(pk=run.pk).update(status=Run.Status.FAILED)
        run.refresh_from_db()
        retry = schedule_retry(run, webhook_data)
        self.assertEqual(raw_body(json.loads(retry.queued_payload)), sent)
        # No call made the retry.
        self.assertIsNone(retry.response_code)


class EnvLimitTests(TestCase):
    """``body_env`` decides from the same rule the kernel applies."""

    def _data(self, body, **extra):
        return {"method": "POST", "query": {}, "content_type": "text/plain", "body": body, **extra}

    def test_a_body_that_fits_is_passed_as_is(self):
        body = "x" * (ENV_ENTRY_MAX_BYTES - len("WEBHOOK_BODY=") - 1)
        self.assertEqual(body_env(self._data(body)), {"WEBHOOK_BODY": body})

    def test_one_byte_more_is_left_out(self):
        body = "x" * (ENV_ENTRY_MAX_BYTES - len("WEBHOOK_BODY="))
        self.assertEqual(body_env(self._data(body)), {"WEBHOOK_BODY": "", "WEBHOOK_BODY_OMITTED": "1"})

    def test_multibyte_text_counts_in_bytes(self):
        room = ENV_ENTRY_MAX_BYTES - len("WEBHOOK_BODY=") - 1
        self.assertTrue(fits_env("WEBHOOK_BODY", "é" * (room // 2)))
        self.assertFalse(fits_env("WEBHOOK_BODY", "é" * (room // 2 + 1)))

    def test_a_body_whose_json_copy_is_too_big_is_left_out_whole(self):
        # 80 KB of UTF-8 text, but WEBHOOK_BODY_JSON escapes each "é" to 6 bytes.
        body = json.dumps("é" * 40_000, ensure_ascii=False)
        data = self._data(body, body_json=json.loads(body))
        self.assertTrue(fits_env("WEBHOOK_BODY", body))
        self.assertEqual(body_env(data), {"WEBHOOK_BODY": "", "WEBHOOK_BODY_OMITTED": "1"})

    def test_no_body_no_variables(self):
        self.assertEqual(body_env({"method": "GET", "query": {}, "content_type": ""}), {})
        self.assertEqual(body_env(None), {})

    @unittest.skipUnless(sys.platform.startswith("linux"), "the limit is Linux's MAX_ARG_STRLEN")
    def test_the_limit_is_the_kernels(self):
        base = {k: v for k, v in os.environ.items() if len(k) + len(v) < 4096}
        ok = "x" * (ENV_ENTRY_MAX_BYTES - len("BIG=") - 1)
        self.assertTrue(fits_env("BIG", ok))
        subprocess.run([sys.executable, "-c", "pass"], env={**base, "BIG": ok}, check=True)
        too_big = ok + "x"
        self.assertFalse(fits_env("BIG", too_big))
        with self.assertRaises(OSError) as raised:
            subprocess.run([sys.executable, "-c", "pass"], env={**base, "BIG": too_big}, check=True)
        self.assertEqual(raised.exception.errno, 7)  # E2BIG: Argument list too long


@unittest.skipUnless(sys.platform.startswith("linux"), "the limit is Linux's MAX_ARG_STRLEN")
class LinuxBoundaryRunTests(_Fixture, TestCase):
    """The biggest body that fits still reaches WEBHOOK_BODY in a real run."""

    def test_the_largest_body_that_fits_is_in_webhook_body(self):
        script = self._webhook_script(code=PRINT_BODY)
        body = "y" * (ENV_ENTRY_MAX_BYTES - len("WEBHOOK_BODY=") - 1)
        _, run, webhook_data = self._post(script, data=body, content_type="text/plain")
        seen = self._execute(run, webhook_data)
        self.assertEqual(seen["env_sizes"]["WEBHOOK_BODY"], len(body))
        self.assertIsNone(seen["flag"])


# --------------------------------------------------------------------------- #
# The request in the event
# --------------------------------------------------------------------------- #


class EnvelopeTests(_Fixture, TestCase):
    def test_the_event_carries_the_request(self):
        script = self._webhook_script()
        body = '{"action": "opened"}'
        _, run, _ = self._post(
            script,
            path_suffix="?src=gh&src=x",
            data=body,
            content_type="application/json",
            headers={
                "X-GitHub-Event": "issues",
                "X-Hub-Signature-256": "sha256=abc",
                "Authorization": "Bearer hunter2",
                "Cookie": "sessionid=s3cret",
                "Proxy-Authorization": "Basic cHJveHk=",
                "Connection": "keep-alive, X-Per-Hop",
                "X-Per-Hop": "1",
                "Keep-Alive": "timeout=5",
                "Upgrade": "h2c",
            },
        )
        event = run.event
        self.assertEqual(event["kind"], "webhook")
        self.assertEqual(event["method"], "POST")
        self.assertEqual(event["path"], f"/webhook/{script.webhook_token[:8]}.../")
        self.assertEqual(event["remote_ip"], "127.0.0.1")
        self.assertEqual(event["query"], {"src": ["gh", "x"]})
        self.assertEqual(event["content_type"], "application/json")
        self.assertEqual(event["body"], body)
        self.assertEqual(event["body_json"], {"action": "opened"})
        self.assertEqual(event["body_size"], len(body))
        self.assertNotIn("body_binary", event)
        self.assertIn("received_at", event)

        headers = event["headers"]
        self.assertEqual(headers["x-github-event"], "issues")
        self.assertEqual(headers["x-hub-signature-256"], "sha256=abc")
        self.assertEqual(headers["content-type"], "application/json")
        for hidden in ("authorization", "cookie", "proxy-authorization", "connection",
                       "x-per-hop", "keep-alive", "upgrade"):
            self.assertNotIn(hidden, headers)
        stored = json.dumps(event)
        for secret in ("hunter2", "s3cret", script.webhook_token):
            self.assertNotIn(secret, stored)

    def test_a_get_has_headers_and_no_body_keys(self):
        script = self._webhook_script()
        _, run, _ = self._post(script, method="get", headers={"X-Source": "probe"})
        self.assertEqual(run.event["method"], "GET")
        self.assertEqual(run.event["headers"]["x-source"], "probe")
        for key in ("body", "body_json", "body_size", "body_binary"):
            self.assertNotIn(key, run.event)

    @override_settings(RATELIMIT_TRUSTED_PROXY_DEPTH=1)
    def test_remote_ip_is_the_caller_behind_a_trusted_proxy(self):
        script = self._webhook_script()
        _, run, _ = self._post(script, data="x", content_type="text/plain",
                               headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(run.event["remote_ip"], "203.0.113.9")

    def test_event_headers_helper(self):
        self.assertEqual(
            event_headers({"Content-Type": "a", "TE": "trailers", "Trailer": "x", "X-A": "b",
                           "Transfer-Encoding": "chunked", "Proxy-Connection": "close"}),
            {"content-type": "a", "x-a": "b"},
        )

    def test_json_that_python_cannot_load_stays_text(self):
        script = self._webhook_script()
        for body in ("[" * 100_000 + "]" * 100_000, "1" * 5000):
            Run.objects.filter(script=script).delete()
            _, run, webhook_data = self._post(script, data=body, content_type="application/json")
            self.assertEqual(webhook_data["body"], body)
            self.assertNotIn("body_json", webhook_data)
            self.assertEqual(run.event["body_size"], len(body))

    def test_json_nested_deeper_than_the_event_can_store_stays_text_in_the_event(self):
        # 2.5 test pass, F1: Python's parser takes deeper nesting than the
        # event can store. The event keeps the body as text, without
        # body_json and without ``truncated`` (it is 3 KB, far under the cap);
        # WEBHOOK_BODY / WEBHOOK_BODY_JSON stay what they always were.
        script = self._webhook_script()
        for depth in (1_500, 3_000):
            body = "[" * depth + "]" * depth
            Run.objects.filter(script=script).delete()
            _, run, webhook_data = self._post(script, data=body, content_type="application/json")
            run.refresh_from_db()
            event = run.event
            self.assertEqual(event["body"], body, depth)
            self.assertEqual(event["body_size"], len(body), depth)
            self.assertNotIn("body_json", event, depth)
            self.assertNotIn("truncated", event, depth)
            self.assertNotIn("body_truncated", event, depth)

            try:
                parsed = json.loads(body)
            except RecursionError:  # the C parser's own depth limit (platform)
                parsed = None
            expected_env = {"WEBHOOK_BODY": body}
            if parsed is not None:
                self.assertEqual(webhook_data["body_json"], parsed, depth)
                expected_env["WEBHOOK_BODY_JSON"] = json.dumps(parsed)
            else:
                self.assertNotIn("body_json", webhook_data, depth)
            self.assertEqual(body_env(webhook_data), expected_env, depth)

    def test_json_the_event_can_store_keeps_body_json(self):
        script = self._webhook_script()
        body = "[" * 900 + "]" * 900
        _, run, _ = self._post(script, data=body, content_type="application/json")
        run.refresh_from_db()
        self.assertIn("body_json", run.event)
        self.assertNotIn("truncated", run.event)

    def test_the_builder_still_takes_v1_data_alone(self):
        event = webhook_event({"method": "POST", "query": {}, "content_type": "", "body": "hi"})
        self.assertEqual(event["body_size"], 2)
        for key in ("path", "headers", "remote_ip"):
            self.assertNotIn(key, event)


@override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=1000)
class TooLargeTests(_Fixture, TestCase):
    def test_a_body_over_the_upload_limit_is_refused_before_any_run(self):
        script = self._webhook_script()
        for content_type in ("application/json", "application/x-www-form-urlencoded", "text/plain"):
            resp, run, _ = self._post(
                script, expect=413, data="a" * 1001, content_type=content_type
            )
            self.assertIn("1000 bytes", resp.json()["error"])
            self.assertIsNone(run)

    def test_a_body_at_the_limit_is_taken(self):
        script = self._webhook_script()
        _, run, webhook_data = self._post(script, data="a" * 1000, content_type="text/plain")
        self.assertEqual(len(webhook_data["body"]), 1000)


class CutShortBodyTests(_Fixture, TestCase):
    """A sender that hangs up mid-body (2.5 test pass, F2): the WSGI server
    hands over the bytes that arrived without an error, so the body is
    shorter than its Content-Length. It gets 400 and no run, signed or not."""

    def _cut_short(self, script, sent=b'{"order": 1', declared=1000, **headers):
        url = reverse("webhook_trigger", args=[script.webhook_token])
        with mock.patch("core.tasks.async_task", return_value="t") as task:
            resp = self.client.generic(
                "POST", url, content_type="application/json", headers=headers,
                **{"wsgi.input": io.BytesIO(sent), "CONTENT_LENGTH": str(declared)},
            )
        self.task_called = task.called
        return resp

    def test_a_body_shorter_than_its_content_length_gets_400_and_no_run(self):
        script = self._webhook_script()
        with self.assertLogs("core.views.webhooks", "WARNING") as logs:
            resp = self._cut_short(script)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json(), {"error": "Could not read the request body."})
        self.assertIn("ended after 11 of 1000 bytes", "\n".join(logs.output))
        self.assertFalse(Run.objects.filter(script=script).exists())
        self.assertFalse(self.task_called)

    def test_a_signed_script_refuses_it_before_checking_the_signature(self):
        script = self._webhook_script(webhook_signature=Script.WebhookSignature.GITHUB)
        script.set_webhook_signature_secret("s")
        script.save()
        resp = self._cut_short(script, **{"X-Hub-Signature-256": "sha256=" + "0" * 64})
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(Run.objects.filter(script=script).exists())

    def test_a_whole_body_is_still_taken(self):
        script = self._webhook_script()
        body = b'{"order": 1}'
        resp = self._cut_short(script, sent=body, declared=len(body))
        self.assertEqual(resp.status_code, 200, resp.content)
        run = Run.objects.get(script=script)
        self.assertEqual(run.event["body"], body.decode())


# --------------------------------------------------------------------------- #
# Response codes and the Recent deliveries card
# --------------------------------------------------------------------------- #


class ResponseCodeTests(_Fixture, TestCase):
    def test_a_queued_call_records_200(self):
        script = self._webhook_script()
        _, run, _ = self._post(script, data="x", content_type="text/plain")
        run.refresh_from_db()
        self.assertEqual(run.response_code, 200)

    def test_a_skipped_call_records_200(self):
        script = self._webhook_script(concurrency_policy="skip")
        Run.objects.create(script=script, workspace=self.ws, status=Run.Status.RUNNING)
        url = reverse("webhook_trigger", args=[script.webhook_token])
        with mock.patch("core.tasks.async_task", return_value="t"):
            resp = self.client.post(url, data="x", content_type="text/plain")
        self.assertEqual(resp.json()["status"], "skipped")
        self.assertEqual(Run.objects.get(pk=resp.json()["run_id"]).response_code, 200)

    def test_a_maintenance_block_records_503(self):
        script = self._webhook_script()
        now = timezone.now()
        MaintenanceWindow.objects.create(
            name="Upgrade", kind=MaintenanceWindow.Kind.ONE_OFF,
            starts_at=now - timedelta(minutes=5), ends_at=now + timedelta(minutes=30),
            blocks=list(MaintenanceWindow.DEFAULT_BLOCKS),
        )
        _, run, _ = self._post(script, expect=503, data="x", content_type="text/plain")
        run.refresh_from_db()
        self.assertEqual(run.response_code, 503)
        self.assertTrue(run.is_maintenance_skipped)

    def test_a_failure_to_queue_records_500(self):
        script = self._webhook_script()
        url = reverse("webhook_trigger", args=[script.webhook_token])
        with mock.patch("core.tasks.async_task", side_effect=RuntimeError("broker down")):
            resp = self.client.post(url, data="x", content_type="text/plain")
        self.assertEqual(resp.status_code, 500)
        run = Run.objects.get(script=script)
        self.assertEqual(run.response_code, 500)
        self.assertEqual(run.status, Run.Status.FAILED)

    def test_other_triggers_record_none(self):
        script = self._script()
        run = Run.objects.create(script=script, workspace=self.ws, event=manual_event())
        self.assertIsNone(run.response_code)


class DeliveriesCardTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create(email="deliveries@example.com")
        WorkspaceMembership.objects.update_or_create(
            user=self.user, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_OWNER}
        )
        self.client.force_login(self.user)

    def _webhook_run(self, script, code=200, **kwargs):
        return Run.objects.create(
            script=script, workspace=self.ws, trigger_type=Run.TriggerType.API,
            event=webhook_event({"method": "POST", "query": {}, "content_type": "", "body": "b"}),
            response_code=code, **kwargs,
        )

    def _page(self, script):
        resp = self.client.get(reverse("cpanel:script_detail", args=[script.pk]))
        self.assertEqual(resp.status_code, 200)
        return resp

    def test_lists_the_last_20_webhook_calls_newest_first(self):
        script = self._webhook_script()
        now = timezone.now()
        calls = []
        for i in range(22):
            run = self._webhook_run(script, status=Run.Status.SUCCESS)
            Run.objects.filter(pk=run.pk).update(created_at=now - timedelta(minutes=30 - i))
            calls.append(run)
        newest = calls[-1]
        Run.objects.filter(pk=newest.pk).update(
            response_code=503, status=Run.Status.CANCELLED, cancel_reason=Run.CancelReason.MAINTENANCE,
        )
        # Not deliveries: a manual run, a scripts.run API run, a retry.
        Run.objects.create(script=script, workspace=self.ws, event=manual_event())
        Run.objects.create(script=script, workspace=self.ws, trigger_type=Run.TriggerType.API,
                           event=api_event(1, "rest"))
        self._webhook_run(script, code=None, attempt=2, retry_of=calls[0])

        resp = self._page(script)
        shown = resp.context["webhook_deliveries"]
        self.assertEqual([r.pk for r in shown], [r.pk for r in reversed(calls)][:20])
        html = resp.content.decode()
        self.assertIn("Recent deliveries", html)
        self.assertIn(">503<", html)
        self.assertIn("maintenance", html)

    def test_duration_and_a_call_from_before_2_5(self):
        script = self._webhook_script()
        now = timezone.now()
        self._webhook_run(script, code=None, status=Run.Status.SUCCESS,
                          started_at=now - timedelta(seconds=3), ended_at=now)
        html = self._page(script).content.decode()
        card = html[html.index('id="webhook-deliveries"'):]
        self.assertIn("Not recorded (a call from before PyRun 2.5)", card)
        self.assertIn("3.0s", card)

    def test_no_calls_yet(self):
        script = self._webhook_script()
        html = self._page(script).content.decode()
        self.assertIn("No calls yet.", html)

    def test_a_script_without_a_webhook_or_calls_has_no_list(self):
        script = self._script()
        self.assertNotIn("Recent deliveries", self._page(script).content.decode())

    def test_past_calls_stay_listed_after_the_webhook_is_disabled(self):
        script = self._script()
        self._webhook_run(script)
        self.assertIn("Recent deliveries", self._page(script).content.decode())


class BackupTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create(email="admin-webhooks@example.com", is_superuser=True)

    def _restore(self, data):
        result = BackupService.restore_backup(data, restore_runs=True, current_user=self.user)
        self.assertTrue(result["success"], result)

    def test_backup_round_trips_the_response_code(self):
        script = self._script()
        coded = Run.objects.create(script=script, workspace=self.ws, status=Run.Status.SUCCESS,
                                   response_code=503)
        plain = Run.objects.create(script=script, workspace=self.ws, status=Run.Status.SUCCESS)
        self._restore(BackupService.create_backup(include_runs=True))
        self.assertEqual(Run.objects.get(pk=coded.pk).response_code, 503)
        self.assertIsNone(Run.objects.get(pk=plain.pk).response_code)

    def test_an_older_or_hand_edited_backup_restores_unknown(self):
        script = self._script()
        runs = [
            Run.objects.create(script=script, workspace=self.ws, status=Run.Status.SUCCESS,
                               response_code=200)
            for _ in range(4)
        ]
        data = BackupService.create_backup(include_runs=True)
        by_id = {row["id"]: row for row in data["runs"]}
        by_id[str(runs[0].pk)].pop("response_code")
        by_id[str(runs[1].pk)]["response_code"] = "200"
        by_id[str(runs[2].pk)]["response_code"] = 70000
        by_id[str(runs[3].pk)]["response_code"] = True
        self._restore(data)
        for run in runs:
            self.assertIsNone(Run.objects.get(pk=run.pk).response_code)
