"""
Synchronous webhook replies (2.5 Stage 4, ticket T22).

What this pins, roughly in order of how much it would hurt to get wrong:

- Async stays the default: a call without ``?sync=1`` to a script left in
  async mode is answered at once, exactly as before, and never takes a wait
  slot.
- A sync call (``?sync=1`` or the script's mode) waits through the shared
  run-wait service: the reply the script set with ``pyrunner_run.respond()``
  comes back with its status (clamped to 200-599), body and only the
  allow-listed headers, masked, and with a sandboxing CSP. No Set-Cookie,
  Location or web page can come from PyRun's origin.
- Past the budget (the script's timeout, at most 55 s) the call gets 202 with
  the run id and a Location for runs.get; with every slot taken it gets 202
  at once (nine concurrent calls: four wait, five don't); no worker → 503 and
  no run; maintenance → 503 + Retry-After; a Skip-policy skip → 409.
- The status sent is stored on the run (``response_code``) for Recent
  deliveries, without a worker's save being able to overwrite it.
- ``respond()`` keeps the rest of the result and ``set_result()`` keeps the
  reply; the helper checks the same rules as PyRun, and a real script's
  reply goes through the real loopback endpoint.
- The mode is set on the webhook settings page, shown on the card, kept by
  backups (an older or hand-edited backup gets async) and reset by a fork.
"""

import json
import sys
import uuid
from contextlib import ExitStack
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import LiveServerTestCase, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Environment,
    GlobalSettings,
    MaintenanceWindow,
    Run,
    Script,
    Workspace,
    WorkspaceMembership,
)
from core.script_helpers import pyrunner_run
from core.services import run_wait, script_service, webhook_reply
from core.services.backup_service import BackupService
from core.services.datastore_token import mint_datastore_token
from core.services.webhook_reply import (
    RESPONSE_KEY,
    SANDBOX_POLICY,
    check_response,
    finished_reply,
    stored_response,
)
from core.services.webhook_signatures import restored_settings
from core.test_run_reports import _secrets
from core.test_webhook_v2 import _Fixture, _setup_wizard_off

User = get_user_model()

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                      "LOCATION": "t22-webhook-sync"}}
REPLY = {"status": 201, "headers": {"Content-Type": "application/json"}, "body": {"ok": True}}


def _envelope(status=200, headers=None, body=None):
    return {RESPONSE_KEY: {"status": status, "headers": headers or {}, "body": body}}


# --------------------------------------------------------------------------- #
# The reply's rules (shared by the store and the view)
# --------------------------------------------------------------------------- #


class ResponseRuleTests(SimpleTestCase):
    def test_the_status_is_clamped_to_200_through_599(self):
        for given, sent in ((99, 200), (100, 200), (204, 204), (302, 302), (404, 404),
                            (599, 599), (600, 599), (10**6, 599), (-1, 200)):
            self.assertEqual(check_response({"status": given})["status"], sent, given)

    def test_the_status_must_be_an_integer(self):
        for status in (True, "200", 200.0, None):
            with self.assertRaises(ValueError, msg=status):
                check_response({"status": status})

    def test_the_defaults_are_200_no_headers_and_no_body(self):
        self.assertEqual(check_response({}), {"status": 200, "headers": {}, "body": None})

    def test_only_the_allowed_headers_pass_with_their_names_as_sent(self):
        reply = check_response({"headers": {
            "content-type": "text/plain; charset=utf-8", "CACHE-CONTROL": "no-store",
            "Content-Language": "fr", "retry-after": "30",
        }})
        self.assertEqual(reply["headers"], {
            "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store",
            "Content-Language": "fr", "Retry-After": "30",
        })
        for name in ("Set-Cookie", "Location", "X-Accel-Redirect", "Access-Control-Allow-Origin",
                     "Content-Security-Policy", "X-Frame-Options", "Refresh", "Link"):
            with self.assertRaises(ValueError, msg=name):
                check_response({"headers": {name: "x"}})

    def test_a_header_value_is_one_short_line_of_text(self):
        for value in ("a\r\nSet-Cookie: s=1", "a\nb", "a\0b", "x" * 1001, 30, None):
            with self.assertRaises(ValueError, msg=value):
                check_response({"headers": {"Cache-Control": value}})
        with self.assertRaises(ValueError):
            check_response({"headers": {"Cache-Control": "a", "cache-control": "b"}})

    def test_content_type_must_be_a_data_type_not_a_page(self):
        for ok in ("application/json", "Application/JSON; charset=utf-8", "text/plain",
                   "text/csv", "application/xml", "text/xml; charset=utf-8",
                   "application/x-www-form-urlencoded", "application/problem+json",
                   "application/vnd.api+json"):
            check_response({"headers": {"Content-Type": ok}})
        for bad in ("text/html", "application/xhtml+xml", "image/svg+xml", "text/javascript",
                    "application/javascript", "application/+json", "multipart/form-data", ""):
            with self.assertRaises(ValueError, msg=bad):
                check_response({"headers": {"Content-Type": bad}})

    def test_the_stored_reply_is_read_leniently(self):
        # Written some other way (set_result, a backup): what the rules don't
        # allow is left out, not sent.
        result = _envelope(700, {"Set-Cookie": "s=1", "Cache-Control": "no-store",
                                 "Content-Type": "text/html", "Retry-After": "a\nb"}, "hi")
        self.assertEqual(stored_response(result), (599, {"Cache-Control": "no-store"}, "hi"))
        for not_a_reply in (None, {}, {"x": 1}, {RESPONSE_KEY: "ok"}, {RESPONSE_KEY: {"status": "200"}},
                            {RESPONSE_KEY: {"status": True}}, [1]):
            self.assertIsNone(stored_response(not_a_reply), not_a_reply)
        self.assertEqual(stored_response({RESPONSE_KEY: {"status": 200, "headers": "x"}}), (200, {}, None))


