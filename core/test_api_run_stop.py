"""
Run and stop scripts over the API, with a sync wait (2.5 Stage 2, ticket T13).

What this pins, roughly in order of how much it would hurt to get wrong:

- A sync wait can't eat the web threads: with every slot taken (default 4,
  shared through the cache) the next ``wait`` call is queued and answered at
  once with its run id (202, ``wait: busy``); five concurrent waits = four
  wait, one doesn't. A slot is released on every exit path.
- With no worker alive a ``wait`` call fails fast (503 ``workers_unavailable``
  + Retry-After) and creates no run; without ``wait`` the run is queued.
- ``scripts.run`` checks the inputs against the schema (400, no run), honours
  the token's script binding, the disabled switch, maintenance windows (503 +
  Retry-After, as a webhook) and the concurrency policy, and answers 200 with
  the result when the run ended within the budget, else 202 with the run id.
- ``runs.stop`` is the run page's Stop: graceful first, a second call kills.
- ``scripts:run`` / ``runs:stop`` are run-class permissions (never on a URL
  token), each limited to 30 calls a minute (429 + Retry-After).
- Every non-read call and every failure lands in ``ApiCall`` (REST and MCP),
  recording never breaks the call, rows go after 30 days (daily housekeeping
  task), and the token's Activity page lists them.
"""

import threading
import uuid
from contextlib import ExitStack
from datetime import timedelta
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_q.models import Schedule

from core.api.mcp import LEGACY
from core.api.registry import registry
from core.models import (
    ApiCall,
    APIToken,
    Environment,
    GlobalSettings,
    MaintenanceWindow,
    Run,
    Script,
    User,
    Workspace,
)
from core.services import api_audit, run_wait
from core.services.schedule_service import HOUSEKEEPING_SCHEDULE_NAME, ScheduleService
from core.tasks import daily_housekeeping_task
from core.test_plugin_api import _mock_setup

ACCEPT = "application/json, text/event-stream"
RUN_ALL = ["scripts:read", "scripts:run", "runs:read", "runs:stop"]
SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string", "default": "Beirut"}, "days": {"type": "integer"}},
    "required": ["days"],
    "additionalProperties": False,
}
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                      "LOCATION": "t13-sync-wait"}}


class DeclarationTests(SimpleTestCase):
    def test_run_and_stop_are_run_class_with_their_own_permissions(self):
        run, stop = registry.get("scripts.run"), registry.get("runs.stop")
        self.assertEqual((run.access, run.permission, run.rate), ("run", "scripts:run", (30, 60)))
        self.assertEqual((stop.access, stop.permission, stop.rate), ("run", "runs:stop", (30, 60)))
        self.assertEqual(run.done, "is_finished")
        # Run permissions: no URL token may hold them, and they aren't write
        # permissions (the writes switch never takes them away).
        for permission in ("scripts:run", "runs:stop"):
            self.assertNotIn(permission, registry.read_permissions())
            self.assertNotIn(permission, registry.write_permissions())
            self.assertFalse(APIToken.url_permissions_ok([permission]))
        offered = {p for p, _ in APIToken.API_PERMISSIONS}
        self.assertTrue({"scripts:run", "runs:stop"} <= offered)

    def test_budget_is_capped_and_leaves_the_caller_time_to_reply(self):
        self.assertEqual(run_wait.budget(), 55)
        self.assertEqual(run_wait.budget(10), 10)
        self.assertEqual(run_wait.budget(500), 55)
        self.assertEqual(run_wait.budget(55, remaining=20), 20 - run_wait.REPLY_MARGIN_SECONDS)
        self.assertEqual(run_wait.budget(55, remaining=1), 0)
        # A short GUNICORN_TIMEOUT (here 30 s) would kill the thread mid-wait.
        with override_settings(SYNC_WAIT_MAX_SECONDS=20):
            self.assertEqual((run_wait.budget(), run_wait.budget(40), run_wait.budget(5)),
                             (20, 20, 5))