class FinishedReplyTests(TestCase):
    def setUp(self):
        env = Environment.objects.create(name="t22env", path=f"t22env{uuid.uuid4().hex[:6]}")
        self.script = Script.objects.create(name="t22 reply", environment=env, code="pass",
                                            workspace=Workspace.get_default())

    def _run(self, status=Run.Status.SUCCESS, result=None):
        return Run.objects.create(script=self.script, workspace=self.script.workspace,
                                  status=status, result=result)

    def test_a_text_body_is_sent_as_text(self):
        response = finished_reply(self._run(result=_envelope(200, body="pong")))
        self.assertEqual((response.status_code, response.content), (200, b"pong"))
        self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(response["Content-Security-Policy"], SANDBOX_POLICY)

    def test_a_json_body_is_sent_as_json_with_its_headers(self):
        result = _envelope(201, {"Cache-Control": "no-store", "Retry-After": "5"},
                           {"text": "café", "n": [1, 2]})
        response = finished_reply(self._run(result=result))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(json.loads(response.content), {"text": "café", "n": [1, 2]})
        self.assertEqual((response["Cache-Control"], response["Retry-After"]), ("no-store", "5"))

    def test_the_script_can_choose_the_content_type(self):
        twiml = "<Response><Message>Hi</Message></Response>"
        response = finished_reply(self._run(result=_envelope(200, {"Content-Type": "text/xml"}, twiml)))
        self.assertEqual((response["Content-Type"], response.content.decode()), ("text/xml", twiml))
        self.assertEqual(response["Content-Security-Policy"], SANDBOX_POLICY)

    def test_no_body_is_an_empty_text_answer(self):
        response = finished_reply(self._run(result=_envelope(204)))
        self.assertEqual((response.status_code, response.content), (204, b""))
        self.assertNotIn("text/html", response["Content-Type"])

    def test_the_reply_is_sent_however_the_run_ended(self):
        response = finished_reply(self._run(Run.Status.FAILED, _envelope(422, body={"error": "bad"})))
        self.assertEqual((response.status_code, json.loads(response.content)), (422, {"error": "bad"}))

    def test_without_a_reply_a_success_answers_200_with_the_result(self):
        run = self._run(result={"total": 3})
        response = finished_reply(run)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {"status": "success", "run_id": str(run.id),
                                                        "script": "t22 reply", "result": {"total": 3}})

    def test_without_a_reply_any_other_end_answers_500(self):
        for status in (Run.Status.FAILED, Run.Status.TIMEOUT, Run.Status.CANCELLED):
            response = finished_reply(self._run(status))
            data = json.loads(response.content)
            self.assertEqual((response.status_code, data["status"], data["result"]), (500, status, None))
            self.assertIn("error", data)


# --------------------------------------------------------------------------- #
# The helper and the loopback endpoint
# --------------------------------------------------------------------------- #


class HelperTests(SimpleTestCase):
    def _respond(self, *args, **kwargs):
        with mock.patch.object(pyrunner_run, "_post", return_value={"ok": True}) as post:
            pyrunner_run.respond(*args, **kwargs)
        path, body, _timeout = post.call_args.args
        self.assertEqual(path, "respond")
        return json.loads(body)

    def test_it_posts_the_reply(self):
        self.assertEqual(self._respond(), {"status": 200, "headers": {}, "body": None})
        self.assertEqual(
            self._respond(201, {"ok": True}, {"content-type": "application/json", "Retry-After": 30}),
            {"status": 201, "headers": {"Content-Type": "application/json", "Retry-After": "30"},
             "body": {"ok": True}},
        )
        self.assertEqual(self._respond(body="pong")["body"], "pong")

    def test_wrong_arguments_fail_in_the_script_before_anything_is_sent(self):
        cases = [
            ((True,), {}, TypeError), (("200",), {}, TypeError),
            ((200, b"raw"), {}, TypeError), ((200, {"x": float("nan")}), {}, ValueError),
            ((200, None, ["Content-Type"]), {}, TypeError),
            ((200, None, {"Set-Cookie": "s=1"}), {}, ValueError),
            ((200, None, {"Cache-Control": "a\r\nb"}), {}, ValueError),
            ((200, None, {"Cache-Control": ["no-store"]}), {}, TypeError),
            ((200, None, {"Content-Type": "text/html"}), {}, ValueError),
            ((200, None, {"Cache-Control": "a", "cache-control": "b"}), {}, ValueError),
            ((200, "x" * (pyrunner_run.RESULT_MAX_BYTES + 1)), {}, ValueError),
        ]
        for args, kwargs, error in cases:
            with mock.patch.object(pyrunner_run, "_post") as post, self.assertRaises(error, msg=args):
                pyrunner_run.respond(*args, **kwargs)
            post.assert_not_called()

    def test_the_helper_and_pyrun_allow_the_same_headers_and_types(self):
        self.assertEqual(set(pyrunner_run.RESPONSE_HEADERS), set(webhook_reply.ALLOWED_HEADERS.values()))
        self.assertEqual(set(pyrunner_run.RESPONSE_CONTENT_TYPES), set(webhook_reply.ALLOWED_CONTENT_TYPES))
        self.assertEqual(pyrunner_run._JSON_SUFFIX_RE.pattern, webhook_reply._JSON_SUFFIX_RE.pattern)
        self.assertEqual(pyrunner_run.RESPONSE_HEADER_MAX_CHARS, webhook_reply.HEADER_VALUE_MAX_CHARS)
        self.assertIn("respond", pyrunner_run.__all__)