# The threads here stand in for several web processes: the per-process cap
# (SYNC_WAIT_SLOTS_PER_PROCESS) is lifted except where a test is about it.
@override_settings(CACHES=LOCMEM, SYNC_WAIT_SLOTS=4, SYNC_WAIT_SLOTS_PER_PROCESS=4)
class SlotTests(SimpleTestCase):
    """The in-flight cap, with real threads (no database involved)."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        patcher = mock.patch.object(run_wait, "workers_alive", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _taken(self):
        from django.core.cache import cache

        return [i for i in range(8) if cache.get(run_wait.SLOT_KEY.format(i)) is not None]

    def test_five_concurrent_waits_four_hold_a_slot_one_does_not(self):
        held, ready, release = [], threading.Semaphore(0), threading.Event()

        def caller():
            with run_wait.reserve(30) as waiter:
                held.append(waiter.held)
                ready.release()
                release.wait(10)

        threads = [threading.Thread(target=caller) for _ in range(5)]
        for thread in threads:
            thread.start()
        for _ in threads:
            self.assertTrue(ready.acquire(timeout=10))
        self.assertEqual(sorted(held), [False, True, True, True, True])
        self.assertEqual(len(self._taken()), 4)
        release.set()
        for thread in threads:
            thread.join(10)
        self.assertEqual(self._taken(), [])

    @override_settings(SYNC_WAIT_SLOTS_PER_PROCESS=3)
    def test_one_process_holds_at_most_its_threads_minus_one_waits(self):
        """2.5 test pass, F5: gunicorn's threads belong to one process each.
        With all of a process's threads waiting, the requests it accepted
        hang, the loopback calls of the waited-for runs among them; so a
        process keeps one thread free even while instance slots are left."""
        held, ready, release = [], threading.Semaphore(0), threading.Event()

        def caller():
            with run_wait.reserve(30) as waiter:
                held.append(waiter.held)
                ready.release()
                release.wait(10)

        threads = [threading.Thread(target=caller) for _ in range(4)]
        try:
            for thread in threads:
                thread.start()
            for _ in threads:
                self.assertTrue(ready.acquire(timeout=10))
            self.assertEqual(sorted(held), [False, True, True, True])
            self.assertEqual(len(self._taken()), 3)  # the fourth instance slot stays free
        finally:
            release.set()
            for thread in threads:
                thread.join(10)
        self.assertEqual(self._taken(), [])
        with run_wait.reserve(30) as waiter:  # the process's count went back down
            self.assertTrue(waiter.held)

    @override_settings(SYNC_WAIT_SLOTS_PER_PROCESS=1)
    def test_a_process_slot_is_given_back_when_no_instance_slot_is_free(self):
        with override_settings(SYNC_WAIT_SLOTS=0), run_wait.reserve(30) as waiter:
            self.assertFalse(waiter.held)
        with run_wait.reserve(30) as waiter:
            self.assertTrue(waiter.held)

    def test_a_slot_is_released_when_the_block_raises(self):
        with self.assertRaises(RuntimeError):
            with run_wait.reserve(30) as waiter:
                self.assertTrue(waiter.held)
                raise RuntimeError("boom")
        self.assertEqual(self._taken(), [])

    def test_release_leaves_a_slot_someone_else_took_over(self):
        from django.core.cache import cache

        key = run_wait.SLOT_KEY.format(0)
        cache.set(key, "someone-else", 60)
        run_wait._release_slot(key, "me")
        self.assertEqual(cache.get(key), "someone-else")

    def test_no_slots_configured_means_never_wait(self):
        with override_settings(SYNC_WAIT_SLOTS=0), run_wait.reserve(30) as waiter:
            self.assertFalse(waiter.held)

    def test_workers_down_refuses_before_taking_a_slot(self):
        with mock.patch.object(run_wait, "workers_alive", return_value=False):
            with self.assertRaises(run_wait.WorkersUnavailable):
                with run_wait.reserve(30):
                    pass
        self.assertEqual(self._taken(), [])


class _Fixture:
    def setUp(self):
        _mock_setup(self)
        self.ws = Workspace.get_default()
        self.other = Workspace.objects.create(name="Other T13 workspace")
        self.env = Environment.objects.create(name="t13env", path=f"t13env{uuid.uuid4().hex[:6]}")
        self.a = self._script("Alpha", inputs_schema=SCHEMA)
        self.b = self._script("Beta")
        self.raw, self.token = self._token(RUN_ALL)
        self.bound_raw, self.bound = self._token(RUN_ALL, script=self.a)
        settings = GlobalSettings.get_settings()
        settings.worker_heartbeat_at = timezone.now()
        settings.save(update_fields=["worker_heartbeat_at"])
        queued = mock.patch("core.tasks.async_task", return_value="t13-task")
        self.async_task = queued.start()
        self.addCleanup(queued.stop)

    def _script(self, name, **kwargs):
        kwargs.setdefault("workspace", self.ws)
        return Script.objects.create(name=name, environment=self.env, code="print(1)", **kwargs)

    def _token(self, permissions, **kwargs):
        raw = APIToken.generate_token(scope="api", kind="bearer")
        token = APIToken.objects.create(
            name=f"t{uuid.uuid4().hex[:6]}", scope="api", token=raw,
            workspace=kwargs.pop("workspace", self.ws), permissions=permissions, **kwargs,
        )
        return raw, token

    def post(self, slug, params=None, raw=None):
        return self.client.post("/api/v1/" + slug, params or {}, content_type="application/json",
                                HTTP_AUTHORIZATION="Bearer " + (raw or self.raw))

    def rpc(self, method, params=None, raw=None):
        return self.client.post(
            "/mcp", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
            content_type="application/json", HTTP_ACCEPT=ACCEPT,
            HTTP_MCP_PROTOCOL_VERSION=LEGACY[0],
            HTTP_AUTHORIZATION="Bearer " + (raw or self.raw),
        )

    def finish_on_sleep(self, **fields):
        """A worker stand-in: the wait's first sleep ends the run (``fields``)."""

        def sleep(_seconds):
            Run.objects.filter(status__in=[Run.Status.PENDING, Run.Status.RUNNING]).update(
                status=Run.Status.SUCCESS, ended_at=timezone.now(), **fields
            )

        return mock.patch.object(run_wait, "_sleep", side_effect=sleep)