class RespondEndpointTests(TestCase):
    def setUp(self):
        env = Environment.objects.create(name="t22env", path=f"t22env{uuid.uuid4().hex[:6]}")
        script = Script.objects.create(name="t22 respond", environment=env, code="pass",
                                       workspace=Workspace.get_default())
        self.run = Run.objects.create(script=script, workspace=script.workspace, status=Run.Status.RUNNING)

    def _post(self, name, body):
        return self.client.post(
            reverse(f"internal:{name}"), data=json.dumps(body), content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {mint_datastore_token(self.run.id)}",
        )

    def test_the_reply_is_stored_in_the_result_masked(self):
        with _secrets():
            resp = self._post("runs_respond", {
                "status": 404,
                "headers": {"Cache-Control": "key abc123xyz"},
                "body": {"msg": "no abc123xyz here", "abc123xyz": 1},
            })
        self.assertEqual(resp.status_code, 200, resp.content)
        self.run.refresh_from_db()
        self.assertEqual(self.run.result, {RESPONSE_KEY: {
            "status": 404,
            "headers": {"Cache-Control": "key [API_KEY:MASKED]"},
            "body": {"msg": "no [API_KEY:MASKED] here", "[API_KEY:MASKED]": 1},
        }})

    def test_respond_keeps_the_result_and_set_result_keeps_the_reply(self):
        with _secrets({}):
            self._post("runs_result", {"result": {"rows": 3}})
            self._post("runs_respond", {"status": 200, "body": "first"})
            self._post("runs_respond", {"status": 202, "body": "second"})
            self.run.refresh_from_db()
            self.assertEqual(self.run.result, {"rows": 3, RESPONSE_KEY: {
                "status": 202, "headers": {}, "body": "second"}})
            self._post("runs_result", {"result": {"rows": 4}})
            self.run.refresh_from_db()
            self.assertEqual(self.run.result, {"rows": 4, RESPONSE_KEY: {
                "status": 202, "headers": {}, "body": "second"}})
            # A result with its own reply replaces it.
            self._post("runs_result", {"result": {RESPONSE_KEY: {"status": 200, "body": "own"}}})
            self.run.refresh_from_db()
            self.assertEqual(self.run.result, {RESPONSE_KEY: {"status": 200, "body": "own"}})

    def test_a_bad_reply_is_refused_and_nothing_is_stored(self):
        for body in ({"status": "200"}, {"headers": {"Set-Cookie": "s=1"}},
                     {"headers": {"Content-Type": "text/html"}}, {"headers": []}):
            with _secrets({}):
                resp = self._post("runs_respond", body)
            self.assertEqual(resp.status_code, 400, body)
            self.assertEqual(resp.json()["error"]["code"], "BAD_REQUEST")
        self.run.refresh_from_db()
        self.assertIsNone(self.run.result)

    def test_the_reply_counts_toward_the_result_cap(self):
        with _secrets({}):
            self._post("runs_result", {"result": {"big": "x" * (200 * 1024)}})
            resp = self._post("runs_respond", {"body": "y" * (60 * 1024)})
        self.assertEqual((resp.status_code, resp.json()["error"]["code"]), (413, "RESULT_TOO_LARGE"))

    def test_a_finished_run_can_no_longer_reply(self):
        Run.objects.filter(pk=self.run.pk).update(status=Run.Status.SUCCESS)
        with _secrets({}):
            resp = self._post("runs_respond", {"body": "late"})
        self.assertEqual((resp.status_code, resp.json()["error"]["code"]), (409, "RUN_NOT_RUNNING"))


# --------------------------------------------------------------------------- #
# The webhook view
# --------------------------------------------------------------------------- #


@override_settings(CACHES=LOCMEM, SYNC_WAIT_SLOTS=4)
class SyncCallTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        settings = GlobalSettings.get_settings()
        settings.worker_heartbeat_at = timezone.now()
        settings.save(update_fields=["worker_heartbeat_at"])
        queued = mock.patch("core.tasks.async_task", return_value="t22-task")
        self.async_task = queued.start()
        self.addCleanup(queued.stop)

    def _call(self, script, query="", method="post", **kwargs):
        url = reverse("webhook_trigger", args=[script.webhook_token]) + query
        if method == "post":
            kwargs.setdefault("data", '{"text": "hi"}')
            kwargs.setdefault("content_type", "application/json")
        return getattr(self.client, method)(url, **kwargs)

    def finish_on_sleep(self, status=Run.Status.SUCCESS, **fields):
        """A worker stand-in: the wait's first sleep ends the run (``fields``)."""

        def sleep(_seconds):
            Run.objects.filter(status__in=[Run.Status.PENDING, Run.Status.RUNNING]).update(
                status=status, ended_at=timezone.now(), **fields
            )

        return mock.patch.object(run_wait, "_sleep", side_effect=sleep)

    def _slots_free(self):
        return all(cache.get(run_wait.SLOT_KEY.format(i)) is None for i in range(4))

    # -- async stays as it was ------------------------------------------- #

    def test_async_is_the_default_and_takes_no_slot(self):
        script = self._webhook_script()
        self.assertEqual(script.webhook_mode, Script.WebhookMode.ASYNC)
        with mock.patch.object(run_wait, "reserve") as reserve:
            for query in ("", "?sync=0", "?sync=no", "?sync="):
                resp = self._call(script, query)
                self.assertEqual(resp.status_code, 200, query)
                self.assertEqual(resp.json()["status"], "queued")
                self.assertEqual(set(resp.json()), {"status", "run_id", "script"})
        reserve.assert_not_called()
        self.assertEqual(set(Run.objects.filter(script=script).values_list("response_code", flat=True)), {200})

    # -- the reply ------------------------------------------------------- #

    def test_sync_query_returns_the_scripts_reply(self):
        script = self._webhook_script()
        with self.finish_on_sleep(result={"rows": 2, RESPONSE_KEY: REPLY}):
            resp = self._call(script, "?sync=1")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(json.loads(resp.content), {"ok": True})
        self.assertEqual(resp["Content-Type"], "application/json")
        self.assertEqual(resp["Content-Security-Policy"], SANDBOX_POLICY)
        run = Run.objects.get(script=script)
        self.assertEqual(run.response_code, 201)
        # The call's query keeps sync, like any parameter.
        self.assertEqual(run.event["query"], {"sync": ["1"]})
        self.assertTrue(self._slots_free())

    def test_sync_mode_waits_without_the_query_and_for_a_get(self):
        script = self._webhook_script(webhook_mode=Script.WebhookMode.SYNC)
        with self.finish_on_sleep(result=_envelope(200, body="pong")):
            resp = self._call(script, method="get")
        self.assertEqual((resp.status_code, resp.content), (200, b"pong"))
        with self.finish_on_sleep(result=_envelope(200, body="pong")):
            self.assertEqual(self._call(script, "?sync=true").content, b"pong")

    def test_the_reply_comes_from_the_masked_result_only_with_allowed_headers(self):
        # A result written some other way (set_result with the key): nothing
        # the rules refuse reaches the caller.
        script = self._webhook_script()
        result = _envelope(100, {"Set-Cookie": "sessionid=x", "Location": "https://evil.example",
                                 "Content-Type": "text/html"}, "<script>alert(1)</script>")
        with self.finish_on_sleep(result=result):
            resp = self._call(script, "?sync=1")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("Set-Cookie", resp)
        self.assertNotIn("Location", resp)
        self.assertEqual(resp["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(resp["Content-Security-Policy"], SANDBOX_POLICY)
        self.assertEqual(resp["X-Content-Type-Options"], "nosniff")

    def test_without_a_reply_the_run_status_and_result_come_back(self):
        script = self._webhook_script()
        with self.finish_on_sleep(result={"total": 7}):
            resp = self._call(script, "?sync=1")
        self.assertEqual((resp.status_code, resp.json()["result"]), (200, {"total": 7}))
        with self.finish_on_sleep(status=Run.Status.FAILED):
            resp = self._call(script, "?sync=1")
        self.assertEqual((resp.status_code, resp.json()["status"]), (500, "failed"))
        codes = set(Run.objects.filter(script=script).values_list("response_code", flat=True))
        self.assertEqual(codes, {200, 500})

    @override_settings(SYNC_WAIT_MAX_SECONDS=55)
    def test_the_budget_is_the_scripts_timeout_at_most_55_seconds(self):
        for timeout, expected in ((5, 5), (300, 55)):
            script = self._webhook_script(timeout_seconds=timeout)
            with mock.patch.object(run_wait, "reserve", wraps=run_wait.reserve) as reserve, \
                    self.finish_on_sleep():
                self._call(script, "?sync=1")
            self.assertEqual(reserve.call_args.args[0], expected, timeout)

    # -- 202, 503, 409 ---------------------------------------------------- #

    def test_past_the_budget_the_call_gets_202_with_the_run_id_and_a_location(self):
        script = self._webhook_script(timeout_seconds=3)
        clock = [100.0]

        def sleep(seconds):
            clock[0] += seconds

        with mock.patch.object(run_wait, "_sleep", side_effect=sleep), \
                mock.patch.object(run_wait, "_clock", side_effect=lambda: clock[0]):
            resp = self._call(script, "?sync=1")
        self.assertEqual(resp.status_code, 202, resp.content)
        data = resp.json()
        run = Run.objects.get(script=script)
        self.assertEqual((data["run_id"], data["status"], data["wait"]), (str(run.id), "pending", "timeout"))
        self.assertEqual(resp["Location"], f"/api/v1/runs.get?run={run.id}")
        self.assertIn("runs.get", data["next"])
        self.assertEqual(clock[0], 103.0)  # waited the whole budget, no more
        self.assertEqual((run.response_code, run.task_id), (202, "t22-task"))
        self.assertTrue(self._slots_free())

    def test_with_every_slot_taken_the_call_gets_202_at_once(self):
        script = self._webhook_script()
        with ExitStack() as stack:
            for _ in range(4):
                self.assertTrue(stack.enter_context(run_wait.reserve(30)).held)
            with mock.patch.object(run_wait, "_sleep") as sleep:
                resp = self._call(script, "?sync=1")
            sleep.assert_not_called()
        self.assertEqual((resp.status_code, resp.json()["wait"]), (202, "busy"))
        run = Run.objects.get(script=script)
        self.assertEqual((run.response_code, run.task_id), (202, "t22-task"))  # queued all the same

    def test_nine_concurrent_sync_calls_four_wait_and_five_get_202_at_once(self):
        """Each waiting call makes the next call while it waits, so up to four
        waits are really in flight, holding their slots, when calls 5-9 come."""
        script = self._webhook_script()
        responses = {}  # call number -> response
        waiting = []  # run ids of calls holding a slot, innermost last
        started = [0]
        sleeps_during = {}

        def call():
            started[0] += 1
            number = started[0]
            sleeps_during[number] = len(sleep_log)
            responses[number] = self._call(script, "?sync=1")
            sleeps_during[number] = len(sleep_log) - sleeps_during[number]

        sleep_log = []

        def sleep(_seconds):
            sleep_log.append(waiting[-1])
            if started[0] < 9:
                if len(waiting) < 4:
                    call()
                else:
                    while started[0] < 9:
                        call()
            Run.objects.filter(pk=waiting[-1]).update(
                status=Run.Status.SUCCESS, ended_at=timezone.now(), result=_envelope(200, body="done")
            )

        real_wait = run_wait.Waiter.wait

        def wait(waiter, run):
            if not waiter.held:
                return real_wait(waiter, run)
            waiting.append(run.pk)
            try:
                return real_wait(waiter, run)
            finally:
                waiting.pop()

        with mock.patch.object(run_wait, "_sleep", side_effect=sleep), \
                mock.patch.object(run_wait.Waiter, "wait", autospec=True, side_effect=wait):
            call()

        self.assertEqual(started[0], 9)
        for number in (1, 2, 3, 4):
            self.assertEqual((responses[number].status_code, responses[number].content), (200, b"done"))
        for number in (5, 6, 7, 8, 9):
            self.assertEqual(responses[number].status_code, 202, number)
            self.assertEqual(responses[number].json()["wait"], "busy")
            self.assertEqual(sleeps_during[number], 0)  # answered without waiting
        codes = sorted(Run.objects.filter(script=script).values_list("response_code", flat=True))
        self.assertEqual(codes, [200] * 4 + [202] * 5)
        self.assertTrue(self._slots_free())

    def test_no_worker_alive_answers_503_and_makes_no_run(self):
        script = self._webhook_script()
        GlobalSettings.objects.update(worker_heartbeat_at=timezone.now() - timedelta(hours=1))
        resp = self._call(script, "?sync=1")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual((resp.json()["status"], resp["Retry-After"]), ("unavailable", "60"))
        self.assertFalse(Run.objects.filter(script=script).exists())
        # An async call is still queued, workers or not.
        self.assertEqual(self._call(script).status_code, 200)

    def test_maintenance_answers_503_with_retry_after_and_frees_the_slot(self):
        script = self._webhook_script()
        now = timezone.now()
        MaintenanceWindow.objects.create(
            name="Upgrade", kind=MaintenanceWindow.Kind.ONE_OFF,
            starts_at=now - timedelta(minutes=5), ends_at=now + timedelta(minutes=30),
            blocks=list(MaintenanceWindow.DEFAULT_BLOCKS),
        )
        with mock.patch.object(run_wait, "_sleep") as sleep:
            resp = self._call(script, "?sync=1")
        sleep.assert_not_called()
        self.assertEqual((resp.status_code, resp.json()["status"]), (503, "maintenance"))
        self.assertTrue(int(resp["Retry-After"]) > 0)
        self.assertEqual(Run.objects.get(script=script).response_code, 503)
        self.assertTrue(self._slots_free())

    def test_a_skip_policy_skip_answers_409_when_sync_and_200_when_async(self):
        script = self._webhook_script(concurrency_policy="skip")
        Run.objects.create(script=script, workspace=self.ws, status=Run.Status.RUNNING)
        resp = self._call(script, "?sync=1")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual((resp.json()["status"], resp.json()["reason"]), ("skipped", "already_running"))
        run = Run.objects.get(pk=resp.json()["run_id"])
        self.assertEqual((run.response_code, run.task_id or None, run.is_skipped), (409, None, True))
        resp = self._call(script)
        self.assertEqual((resp.status_code, resp.json()["status"]), (200, "skipped"))
        self.assertTrue(self._slots_free())

    def test_refusals_before_the_run_take_no_slot(self):
        # Signature, disabled, body: the same order as an async call; none
        # of them waits or creates a run.
        signed = self._webhook_script(webhook_signature=Script.WebhookSignature.GITHUB,
                                      webhook_mode=Script.WebhookMode.SYNC)
        signed.set_webhook_signature_secret("s")
        signed.save()
        disabled = self._webhook_script(is_enabled=False, webhook_mode=Script.WebhookMode.SYNC)
        plain = self._webhook_script(webhook_mode=Script.WebhookMode.SYNC)
        with mock.patch.object(run_wait, "reserve") as reserve:
            self.assertEqual(self._call(signed).status_code, 401)
            self.assertEqual(self._call(disabled).status_code, 403)
            with override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=10):
                self.assertEqual(self._call(plain, data="x" * 50, content_type="text/plain").status_code, 413)
        reserve.assert_not_called()
        self.assertFalse(Run.objects.filter(script__in=[signed, disabled, plain]).exists())

    def test_a_failure_to_queue_answers_500_and_frees_the_slot(self):
        script = self._webhook_script()
        self.async_task.side_effect = RuntimeError("broker down")
        resp = self._call(script, "?sync=1")
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(Run.objects.get(script=script).response_code, 500)
        self.assertTrue(self._slots_free())

    def test_recent_deliveries_show_the_codes_sync_calls_got(self):
        user = User.objects.create(email="t22-owner@example.com")
        WorkspaceMembership.objects.update_or_create(
            user=user, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_OWNER})
        self.client.force_login(user)
        script = self._webhook_script()
        with self.finish_on_sleep(result=_envelope(418, body="teapot")):
            self._call(script, "?sync=1")
        page = self.client.get(reverse("cpanel:script_detail", args=[script.pk])).content.decode()
        deliveries = page[page.index('id="webhook-deliveries"'):]
        self.assertIn(">418<", deliveries.replace(" ", "").replace("\n", ""))