class RunTests(_Fixture, TestCase):
    def test_run_without_wait_queues_and_answers_202_with_the_run_id(self):
        response = self.post("scripts.run", {"script": str(self.a.id), "inputs": {"days": 3}})
        self.assertEqual(response.status_code, 202, response.content)
        data = response.json()["data"]
        run = Run.objects.get(pk=data["run_id"])
        self.assertEqual((data["status"], data["is_finished"], data["wait"]),
                         ("pending", False, None))
        self.assertIn("runs.get", data["next"])
        self.assertEqual(data["script"], {"id": str(self.a.id), "name": "Alpha"})
        # Validated with the default filled in, compared as a dict (jsonb order).
        self.assertEqual(run.inputs, {"city": "Beirut", "days": 3})
        self.assertEqual(run.trigger_type, Run.TriggerType.API)
        self.assertEqual(run.task_id, "t13-task")
        self.assertIsNone(run.triggered_by)
        self.assertEqual({k: run.event[k] for k in ("kind", "token_id", "via")},
                         {"kind": "api", "token_id": str(self.token.pk), "via": "rest"})

    def test_bad_inputs_are_refused_and_no_run_is_created(self):
        for inputs in ({}, {"days": "three"}, {"days": 1, "unknown": 2}):
            response = self.post("scripts.run", {"script": str(self.a.id), "inputs": inputs})
            self.assertEqual(response.status_code, 400, inputs)
            self.assertEqual(response.json()["error"]["code"], "invalid_input")
            self.assertIn("days" if "unknown" not in inputs else "unknown",
                          response.json()["error"]["message"])
        self.assertIn("doesn't declare", self.post(
            "scripts.run", {"script": str(self.b.id), "inputs": {"x": 1}}).json()["error"]["message"])
        self.assertFalse(Run.objects.exists())

    def test_scope_binding_disabled_and_parameter_checks(self):
        foreign = self._script("Foreign", workspace=self.other)
        for script, raw in ((foreign, None), (self.b, self.bound_raw), (uuid.uuid4(), None)):
            response = self.post("scripts.run", {"script": str(getattr(script, "id", script))},
                                 raw=raw)
            self.assertEqual(response.status_code, 404, script)
        # A bound token may leave the script out.
        response = self.post("scripts.run", {"inputs": {"days": 1}}, raw=self.bound_raw)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["data"]["script"]["id"], str(self.a.id))
        self.b.is_enabled = False
        self.b.save(update_fields=["is_enabled"])
        response = self.post("scripts.run", {"script": str(self.b.id)})
        self.assertEqual((response.status_code, response.json()["error"]["code"]),
                         (409, "script_disabled"))
        response = self.post("scripts.run", {"script": str(self.a.id), "wait_seconds": 5,
                                             "inputs": {"days": 1}})
        self.assertEqual(response.status_code, 400)
        response = self.post("scripts.run", {"script": str(self.a.id), "wait": True,
                                             "wait_seconds": 56})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Run.objects.count(), 1)

    def test_permissions_and_post_only(self):
        raw, _ = self._token(["scripts:read", "runs:read"])
        for slug, params in (("scripts.run", {"script": str(self.b.id)}),
                             ("runs.stop", {"run": str(uuid.uuid4())})):
            response = self.post(slug, params, raw=raw)
            self.assertEqual((response.status_code, response.json()["error"]["code"]),
                             (403, "forbidden"))
            response = self.client.get("/api/v1/" + slug, HTTP_AUTHORIZATION="Bearer " + self.raw)
            self.assertEqual(response.status_code, 405)
        self.assertFalse(Run.objects.exists())

    def test_wait_returns_the_result_when_the_run_ends_in_time(self):
        with self.finish_on_sleep(result={"total": 42}, exit_code=0):
            response = self.post("scripts.run", {"script": str(self.a.id), "wait": True,
                                                 "inputs": {"days": 2}})
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual((data["status"], data["is_finished"], data["wait"], data["result"]),
                         ("success", True, "finished", {"total": 42}))
        self.assertNotIn("next", data)

    def test_wait_past_the_budget_answers_202_and_the_run_keeps_going(self):
        clock = [0.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        with mock.patch.object(run_wait, "_sleep", side_effect=sleep), \
                mock.patch.object(run_wait, "_clock", side_effect=lambda: clock[0]):
            response = self.post("scripts.run", {"script": str(self.a.id), "wait": True,
                                                 "wait_seconds": 7, "inputs": {"days": 2}})
        self.assertEqual(response.status_code, 202, response.content)
        data = response.json()["data"]
        self.assertEqual((data["status"], data["wait"]), ("pending", "timeout"))
        self.assertIn("runs.get", data["next"])
        # 0.5 s first, backing off to 2 s, never sleeping past the budget.
        self.assertEqual(sleeps[:3], [0.5, 0.75, 1.125])
        self.assertLessEqual(max(sleeps), run_wait.POLL_MAX_SECONDS)
        self.assertAlmostEqual(sum(sleeps), 7)

    def test_no_worker_alive_fails_fast_without_creating_a_run(self):
        settings = GlobalSettings.get_settings()
        settings.worker_heartbeat_at = timezone.now() - timedelta(minutes=10)
        settings.save(update_fields=["worker_heartbeat_at"])
        response = self.post("scripts.run", {"script": str(self.b.id), "wait": True})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "workers_unavailable")
        self.assertEqual(response["Retry-After"], "60")
        self.assertFalse(Run.objects.exists())
        # Without wait the run is queued, as Run now would.
        self.assertEqual(self.post("scripts.run", {"script": str(self.b.id)}).status_code, 202)

    @override_settings(SYNC_WAIT_SLOTS=4, SYNC_WAIT_SLOTS_PER_PROCESS=4)
    def test_with_every_slot_taken_the_next_wait_answers_at_once(self):
        with ExitStack() as stack:
            for _ in range(4):
                self.assertTrue(stack.enter_context(run_wait.reserve(30)).held)
            with mock.patch.object(run_wait, "_sleep") as sleep:
                response = self.post("scripts.run", {"script": str(self.b.id), "wait": True})
            sleep.assert_not_called()
            self.assertEqual(response.status_code, 202, response.content)
            data = response.json()["data"]
            self.assertEqual((data["wait"], data["status"]), ("busy", "pending"))
            self.assertTrue(Run.objects.filter(pk=data["run_id"], task_id="t13-task").exists())
        # Slots free again: the next call waits.
        with self.finish_on_sleep():
            response = self.post("scripts.run", {"script": str(self.b.id), "wait": True})
        self.assertEqual((response.status_code, response.json()["data"]["wait"]),
                         (200, "finished"))

    def test_slot_is_released_when_the_call_fails_after_taking_it(self):
        window = MaintenanceWindow.objects.create(
            name="Upgrade", kind=MaintenanceWindow.Kind.ONE_OFF, blocks=["api"],
            starts_at=timezone.now() - timedelta(minutes=5),
            ends_at=timezone.now() + timedelta(minutes=30),
        )
        response = self.post("scripts.run", {"script": str(self.a.id), "wait": True,
                                             "inputs": {"days": 1}})
        # As a webhook: 503 + Retry-After, and the refused call is a skipped run.
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "maintenance")
        self.assertIn("Upgrade", response.json()["error"]["message"])
        self.assertTrue(1700 <= int(response["Retry-After"]) <= 1800)
        run = Run.objects.get()
        self.assertEqual((run.status, run.cancel_reason),
                         (Run.Status.CANCELLED, Run.CancelReason.MAINTENANCE))
        self.assertEqual(run_wait._take_slot(5)[0], run_wait.SLOT_KEY.format(0))
        window.delete()

    def test_skip_policy_answers_200_with_the_skipped_run(self):
        self.b.concurrency_policy = Script.ConcurrencyPolicy.SKIP
        self.b.save(update_fields=["concurrency_policy"])
        Run.objects.create(script=self.b, workspace=self.ws, status=Run.Status.RUNNING)
        response = self.post("scripts.run", {"script": str(self.b.id), "wait": True})
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual((data["status"], data["cancel_reason"], data["wait"]),
                         ("cancelled", "concurrency_skip", "finished"))

    def test_mcp_tool_call_runs_the_script(self):
        response = self.rpc("tools/call", {"name": "scripts-run",
                                           "arguments": {"script": str(self.b.id)}})
        result = response.json()["result"]
        self.assertFalse(result["isError"])
        run = Run.objects.get(pk=result["structuredContent"]["run_id"])
        self.assertEqual(run.event["via"], "mcp")
        tools = {t["name"]: t for t in self.rpc("tools/list").json()["result"]["tools"]}
        self.assertFalse(tools["scripts-run"]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["runs-stop"]["annotations"]["readOnlyHint"])

    def test_rate_limit_is_30_a_minute_with_retry_after(self):
        run = Run.objects.create(script=self.b, workspace=self.ws, status=Run.Status.SUCCESS)
        for _ in range(30):
            self.assertEqual(self.post("runs.stop", {"run": str(run.id)}).status_code, 200)
        response = self.post("runs.stop", {"run": str(run.id)})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "rate_limited")
        self.assertTrue(response["Retry-After"])
        # Counted per endpoint: scripts.run still has its own 30.
        self.assertEqual(self.post("scripts.run", {"script": str(self.b.id)}).status_code, 202)

    def test_catalog_and_openapi_mark_the_202(self):
        catalog = self.client.get("/api/v1/_catalog", HTTP_AUTHORIZATION="Bearer " + self.raw)
        entries = {e["slug"]: e for e in catalog.json()["data"]["endpoints"]}
        self.assertEqual(entries["scripts.run"]["access"], "run")
        self.assertEqual(entries["scripts.run"]["rest_accepted"], "202 while `is_finished` is false")
        self.assertNotIn("rest_accepted", entries["runs.stop"])
        document = self.client.get("/api/v1/_openapi.json",
                                   HTTP_AUTHORIZATION="Bearer " + self.raw).json()
        self.assertIn("202", document["paths"]["/scripts.run"]["post"]["responses"])
        self.assertNotIn("get", document["paths"]["/scripts.run"])