class EndToEndTests(LiveServerTestCase):
    """A real script's respond() goes through the real loopback endpoint, and
    the sync call gets it back (the worker runs inline, in the request)."""

    host = "127.0.0.1"  # the internal API answers loopback only

    def setUp(self):
        _setup_wizard_off(self)
        ws = Workspace.get_default() or Workspace.objects.create(name="Default", is_default=True)
        env = Environment.objects.create(name="t22e2e", path=f"t22e2e{uuid.uuid4().hex[:6]}")
        code = (
            "import os, urllib.parse, pyrunner_run\n"
            "event = pyrunner_run.event()\n"
            "form = urllib.parse.parse_qs(event['body'])\n"
            "pyrunner_run.set_result({'command': form['command'][0]})\n"
            "pyrunner_run.respond(200, {'response_type': 'in_channel',\n"
            "    'text': 'Weather in ' + form['text'][0] + ' (key ' + os.environ['API_KEY'] + ')'},\n"
            "    {'Cache-Control': 'no-store'})\n"
        )
        self.script = Script.objects.create(
            name="t22 slash", environment=env, workspace=ws, code=code,
            webhook_token="tok_" + uuid.uuid4().hex, webhook_mode=Script.WebhookMode.SYNC,
        )

    @mock.patch("core.executor._validate_environment", return_value=sys.executable)
    def test_a_slash_command_gets_the_scripts_reply(self, _val):
        from core.tasks import execute_run_task

        def run_now(_func, run_id, webhook_data, **_kwargs):
            execute_run_task(run_id, webhook_data)
            return "t22-inline"

        url = reverse("webhook_trigger", args=[self.script.webhook_token])
        with override_settings(PYRUNNER_INTERNAL_BASE_URL=self.live_server_url, CACHES=LOCMEM,
                               SYNC_WAIT_SLOTS=4), \
                _secrets(), \
                mock.patch("core.executor.ClaudeService.get_script_env", return_value={}), \
                mock.patch.object(run_wait, "workers_alive", return_value=True), \
                mock.patch("core.tasks.async_task", side_effect=run_now):
            resp = self.client.post(url, data="command=%2Fweather&text=Paris",
                                    content_type="application/x-www-form-urlencoded")
        run = Run.objects.get(script=self.script)
        self.assertEqual(resp.status_code, 200, (resp.content, run.stderr))
        self.assertEqual(resp.json(), {"response_type": "in_channel",
                                       "text": "Weather in Paris (key [API_KEY:MASKED])"})
        self.assertEqual(resp["Cache-Control"], "no-store")
        self.assertEqual(run.status, Run.Status.SUCCESS)
        self.assertEqual(run.result["command"], "/weather")
        self.assertEqual(run.response_code, 200)