class StopTests(_Fixture, TestCase):
    def test_first_stop_is_graceful_the_second_kills(self):
        run = Run.objects.create(script=self.b, workspace=self.ws, status=Run.Status.RUNNING,
                                 started_at=timezone.now(), pid=4242)
        with mock.patch("core.executor_backends.local.signal_process_tree") as signal, \
                mock.patch("core.executor._kill_process_tree") as kill:
            first = self.post("runs.stop", {"run": str(run.id)}).json()["data"]
            signal.assert_called_once_with(4242)
            kill.assert_not_called()
            self.assertEqual((first["changed"], first["status"], first["is_stopping"]),
                             (True, "running", True))
            self.assertIsNotNone(first["stop_requested_at"])
            second = self.post("runs.stop", {"run": str(run.id)}).json()["data"]
            kill.assert_called_once_with(4242)
        self.assertEqual((second["changed"], second["status"], second["is_finished"]),
                         (True, "cancelled", True))

    def test_pending_is_cancelled_and_a_finished_run_is_left_alone(self):
        pending = Run.objects.create(script=self.b, workspace=self.ws, status=Run.Status.PENDING)
        data = self.post("runs.stop", {"run": str(pending.id)}).json()["data"]
        self.assertEqual((data["changed"], data["status"]), (True, "cancelled"))
        data = self.post("runs.stop", {"run": str(pending.id)}).json()["data"]
        self.assertEqual((data["changed"], data["status"]), (False, "cancelled"))

    def test_bound_token_cannot_stop_another_scripts_run(self):
        run = Run.objects.create(script=self.b, workspace=self.ws, status=Run.Status.PENDING)
        response = self.post("runs.stop", {"run": str(run.id)}, raw=self.bound_raw)
        self.assertEqual(response.status_code, 404)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.PENDING)