# --------------------------------------------------------------------------- #
# Console, backups, forks
# --------------------------------------------------------------------------- #


class ConsoleTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create(email="t22-settings@example.com")
        WorkspaceMembership.objects.update_or_create(
            user=self.user, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_OWNER}
        )
        self.client.force_login(self.user)
        self.script = self._webhook_script()
        self.url = reverse("cpanel:webhook_settings", args=[self.script.pk])

    def _save(self, mode):
        return self.client.post(self.url, {
            "webhook_mode": mode, "webhook_signature": "none", "webhook_signature_encoding": "hex",
            "webhook_signature_tolerance": 300, "webhook_rate_per_minute": 60, "webhook_rate_burst": 30,
        })

    def test_the_settings_page_sets_the_mode(self):
        page = self.client.get(self.url).content.decode()
        self.assertIn('name="webhook_mode"', page)
        self.assertIn("?sync=1", page)
        resp = self._save("sync")
        self.assertRedirects(resp, reverse("cpanel:script_detail", args=[self.script.pk]))
        self.script.refresh_from_db()
        self.assertEqual(self.script.webhook_mode, "sync")
        resp = self._save("later")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("webhook_mode", resp.context["form"].errors)
        self.script.refresh_from_db()
        self.assertEqual(self.script.webhook_mode, "sync")

    def test_the_card_shows_the_mode_and_both_curl_examples(self):
        detail = reverse("cpanel:script_detail", args=[self.script.pk])
        page = self.client.get(detail).content.decode()
        summary = page[page.index('id="webhook-settings-summary"'):page.index('id="curl-example"')]
        self.assertIn("Async", summary)
        self.assertIn("Reply, signature and limits", summary)
        example = page[page.index('id="curl-example"'):]
        example = example[:example.index("</pre>")]
        self.assertIn(f"{self.script.webhook_token}/?sync=1", example)
        Script.objects.filter(pk=self.script.pk).update(webhook_mode="sync")
        page = self.client.get(detail).content.decode()
        summary = page[page.index('id="webhook-settings-summary"'):page.index('id="curl-example"')]
        self.assertIn("Sync", summary)
        self.assertNotIn("?sync=1", page)  # every call waits already


class BackupAndForkTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create(email="t22-admin@example.com", is_superuser=True)

    def _restore(self, data):
        result = BackupService.restore_backup(data, restore_runs=False, current_user=self.user)
        self.assertTrue(result["success"], result)

    def test_backup_round_trips_the_mode(self):
        script = self._webhook_script(webhook_mode=Script.WebhookMode.SYNC)
        data = BackupService.create_backup(include_runs=False)
        row = next(s for s in data["scripts"] if s["id"] == str(script.pk))
        self.assertEqual(row["webhook_mode"], "sync")
        Script.objects.filter(pk=script.pk).update(webhook_mode="async")
        self._restore(data)
        self.assertEqual(Script.objects.get(pk=script.pk).webhook_mode, "sync")

    def test_an_older_or_hand_edited_backup_restores_async(self):
        older = self._webhook_script(webhook_mode=Script.WebhookMode.SYNC)
        edited = self._webhook_script(webhook_mode=Script.WebhookMode.SYNC)
        data = BackupService.create_backup(include_runs=False)
        rows = {row["id"]: row for row in data["scripts"]}
        rows[str(older.pk)].pop("webhook_mode")
        rows[str(edited.pk)]["webhook_mode"] = ["sync"]
        self._restore(data)
        for script in (older, edited):
            self.assertEqual(Script.objects.get(pk=script.pk).webhook_mode, "async")
        self.assertEqual(restored_settings({})[0]["webhook_mode"], "async")
        self.assertEqual(restored_settings({"webhook_mode": "SYNC"})[0]["webhook_mode"], "async")

    def test_a_fork_starts_async(self):
        script = self._webhook_script(webhook_mode=Script.WebhookMode.SYNC)
        fork = script_service.fork_script(script, user=self.user)
        self.assertEqual(fork.webhook_mode, "async")