class AuditTests(_Fixture, TestCase):
    def test_run_stop_and_failures_are_recorded_reads_are_not(self):
        response = self.post("scripts.run", {"script": str(self.b.id)})
        self.client.get("/api/v1/scripts.list", HTTP_AUTHORIZATION="Bearer " + self.raw)
        self.client.get("/api/v1/_catalog", HTTP_AUTHORIZATION="Bearer " + self.raw)
        self.post("runs.stop", {"run": response.json()["data"]["run_id"]})
        self.post("nope.nothing")
        self.client.get("/api/v1/runs.output", {"run": "x"},
                        HTTP_AUTHORIZATION="Bearer " + self.raw)  # no runs:output
        rows = {row.slug: row for row in ApiCall.objects.all()}
        self.assertEqual(set(rows), {"scripts.run", "runs.stop", "nope.nothing", "runs.output"})
        call = rows["scripts.run"]
        self.assertEqual((call.token, call.workspace, call.ok, call.status, call.code),
                         (self.token, self.ws, True, 202, ""))
        self.assertEqual(call.request_id, response["X-Request-ID"])
        self.assertEqual(call.ip, "127.0.0.1")
        self.assertEqual((rows["runs.stop"].ok, rows["runs.stop"].status), (True, 200))
        self.assertEqual((rows["nope.nothing"].ok, rows["nope.nothing"].status,
                          rows["nope.nothing"].code), (False, 404, "not_found"))
        self.assertEqual((rows["runs.output"].status, rows["runs.output"].code),
                         (403, "forbidden"))

    def test_mcp_calls_are_recorded_with_their_rest_status(self):
        self.rpc("tools/list")
        self.rpc("tools/call", {"name": "scripts-run", "arguments": {"script": str(self.b.id)}})
        self.rpc("tools/call", {"name": "runs-stop", "arguments": {"run": str(uuid.uuid4())}})
        self.rpc("tools/call", {"name": "no-such-tool", "arguments": {}})
        rows = list(ApiCall.objects.order_by("created_at", "id").values_list(
            "slug", "ok", "status", "code"))
        self.assertEqual(rows, [
            ("scripts.run", True, 202, ""),
            ("runs.stop", False, 404, "not_found"),
            ("mcp:tools/call", False, 400, "jsonrpc:-32602"),
        ])

    def test_an_audit_failure_never_breaks_the_call(self):
        with mock.patch.object(ApiCall.objects, "create", side_effect=RuntimeError("db down")), \
                self.assertLogs("core.services.api_audit", "ERROR"):
            response = self.post("scripts.run", {"script": str(self.b.id)})
        self.assertEqual(response.status_code, 202)

    def test_revoked_token_keeps_its_rows_until_the_purge(self):
        api_audit.record(self.token, slug="scripts.run", ok=True, status=202)
        self.token.delete()
        row = ApiCall.objects.get()
        self.assertIsNone(row.token_id)
        self.assertEqual(row.workspace, self.ws)

    def test_daily_housekeeping_purges_rows_older_than_30_days(self):
        for days in (31, 29):
            api_audit.record(self.token, slug=f"scripts.run-{days}", ok=True, status=202)
            ApiCall.objects.filter(slug=f"scripts.run-{days}").update(
                created_at=timezone.now() - timedelta(days=days))
        self.assertEqual(daily_housekeeping_task(), {"success": True, "api_calls_deleted": 1})
        self.assertEqual(list(ApiCall.objects.values_list("slug", flat=True)), ["scripts.run-29"])
        Schedule.objects.filter(name=HOUSEKEEPING_SCHEDULE_NAME).delete()
        self.assertTrue(ScheduleService.ensure_housekeeping_schedule())
        self.assertFalse(ScheduleService.ensure_housekeeping_schedule())
        schedule = Schedule.objects.get(name=HOUSEKEEPING_SCHEDULE_NAME)
        self.assertEqual((schedule.func, schedule.schedule_type),
                         ("core.tasks.daily_housekeeping_task", Schedule.DAILY))


class ConsoleTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_superuser(username="t13admin", email="t13@example.com", password="pw-t13-x")
        self.client.force_login(self.user)

    def test_activity_page_lists_this_tokens_calls_only(self):
        api_audit.record(self.token, slug="scripts.run", ok=False, status=503,
                         code="workers_unavailable", ms=12, ip="10.0.0.9", request_id="req-t13")
        api_audit.record(self.bound, slug="runs.stop", ok=True, status=200)
        page = self.client.get(reverse("cpanel:api_token_activity", args=[self.token.pk]))
        self.assertContains(page, "workers_unavailable")
        self.assertContains(page, "req-t13")
        self.assertContains(page, "10.0.0.9")
        self.assertNotContains(page, "runs.stop")
        listing = self.client.get(reverse("cpanel:api_token_list"))
        self.assertContains(listing, reverse("cpanel:api_token_activity", args=[self.token.pk]))

    def test_activity_page_404s_outside_the_workspace_and_for_other_scopes(self):
        _, foreign = self._token(RUN_ALL, workspace=self.other)
        legacy = APIToken.objects.create(name="ds", scope="datastores", workspace=self.ws,
                                         token=APIToken.generate_token())
        for token in (foreign, legacy):
            page = self.client.get(reverse("cpanel:api_token_activity", args=[token.pk]))
            self.assertEqual(page.status_code, 404)

    def test_connect_shows_run_and_stop(self):
        page = self.client.get(reverse("cpanel:connect"))
        self.assertContains(page, "/scripts.run")
        self.assertContains(page, "/runs.stop")
        self.assertContains(page, "<code>scripts-run</code>", html=False)


class HousekeepingScheduleTests(TestCase):
    def test_pause_all_keeps_it_and_resume_restores_it(self):
        ScheduleService.ensure_housekeeping_schedule()
        ScheduleService.pause_all_schedules()
        self.assertTrue(Schedule.objects.filter(name=HOUSEKEEPING_SCHEDULE_NAME).exists())
        Schedule.objects.filter(name=HOUSEKEEPING_SCHEDULE_NAME).delete()
        ScheduleService.resume_all_schedules()
        self.assertTrue(Schedule.objects.filter(name=HOUSEKEEPING_SCHEDULE_NAME).exists())
