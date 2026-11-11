"""
Watchers (2.5 Stage 7, ticket T31): polling triggers.

What this pins, roughly in order of how much it would hurt to get wrong:

- Exactly one run per new item: the first check only marks; later checks
  start a run per new event, oldest first, across ticks, a re-reported event
  and a worker that died mid-check (the claim expires, the seen ring saved
  after every run stops a second run).
- The cursor moves only after every run of the check is queued; a failure,
  a cap cut or a refused input keeps it where it was.
- Claims (a second check while one is going is refused), maintenance
  windows and an unrunnable script skip the check without asking the source,
  the fire cap per hour, doubling backoff up to an hour, auto-disable after
  10 failures with an email to the creator, the adapter timeout.
- The built-in adapters against a local stand-in server: ``http_poll``
  (list path, id path, order, since parameter) and ``rss_new_item`` (RSS 2.0,
  Atom); SSRF guard; config validation from ``config_schema``.
"""

import datetime
import http.server
import json
import threading
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Channel, Environment, GlobalSettings, MaintenanceWindow, Run, Script, Watcher, Workspace, WorkspaceMembership,
)
from core.triggers import registry, runner
from core.triggers.adapters import (
    Adapter, AdapterError, CheckContext, HttpPollAdapter, RssNewItemAdapter, validate_config,
)

User = get_user_model()


def _tasks():
    counter = iter(range(1, 10_000))
    return mock.patch("core.tasks.async_task", side_effect=lambda *a, **k: f"task-{next(counter)}")


class FakeAdapter(Adapter):
    """In-memory source: ``items`` is the source's list (oldest first)."""

    key = "fake"
    title = "Fake source"
    config_schema = {"type": "object", "properties": {"tag": {"type": "string"}}}

    def __init__(self):
        self.items = []
        self.calls = []
        self.error = None
        self.ignore_limit = False

    def check(self, ctx, config, state):
        self.calls.append({"state": state, "limit": ctx.limit, "config": config})
        if self.error:
            raise self.error
        if state is None:
            return [], {"pos": len(self.items)}
        new = self.items[state["pos"]:]
        if ctx.limit is not None and not self.ignore_limit:
            new = new[:ctx.limit]
        events = [{"id": item["id"], "item": item} for item in new]
        return events, {"pos": state["pos"] + len(new)}

    def test(self, ctx, config):
        return f"{len(self.items)} items", self.items[-3:]


class _Fixture(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default() or Workspace.objects.create(name="Default", is_default=True)
        self.env = Environment.objects.create(name="wenv", path=f"wenv{uuid.uuid4().hex[:6]}")
        self.user = User.objects.create(email="watch@example.com", username="watch")
        self.script = Script.objects.create(
            name=f"w{uuid.uuid4().hex[:6]}", environment=self.env, workspace=self.ws, is_enabled=True, code="pass",
        )
        self.fake = FakeAdapter()
        patcher = mock.patch.object(
            registry, "adapters", return_value={**{a.key: a for a in registry.BUILTIN_ADAPTERS}, "fake": self.fake}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _watcher(self, **kwargs):
        kwargs.setdefault("name", "Orders")
        kwargs.setdefault("adapter", "fake")
        kwargs.setdefault("script", self.script)
        return Watcher.objects.create(workspace=self.ws, created_by=self.user, **kwargs)

    def _check(self, watcher):
        with _tasks():
            return runner.check_watcher(watcher.pk)

    def _due(self, watcher):
        Watcher.objects.filter(pk=watcher.pk).update(next_check_at=None)

    def _runs(self):
        return list(Run.objects.filter(script=self.script).order_by("created_at"))


class ExactlyOnceTests(_Fixture):
    def test_first_check_marks_then_each_new_item_starts_one_run(self):
        self.fake.items = [{"id": "a"}, {"id": "b"}]
        watcher = self._watcher()
        first = self._check(watcher)
        self.assertEqual(first.outcome, runner.OK)
        self.assertIn("First check", first.message)
        self.assertEqual(self._runs(), [])

        self.fake.items += [{"id": "c", "title": "Third"}]
        self._due(watcher)
        second = self._check(watcher)
        self.assertEqual((second.outcome, second.runs), (runner.OK, 1))
        runs = self._runs()
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run.trigger_type, Run.TriggerType.WATCHER)
        self.assertIsNone(run.triggered_by)
        self.assertEqual(run.event["kind"], "watcher")
        self.assertEqual(run.event["event_id"], "c")
        self.assertEqual(run.event["item"], {"id": "c", "title": "Third"})
        self.assertEqual(run.event["watcher"], {"id": str(watcher.pk), "name": "Orders", "adapter": "fake"})
        self.assertTrue(run.task_id)

        # Nothing new: no run.
        self._due(watcher)
        self.assertEqual(self._check(watcher).message, "Nothing new.")
        self.assertEqual(len(self._runs()), 1)

        watcher.refresh_from_db()
        self.assertEqual(watcher.state, {"cursor": {"pos": 3}, "seen": ["c"]})
        self.assertEqual([e["id"] for e in watcher.recent_events], ["c"])
        self.assertEqual(watcher.recent_events[0]["run"], str(run.pk))
        self.assertIsNotNone(watcher.last_event_at)
        self.assertIsNone(watcher.checking_since)

    def test_runs_start_oldest_first_with_inputs_from_the_input_map(self):
        self.script.inputs_schema = {
            "type": "object",
            "properties": {"title": {"type": "string"}, "ref": {"type": "string"}, "n": {"type": "integer", "default": 7}},
        }
        self.script.save()
        watcher = self._watcher(input_map={"title": "item.title", "ref": "event_id", "n": "item.missing"})
        self._check(watcher)
        self.fake.items = [{"id": "1", "title": "one"}, {"id": "2", "title": "two"}]
        self._due(watcher)
        self._check(watcher)
        runs = self._runs()
        self.assertEqual([r.inputs for r in runs], [
            {"title": "one", "ref": "1", "n": 7}, {"title": "two", "ref": "2", "n": 7},
        ])

    def test_an_event_reported_again_never_starts_a_second_run(self):
        watcher = self._watcher()
        self._check(watcher)
        self.fake.items = [{"id": "x"}]
        self._due(watcher)
        self._check(watcher)
        # The source reports "x" again (its cursor went back).
        Watcher.objects.filter(pk=watcher.pk).update(state={"cursor": {"pos": 0}, "seen": ["x"]})
        self._due(watcher)
        self._check(watcher)
        self.assertEqual(len(self._runs()), 1)

    def test_a_worker_dying_mid_check_never_starts_a_run_twice(self):
        watcher = self._watcher()
        self._check(watcher)
        self.fake.items = [{"id": "p"}, {"id": "q"}]
        self._due(watcher)

        # The worker dies after queueing both runs, before the cursor is saved.
        class Died(BaseException):
            pass

        with _tasks(), mock.patch.object(runner, "_release", side_effect=Died):
            with self.assertRaises(Died):
                runner.check_watcher(watcher.pk)
        self.assertEqual(len(self._runs()), 2)
        watcher.refresh_from_db()
        self.assertIsNotNone(watcher.checking_since)  # the claim is left behind
        self.assertEqual(watcher.state["cursor"], {"pos": 0})  # cursor not advanced
        self.assertEqual(watcher.seen, ["p", "q"])

        # While the claim is fresh, nobody else checks it.
        self.assertEqual(self._check(watcher).outcome, runner.BUSY)
        self.assertNotIn(watcher.pk, list(runner.due().values_list("pk", flat=True)))
        # After it expires, a restarted worker re-checks: same events, no new runs.
        Watcher.objects.filter(pk=watcher.pk).update(
            checking_since=timezone.now() - datetime.timedelta(seconds=runner.CLAIM_SECONDS + 1)
        )
        self.assertIn(watcher.pk, list(runner.due().values_list("pk", flat=True)))
        result = self._check(watcher)
        self.assertEqual((result.outcome, result.runs), (runner.OK, 0))
        self.assertEqual(len(self._runs()), 2)
        watcher.refresh_from_db()
        self.assertEqual(watcher.state["cursor"], {"pos": 2})
        self.assertIsNone(watcher.checking_since)

    def test_the_seen_ring_keeps_the_last_5000_ids(self):
        watcher = self._watcher()
        old = [f"o{i}" for i in range(Watcher.SEEN_MAX)]
        Watcher.objects.filter(pk=watcher.pk).update(state={"cursor": {"pos": 0}, "seen": old})
        self.fake.items = [{"id": "new"}]
        self._check(watcher)
        watcher.refresh_from_db()
        self.assertEqual(len(watcher.seen), Watcher.SEEN_MAX)
        self.assertEqual(watcher.seen[-1], "new")
        self.assertNotIn("o0", watcher.seen)

    def test_event_log_keeps_the_last_50_newest_first(self):
        watcher = self._watcher(fire_cap_per_hour=100)
        self._check(watcher)
        self.fake.items = [{"id": str(i)} for i in range(55)]
        self._due(watcher)
        self._check(watcher)
        watcher.refresh_from_db()
        self.assertEqual(len(watcher.recent_events), Watcher.EVENT_LOG_MAX)
        self.assertEqual(watcher.recent_events[0]["id"], "54")
        self.assertEqual(watcher.recent_events[-1]["id"], "5")

    def test_tick_checks_due_watchers_only(self):
        due = self._watcher(name="due")
        later = self._watcher(name="later", next_check_at=timezone.now() + datetime.timedelta(minutes=5))
        off = self._watcher(name="off", enabled=False)
        with _tasks():
            result = runner.tick()
        self.assertEqual(result["checked"], 1)
        for watcher, checked in ((due, True), (later, False), (off, False)):
            watcher.refresh_from_db()
            self.assertEqual(watcher.last_checked_at is not None, checked, watcher.name)

    def test_pause_all_stops_the_tick(self):
        from core.models import GlobalSettings

        settings = GlobalSettings.get_settings()
        settings.schedules_paused = True
        settings.save()
        watcher = self._watcher()
        with _tasks():
            self.assertEqual(runner.tick(), {"paused": True, "checked": 0})
        watcher.refresh_from_db()
        self.assertIsNone(watcher.last_checked_at)


class GuardTests(_Fixture):
    def _marked(self, **kwargs):
        watcher = self._watcher(**kwargs)
        self._check(watcher)
        self.fake.items = [{"id": "n1"}]
        self._due(watcher)
        self.fake.calls.clear()
        return watcher

    def test_maintenance_window_skips_the_check_and_keeps_the_state(self):
        watcher = self._marked()
        now = timezone.now()
        MaintenanceWindow.objects.create(
            name="Upgrade", kind=MaintenanceWindow.Kind.ONE_OFF, starts_at=now - datetime.timedelta(minutes=1),
            ends_at=now + datetime.timedelta(hours=1), blocks=["watcher"],
        )
        before = Watcher.objects.get(pk=watcher.pk).state
        result = self._check(watcher)
        self.assertEqual(result.outcome, runner.MAINTENANCE)
        self.assertIn('"Upgrade"', result.message)
        self.assertEqual(self.fake.calls, [])
        watcher.refresh_from_db()
        self.assertEqual(watcher.state, before)
        self.assertEqual(self._runs(), [])
        self.assertIsNone(watcher.checking_since)

    def test_window_not_blocking_watchers_lets_the_check_through(self):
        watcher = self._marked()
        now = timezone.now()
        MaintenanceWindow.objects.create(
            name="Hooks", kind=MaintenanceWindow.Kind.ONE_OFF, starts_at=now - datetime.timedelta(minutes=1),
            ends_at=now + datetime.timedelta(hours=1), blocks=["webhook"],
        )
        self.assertEqual(self._check(watcher).runs, 1)

    def test_new_windows_block_watchers_by_default(self):
        self.assertIn("watcher", MaintenanceWindow.DEFAULT_BLOCKS)
        self.assertEqual(MaintenanceWindow.BLOCK_PHRASES["watcher"], "watcher runs")

    def test_disabled_script_skips_the_check(self):
        watcher = self._marked()
        Script.objects.filter(pk=self.script.pk).update(is_enabled=False)
        result = self._check(watcher)
        self.assertEqual(result.outcome, runner.SCRIPT_UNAVAILABLE)
        self.assertEqual(self.fake.calls, [])
        watcher.refresh_from_db()
        self.assertEqual(watcher.state["cursor"], {"pos": 0})

    def test_concurrency_policy_applies_per_run(self):
        watcher = self._marked()
        Script.objects.filter(pk=self.script.pk).update(concurrency_policy="skip")
        Run.objects.create(script=self.script, workspace=self.ws, status=Run.Status.RUNNING)
        self.assertEqual(self._check(watcher).runs, 1)
        run = Run.objects.get(script=self.script, trigger_type=Run.TriggerType.WATCHER)
        self.assertTrue(run.is_skipped)
        watcher.refresh_from_db()
        self.assertEqual(watcher.seen, ["n1"])  # the event is used up, like a skipped webhook call

    def test_bad_inputs_fail_the_check_and_keep_the_cursor(self):
        self.script.inputs_schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
        self.script.save()
        watcher = self._marked(input_map={"n": "item.id"})
        result = self._check(watcher)
        self.assertEqual(result.outcome, runner.FAILED)
        self.assertIn("inputs refused it", result.message)
        self.assertEqual(self._runs(), [])  # the pending row is deleted
        watcher.refresh_from_db()
        self.assertEqual((watcher.state["cursor"], watcher.error_count), ({"pos": 0}, 1))

    def test_fire_cap_limits_runs_per_hour_and_the_rest_wait(self):
        watcher = self._watcher(fire_cap_per_hour=2)
        self._check(watcher)
        self.fake.items = [{"id": str(i)} for i in range(5)]
        self._due(watcher)
        self.fake.calls.clear()
        self.assertEqual(self._check(watcher).runs, 2)
        self.assertEqual(self.fake.calls[0]["limit"], 2)
        self._due(watcher)
        capped = self._check(watcher)
        self.assertEqual(capped.outcome, runner.CAPPED)
        self.assertEqual(len(self.fake.calls), 1)  # the source isn't asked
        watcher.refresh_from_db()
        self.assertGreater(watcher.next_check_at, timezone.now() + datetime.timedelta(minutes=50))
        # The next hour.
        Watcher.objects.filter(pk=watcher.pk).update(
            cap_window_start=timezone.now() - datetime.timedelta(hours=1, seconds=1), next_check_at=None,
        )
        self.assertEqual(self._check(watcher).runs, 2)
        self.assertEqual([r.event["event_id"] for r in self._runs()], ["0", "1", "2", "3"])

    def test_adapter_ignoring_the_limit_is_cut_and_its_cursor_kept(self):
        watcher = self._watcher(fire_cap_per_hour=2)
        self._check(watcher)
        self.fake.items = [{"id": str(i)} for i in range(5)]
        self.fake.ignore_limit = True
        self._due(watcher)
        result = self._check(watcher)
        self.assertEqual((result.outcome, result.runs), (runner.CAPPED, 2))
        watcher.refresh_from_db()
        self.assertEqual(watcher.state["cursor"], {"pos": 0})
        self.assertEqual(watcher.seen, ["0", "1"])

    def test_failures_back_off_and_turn_the_watcher_off_after_ten(self):
        watcher = self._marked(interval_seconds=60)
        self.fake.error = AdapterError("The source answered HTTP 500.")
        with mock.patch("core.services.notification_service.NotificationService.send_email") as send:
            result = self._check(watcher)
            self.assertEqual(result.outcome, runner.FAILED)
            watcher.refresh_from_db()
            self.assertEqual(watcher.error_count, 1)
            self.assertEqual(watcher.last_error, "The source answered HTTP 500.")
            self.assertEqual(watcher.state["cursor"], {"pos": 0})
            delay = (watcher.next_check_at - watcher.last_checked_at).total_seconds()
            self.assertEqual(delay, 120)
            for _ in range(8):
                self._due(watcher)
                self._check(watcher)
            watcher.refresh_from_db()
            self.assertEqual((watcher.error_count, watcher.enabled), (9, True))
            self.assertEqual((watcher.next_check_at - watcher.last_checked_at).total_seconds(), 3600)
            send.assert_not_called()
            self._due(watcher)
            last = self._check(watcher)
        self.assertIn("turned itself off", last.message)
        watcher.refresh_from_db()
        self.assertFalse(watcher.enabled)
        self.assertIsNotNone(watcher.auto_disabled_at)
        self.assertTrue(watcher.is_auto_disabled)
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["to"], "watch@example.com")
        self.assertIn("HTTP 500", send.call_args.args[1])
        # A success after failures resets the count.
        watcher2 = self._marked()
        Watcher.objects.filter(pk=watcher2.pk).update(error_count=3)
        self.fake.error = None
        self._check(watcher2)
        watcher2.refresh_from_db()
        self.assertEqual((watcher2.error_count, watcher2.last_error), (0, ""))

    def test_backoff_never_exceeds_an_hour_unless_the_interval_does(self):
        w = Watcher(interval_seconds=300)
        self.assertEqual([runner.backoff_delay(w, n).total_seconds() for n in (1, 2, 3, 4)], [600, 1200, 2400, 3600])
        self.assertEqual(runner.backoff_delay(Watcher(interval_seconds=7200), 3).total_seconds(), 7200)

    def test_unknown_adapter_and_missing_connection_are_failures(self):
        watcher = self._watcher(adapter="gone")
        result = self._check(watcher)
        self.assertEqual(result.outcome, runner.FAILED)
        self.assertIn('"gone"', result.message)

    def test_error_text_is_masked_with_the_connection_values(self):
        watcher = self._marked()
        self.fake.error = RuntimeError("token sk-live-abcdef123 was refused")
        with mock.patch.object(runner, "_masks", return_value={"TOKEN": "sk-live-abcdef123"}):
            self._check(watcher)
        watcher.refresh_from_db()
        self.assertNotIn("sk-live-abcdef123", watcher.last_error)

    def test_a_check_that_outlasts_the_timeout_is_a_failure(self):
        release = threading.Event()

        class Slow(Adapter):
            key = "slow"

            def check(self, ctx, config, state):
                release.wait(5)
                return [], None

        try:
            with self.assertRaises(TimeoutError):
                runner._call_adapter(Slow(), CheckContext(timeout=-4.8), {}, None)
        finally:
            release.set()

    def test_a_crash_in_the_runner_frees_the_claim(self):
        watcher = self._marked()
        with _tasks(), mock.patch("core.tasks.queue_script_run", side_effect=RuntimeError("bug")):
            with self.assertRaises(RuntimeError):
                runner.check_watcher(watcher.pk)
        watcher.refresh_from_db()
        self.assertIsNone(watcher.checking_since)

    def test_targets_mirror_the_script(self):
        watcher = self._watcher()
        self.assertEqual(watcher.targets, [str(self.script.pk)])


class ScheduleTests(TestCase):
    def test_watchers_tick_has_its_own_infra_schedule(self):
        from django_q.models import Schedule

        from core.services.schedule_service import INFRA_SCHEDULE_NAMES, WATCHERS_SCHEDULE_NAME, ScheduleService

        self.assertIn(WATCHERS_SCHEDULE_NAME, INFRA_SCHEDULE_NAMES)
        Schedule.objects.filter(name=WATCHERS_SCHEDULE_NAME).delete()
        self.assertTrue(ScheduleService.ensure_watchers_schedule())
        self.assertFalse(ScheduleService.ensure_watchers_schedule())
        row = Schedule.objects.get(name=WATCHERS_SCHEDULE_NAME)
        self.assertEqual((row.func, row.schedule_type, row.minutes), ("core.tasks.watchers_tick_task", "I", 1))


# --------------------------------------------------------------------------- #
# Built-in adapters against a local stand-in


class _Source(http.server.BaseHTTPRequestHandler):
    routes = {}
    requests = []

    def do_GET(self):  # noqa: N802
        type(self).requests.append(self.path)
        path = self.path.split("?")[0]
        status, content_type, body = type(self).routes.get(path, (404, "text/plain", b"nope"))
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=True)
class _ServerTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Source)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        super().tearDownClass()

    def setUp(self):
        _Source.routes = {}
        _Source.requests = []

    def serve(self, path, body, content_type="application/json", status=200):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode() if content_type == "application/json" else body.encode()
        _Source.routes[path] = (status, content_type, body)


class HttpPollTests(_ServerTests):
    adapter = HttpPollAdapter()

    def ctx(self, limit=None):
        return CheckContext(limit=limit, timeout=5)

    def test_new_entries_oldest_first_by_id_path_in_a_nested_list(self):
        self.serve("/orders", {"data": {"items": [{"meta": {"uid": 2}}, {"meta": {"uid": 1}}]}})
        config = {"url": self.base + "/orders", "items_path": "data.items", "id_path": "meta.uid"}
        events, state = self.adapter.check(self.ctx(), config, None)
        self.assertEqual(events, [])
        self.assertEqual(state["ids"], ["1", "2"])
        self.serve("/orders", {"data": {"items": [{"meta": {"uid": 4}}, {"meta": {"uid": 3}}, {"meta": {"uid": 2}}]}})
        events, state = self.adapter.check(self.ctx(), config, state)
        self.assertEqual([e["id"] for e in events], ["3", "4"])
        self.assertEqual(events[0]["item"], {"meta": {"uid": 3}})
        self.assertEqual(state["ids"], ["1", "2", "3", "4"])

    def test_limit_reports_only_that_many_and_keeps_the_rest_for_later(self):
        self.serve("/l", [{"id": 1}])
        config = {"url": self.base + "/l", "newest_first": False}
        _, state = self.adapter.check(self.ctx(), config, None)
        self.serve("/l", [{"id": 1}, {"id": 2}, {"id": 3}])
        events, state = self.adapter.check(self.ctx(limit=1), config, state)
        self.assertEqual([e["id"] for e in events], ["2"])
        events, _ = self.adapter.check(self.ctx(), config, state)
        self.assertEqual([e["id"] for e in events], ["3"])

    def test_since_parameter_carries_the_newest_value(self):
        self.serve("/s", [{"id": "b", "ts": 20}, {"id": "a", "ts": 10}])
        config = {"url": self.base + "/s", "since_param": "after", "since_path": "ts"}
        _, state = self.adapter.check(self.ctx(), config, None)
        self.assertEqual(state["since"], "20")
        self.assertEqual(_Source.requests[-1], "/s")
        self.serve("/s", [{"id": "c", "ts": 30}])
        events, state = self.adapter.check(self.ctx(), config, state)
        self.assertEqual(_Source.requests[-1], "/s?after=20")
        self.assertEqual((len(events), state["since"]), (1, "30"))

    def test_failures_are_adapter_errors_without_the_url(self):
        self.serve("/bad", "<html>", content_type="text/html")
        self.serve("/obj", {"items": {}})
        self.serve("/err", {"x": 1}, status=503)
        cases = [
            ({"url": self.base + "/bad"}, "isn't JSON"),
            ({"url": self.base + "/obj", "items_path": "items"}, 'No JSON list at "items"'),
            ({"url": self.base + "/err?key=s3cret"}, "HTTP 503"),
            ({"url": "ftp://example.com/x"}, "http:// or https://"),
        ]
        for config, text in cases:
            with self.subTest(config=config):
                with self.assertRaises(AdapterError) as caught:
                    self.adapter.check(self.ctx(), {"id_path": "id", **config}, None)
                self.assertIn(text, str(caught.exception))
                self.assertNotIn("s3cret", str(caught.exception))

    @override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=False)
    def test_private_addresses_are_refused_by_the_ssrf_guard(self):
        self.serve("/x", [])
        with self.assertRaises(AdapterError) as caught:
            self.adapter.check(self.ctx(), {"url": self.base + "/x", "id_path": "id"}, None)
        self.assertTrue(str(caught.exception))
        self.assertEqual(_Source.requests, [])

    def test_test_describes_the_list_and_shows_samples_newest_first(self):
        self.serve("/t", [{"id": 9}, {"id": 8}, {"id": 7}, {"id": 6}])
        message, items = self.adapter.test(self.ctx(), {"url": self.base + "/t", "id_path": "id"})
        self.assertIn("Found 4 entries", message)
        self.assertIn("'9'", message)
        self.assertEqual([i["id"] for i in items], [9, 8, 7])


RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Blog</title>
<item><title>Second</title><link>https://example.com/2</link><guid>post-2</guid>
<pubDate>Tue, 10 Nov 2026 10:00:00 GMT</pubDate><description>Two</description><category>news</category></item>
<item><title>First</title><link>https://example.com/1</link><guid>post-1</guid></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Feed</title>
<entry><title>Hello</title><id>urn:uuid:1</id><link rel="alternate" href="https://example.com/hello"/>
<updated>2026-11-10T10:00:00Z</updated><summary>Hi</summary><author><name>Ana</name></author>
<category term="intro"/></entry>
</feed>"""


class RssTests(_ServerTests):
    adapter = RssNewItemAdapter()

    def test_rss_items_new_since_the_mark_oldest_first(self):
        self.serve("/feed", RSS, content_type="application/rss+xml")
        config = {"url": self.base + "/feed"}
        events, state = self.adapter.check(CheckContext(timeout=5), config, None)
        self.assertEqual((events, state), ([], {"ids": ["post-1", "post-2"]}))
        newer = RSS.replace("<item><title>Second", "<item><title>Third</title><guid>post-3</guid></item>"
                                                    "<item><title>Second")
        self.serve("/feed", newer, content_type="application/rss+xml")
        events, state = self.adapter.check(CheckContext(timeout=5), config, state)
        self.assertEqual([e["id"] for e in events], ["post-3"])
        self.assertEqual(events[0]["item"]["title"], "Third")

    def test_rss_and_atom_fields(self):
        from core.triggers.adapters.rss import parse_feed

        second = parse_feed(RSS.encode())[0]
        self.assertEqual(second, {
            "id": "post-2", "title": "Second", "link": "https://example.com/2",
            "published": "Tue, 10 Nov 2026 10:00:00 GMT", "author": "", "summary": "Two", "categories": ["news"],
        })
        entry = parse_feed(ATOM.encode())[0]
        self.assertEqual(
            (entry["id"], entry["link"], entry["author"], entry["categories"], entry["published"]),
            ("urn:uuid:1", "https://example.com/hello", "Ana", ["intro"], "2026-11-10T10:00:00Z"),
        )

    def test_entities_and_non_feeds_are_refused(self):
        from core.triggers.adapters.rss import parse_feed

        bomb = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel/></rss>'
        # Declarations padded past the first 64 KB are refused too.
        padded = b'<?xml version="1.0"?><!DOCTYPE r [<!--' + b"x" * 70000 + b'--><!ENTITY a "aaaa">]><rss/>'
        cases = ((bomb, "entities"), (padded, "entities"), (b"<html/>", "not an RSS"), (b"{}", "isn't an XML feed"))
        for body, text in cases:
            with self.subTest(text=text), self.assertRaises(AdapterError) as caught:
                parse_feed(body)
            self.assertIn(text, str(caught.exception))

    def test_test_names_the_newest_item(self):
        self.serve("/feed", RSS, content_type="application/rss+xml")
        message, items = self.adapter.test(CheckContext(timeout=5), {"url": self.base + "/feed"})
        self.assertIn("Found 2 items", message)
        self.assertIn("'Second'", message)
        self.assertEqual(items[0]["id"], "post-2")


class ConfigTests(TestCase):
    def test_validate_config_against_the_schema(self):
        schema = HttpPollAdapter.config_schema
        cleaned, errors = validate_config(schema, {"url": "https://x.test/a", "id_path": "", "newest_first": "yes",
                                                   "bogus": 1})
        self.assertEqual(cleaned, {"url": "https://x.test/a"})
        self.assertEqual(set(errors), {"newest_first", "bogus"})
        cleaned, errors = validate_config(schema, {})
        self.assertEqual(errors, {"url": "This field is required."})  # id_path has a default

    def test_patterns_from_integration_triggers_are_checked(self):
        from core.integrations.framework import load_triggers

        schema = load_triggers("github")[0].config_schema
        self.assertEqual(validate_config(schema, {"repo": "octo/shop"}), ({"repo": "octo/shop"}, {}))
        self.assertIn("repo", validate_config(schema, {"repo": "not a repo"})[1])

    def test_registry_lists_the_built_ins(self):
        # T32 added the connection-backed ones after them (core.test_watcher_adapters).
        self.assertEqual(list(registry.adapters())[:2], ["http_poll", "rss_new_item"])
        self.assertIsNone(registry.get("nope"))
        self.assertEqual(registry.title("nope"), "nope")


class MigrationTests(TestCase):
    def test_0087_adds_watcher_to_windows_still_on_the_old_defaults_only(self):
        import importlib

        from django.apps import apps

        migration = importlib.import_module("core.migrations.0087_watchers")
        now = timezone.now()
        common = {"kind": MaintenanceWindow.Kind.ONE_OFF, "starts_at": now, "ends_at": now + datetime.timedelta(hours=1)}
        old = MaintenanceWindow.objects.create(name="old", blocks=["scheduled", "webhook", "channel"], **common)
        edited = MaintenanceWindow.objects.create(name="edited", blocks=["webhook", "manual"], **common)
        migration.add_watcher_block(apps, None)
        migration.add_watcher_block(apps, None)  # idempotent
        old.refresh_from_db()
        edited.refresh_from_db()
        self.assertEqual(old.blocks, ["scheduled", "webhook", "channel", "watcher"])
        self.assertEqual(edited.blocks, ["webhook", "manual"])


class ConsoleTests(_Fixture):
    """The Triggers pages: per script, the workspace list, the form, Check now, the event log."""

    def setUp(self):
        super().setUp()
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()
        Environment.objects.filter(pk=self.env.pk).update(is_default=True)
        self.admin = User.objects.create_user(
            username="wadmin@example.com", email="wadmin@example.com", password="x-Pass-12345!",
            is_superuser=True, is_staff=True,
        )
        WorkspaceMembership.ensure(self.admin, self.ws, role=WorkspaceMembership.ROLE_OWNER)
        self.client.force_login(self.admin)

    def _form(self, **overrides):
        data = {"name": "Orders", "script": str(self.script.pk), "interval_seconds": "120",
                "fire_cap_per_hour": "10", "input_map": "", "enabled": "on", "cfg_tag": "x", "action": "save"}
        data.update(overrides)
        return {k: v for k, v in data.items() if v is not None}

    def test_script_triggers_page_lists_schedule_webhook_watchers_and_channels(self):
        self._watcher(name="Feed watch")
        Channel.objects.create(workspace=self.ws, name="Ops bot", provider="telegram",
                               inbound_handler="script", inbound_target_id=self.script.pk)
        Channel.objects.create(workspace=self.ws, name="Other bot", provider="telegram", inbound_handler="pyai")
        resp = self.client.get(reverse("cpanel:script_triggers", args=[self.script.pk]))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        for section in ("trigger-schedule", "trigger-webhook", "trigger-watchers", "trigger-channels"):
            self.assertIn(f'id="{section}"', html)
        self.assertIn("Feed watch", html)
        self.assertIn("Ops bot", html)
        self.assertNotIn("Other bot", html)
        self.assertIn("Turn it on from the script", html)
        self.script.create_webhook_token()
        html = self.client.get(reverse("cpanel:script_triggers", args=[self.script.pk])).content.decode()
        self.assertIn(reverse("cpanel:webhook_settings", args=[self.script.pk]), html)
        # The script page links here.
        detail = self.client.get(reverse("cpanel:script_detail", args=[self.script.pk])).content.decode()
        self.assertIn(reverse("cpanel:script_triggers", args=[self.script.pk]), detail)

    def test_workspace_list_and_sidebar(self):
        self._watcher(name="Feed watch")
        resp = self.client.get(reverse("cpanel:watcher_list"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("Feed watch", html)
        self.assertIn(self.script.name, html)
        self.assertIn(f'href="{reverse("cpanel:watcher_list")}"', html)  # the sidebar entry

    def test_another_workspaces_watcher_is_not_found(self):
        other_ws = Workspace.objects.create(name="Other")
        other_script = Script.objects.create(name="o", environment=self.env, workspace=other_ws, code="pass")
        other = Watcher.objects.create(workspace=other_ws, script=other_script, name="Theirs", adapter="fake")
        for name in ("watcher_detail", "watcher_edit"):
            self.assertEqual(self.client.get(reverse(f"cpanel:{name}", args=[other.pk])).status_code, 404)
        for name in ("watcher_check", "watcher_toggle", "watcher_delete"):
            self.assertEqual(self.client.post(reverse(f"cpanel:{name}", args=[other.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("cpanel:script_triggers", args=[other_script.pk])).status_code, 404)
        self.assertNotIn("Theirs", self.client.get(reverse("cpanel:watcher_list")).content.decode())
        # The form can't point a watcher at another workspace's script either.
        resp = self.client.post(reverse("cpanel:watcher_create") + "?adapter=fake",
                                self._form(script=str(other_script.pk)))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("script", resp.context["form"].errors)
        self.assertFalse(Watcher.objects.filter(workspace=self.ws).exists())

    def test_create_picks_an_adapter_then_saves_its_settings(self):
        url = reverse("cpanel:watcher_create")
        pick = self.client.get(url, {"script": str(self.script.pk)}).content.decode()
        self.assertIn("New entry in a JSON list (HTTP)", pick)
        self.assertIn("?adapter=rss_new_item", pick)
        form = self.client.get(url, {"script": str(self.script.pk), "adapter": "fake"}).content.decode()
        self.assertIn('name="cfg_tag"', form)
        resp = self.client.post(f"{url}?script={self.script.pk}&adapter=fake", self._form())
        watcher = Watcher.objects.get()
        self.assertRedirects(resp, reverse("cpanel:watcher_detail", args=[watcher.pk]))
        self.assertEqual((watcher.adapter, watcher.config, watcher.interval_seconds, watcher.fire_cap_per_hour),
                         ("fake", {"tag": "x"}, 120, 10))
        self.assertEqual(watcher.created_by, self.admin)
        self.assertEqual(watcher.targets, [str(self.script.pk)])
        self.assertIsNone(watcher.state)
        self.assertEqual(self.client.get(resp.url).status_code, 200)

    def test_form_refuses_bad_values(self):
        resp = self.client.post(reverse("cpanel:watcher_create") + "?adapter=http_poll",
                                self._form(cfg_tag=None, interval_seconds="30", cfg_url=""))
        self.assertEqual(resp.status_code, 200)
        errors = resp.context["form"].errors
        self.assertIn("interval_seconds", errors)
        self.assertIn("cfg_url", errors)
        url = reverse("cpanel:watcher_create") + "?adapter=fake"
        resp = self.client.post(url, self._form(input_map='{"title": "item.title"}'))
        self.assertIn("declares no inputs", resp.context["form"].errors["input_map"][0])
        self.script.inputs_schema = {"type": "object", "properties": {"title": {"type": "string"}}}
        self.script.save(update_fields=["inputs_schema"])
        resp = self.client.post(url, self._form(input_map='{"title": "item.title", "nope": "item.x"}'))
        self.assertEqual(resp.context["form"].errors["input_map"], ["Not inputs of the script: nope."])
        resp = self.client.post(url, self._form(input_map='["item.title"]'))
        self.assertIn("input_map", resp.context["form"].errors)
        self.assertFalse(Watcher.objects.exists())
        self.client.post(url, self._form(input_map='{"title": "item.title"}'))
        self.assertEqual(Watcher.objects.get().input_map, {"title": "item.title"})

    def test_editing_the_source_resets_the_state_and_a_rename_keeps_it(self):
        watcher = self._watcher(config={"tag": "x"}, state={"cursor": {"pos": 3}, "seen": ["a"]})
        url = reverse("cpanel:watcher_edit", args=[watcher.pk])
        page = self.client.get(url).content.decode()
        self.assertIn('value="x"', page)  # the stored setting is shown
        self.assertIn('value="Orders"', page)
        self.client.post(url, self._form(name="Renamed"))
        watcher.refresh_from_db()
        self.assertEqual((watcher.name, watcher.state), ("Renamed", {"cursor": {"pos": 3}, "seen": ["a"]}))
        self.client.post(url, self._form(cfg_tag="y"))
        watcher.refresh_from_db()
        self.assertEqual(watcher.config, {"tag": "y"})
        self.assertIsNone(watcher.state)

    def test_test_button_asks_the_source_and_saves_nothing(self):
        self.fake.items = [{"id": "a", "token": "sk-live-abcdef123"}]
        url = reverse("cpanel:watcher_create") + "?adapter=fake"
        with mock.patch.object(runner, "_masks", return_value={"TOKEN": "sk-live-abcdef123"}):
            resp = self.client.post(url, self._form(action="test"))
        self.assertEqual(resp.status_code, 200)
        result = resp.context["test_result"]
        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "1 items")
        html = resp.content.decode()
        self.assertIn('id="watcher-test-result"', html)
        self.assertNotIn("sk-live-abcdef123", html)
        self.assertFalse(Watcher.objects.exists())
        with mock.patch.object(self.fake, "test", side_effect=AdapterError("The source answered 500.")):
            resp = self.client.post(url, self._form(action="test"))
        self.assertEqual(resp.context["test_result"]["message"], "The source answered 500.")
        self.assertFalse(Watcher.objects.exists())

    def test_check_now_runs_one_check_and_the_event_log_links_its_runs(self):
        watcher = self._watcher()
        check = reverse("cpanel:watcher_check", args=[watcher.pk])
        with _tasks():
            resp = self.client.post(check, follow=True)
        self.assertContains(resp, "First check: marked where the source is.")
        self.fake.items = [{"id": "a"}, {"id": "b"}]
        Watcher.objects.filter(pk=watcher.pk).update(state={"cursor": {"pos": 0}, "seen": []}, next_check_at=None)
        back = reverse("cpanel:script_triggers", args=[self.script.pk])
        with _tasks():
            resp = self.client.post(check, {"next": back})
        self.assertRedirects(resp, back)
        runs = self._runs()
        self.assertEqual(len(runs), 2)
        runs[0].delete()
        detail = reverse("cpanel:watcher_detail", args=[watcher.pk])
        resp = self.client.get(detail)
        page = resp.content.decode()
        self.assertIn(reverse("cpanel:run_detail", args=[runs[1].pk]), page)
        self.assertIn("deleted", page)
        self.assertEqual([e["id"] for e in resp.context["events"]], ["b", "a"])  # newest first
        # A "next" pointing elsewhere is ignored.
        with _tasks():
            resp = self.client.post(check, {"next": "https://evil.example/"})
        self.assertRedirects(resp, detail)

    def test_turning_on_again_clears_the_auto_disable(self):
        watcher = self._watcher(enabled=False, auto_disabled_at=timezone.now(), error_count=10,
                                next_check_at=timezone.now() + datetime.timedelta(hours=1))
        detail = self.client.get(reverse("cpanel:watcher_detail", args=[watcher.pk])).content.decode()
        self.assertIn("Turned off after errors", detail)
        self.client.post(reverse("cpanel:watcher_toggle", args=[watcher.pk]))
        watcher.refresh_from_db()
        self.assertEqual((watcher.enabled, watcher.auto_disabled_at, watcher.error_count, watcher.next_check_at),
                         (True, None, 0, None))
        self.client.post(reverse("cpanel:watcher_toggle", args=[watcher.pk]))
        watcher.refresh_from_db()
        self.assertFalse(watcher.enabled)
        self.assertIsNone(watcher.auto_disabled_at)

    def test_delete_keeps_runs_and_plugin_owned_watchers_need_force(self):
        watcher = self._watcher(owner_plugin="shop")
        member = User.objects.create_user(username="wm@example.com", email="wm@example.com", password="x-Pass-12345!")
        WorkspaceMembership.ensure(member, self.ws, role=WorkspaceMembership.ROLE_MEMBER)
        self.client.force_login(member)
        self.client.post(reverse("cpanel:watcher_delete", args=[watcher.pk]), {"force": "1"})
        self.assertTrue(Watcher.objects.filter(pk=watcher.pk).exists())
        self.client.force_login(self.admin)
        resp = self.client.post(reverse("cpanel:watcher_delete", args=[watcher.pk]), {"force": "1"})
        self.assertRedirects(resp, reverse("cpanel:script_triggers", args=[self.script.pk]))
        self.assertFalse(Watcher.objects.filter(pk=watcher.pk).exists())


class RegistryListTests(_Fixture):
    """``watchers.list`` on the registry: the workspace's watchers, never settings, state or errors."""

    SECRET_URL = "https://feeds.example.test/orders?key=wl-secret-key-0031"

    def setUp(self):
        super().setUp()
        from core.test_plugin_api import _mock_setup

        _mock_setup(self)
        self.admin = User.objects.create_superuser(username="wl-admin", email="wl@example.com", password="x")
        self.watcher = self._watcher(
            name="Orders feed", adapter="http_poll", config={"url": self.SECRET_URL},
            state={"cursor": {"ids": ["wl-state-id"]}, "seen": ["wl-seen-id"]},
            input_map={"title": "item.title"}, last_error="GET " + self.SECRET_URL + " failed", error_count=2,
        )
        self.other_script = Script.objects.create(name="Second", environment=self.env, workspace=self.ws, code="pass")
        self._watcher(name="Off one", script=self.other_script, enabled=False)
        other_ws = Workspace.objects.create(name="Other wl")
        foreign = Script.objects.create(name="Foreign", environment=self.env, workspace=other_ws, code="pass")
        Watcher.objects.create(workspace=other_ws, script=foreign, name="Foreign watch", adapter="fake")

    def _token(self, permissions=("watchers:read",), **kwargs):
        from core.models import APIToken

        raw = APIToken.generate_token(scope="api")
        APIToken.objects.create(name=f"t{uuid.uuid4().hex[:6]}", scope="api", token=raw, workspace=self.ws,
                                permissions=list(permissions), created_by=self.admin, **kwargs)
        return raw

    def _get(self, raw, params=None):
        return self.client.get("/api/v1/watchers.list", params or {}, HTTP_AUTHORIZATION="Bearer " + raw)

    def test_declaration(self):
        from core.api.registry import registry as api_registry
        from core.api.tools import permissions_for
        from core.models import APIToken

        spec = api_registry.get("watchers.list")
        self.assertEqual((spec.access, spec.permission, spec.pyai_tier), ("read", "watchers:read", "channel"))
        self.assertIn("watchers:read", {p for p, _ in APIToken.API_PERMISSIONS})
        self.assertIn("watchers:read", permissions_for("channel"))

    def test_lists_the_workspaces_watchers_without_settings_state_or_errors(self):
        resp = self._get(self._token())
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["count"], 2)
        self.assertEqual([w["name"] for w in data["watchers"]], ["Off one", "Orders feed"])
        row = data["watchers"][1]
        self.assertEqual(row["adapter"], {"key": "http_poll", "title": "New entry in a JSON list (HTTP)"})
        self.assertEqual(row["script"], {"id": str(self.script.pk), "name": self.script.name})
        self.assertEqual((row["enabled"], row["error_count"], row["interval_seconds"]), (True, 2, 300))
        self.assertNotIn("config", row)
        self.assertNotIn("state", row)
        self.assertNotIn("last_error", row)
        text = resp.content.decode()
        for leak in ("wl-secret-key-0031", "wl-state-id", "wl-seen-id", "item.title", "Foreign watch"):
            self.assertNotIn(leak, text)

    def test_filters_and_a_token_bound_to_one_script(self):
        raw = self._token()
        only_on = self._get(raw, {"enabled": "true"}).json()["data"]["watchers"]
        self.assertEqual([w["name"] for w in only_on], ["Orders feed"])
        second = self._get(raw, {"script": str(self.other_script.pk)}).json()["data"]["watchers"]
        self.assertEqual([w["name"] for w in second], ["Off one"])
        self.assertEqual(self._get(raw, {"script": "nope"}).status_code, 404)
        bound = self._token(script=self.script)
        self.assertEqual([w["name"] for w in self._get(bound).json()["data"]["watchers"]], ["Orders feed"])
        self.assertEqual(self._get(bound, {"script": str(self.other_script.pk)}).status_code, 404)

    def test_needs_the_permission(self):
        self.assertEqual(self._get(self._token(permissions=("schedules:read",))).status_code, 403)


class BackupTests(_Fixture):
    def test_watchers_round_trip_with_state_and_health(self):
        from core.models import Connection
        from core.services.backup_service import BackupService

        conn = Connection.objects.create(workspace=self.ws, name="feed", provider="http", auth_kind="bearer")
        last = timezone.now() - datetime.timedelta(minutes=5)
        watcher = self._watcher(
            name="Orders", config={"tag": "x"}, input_map={"title": "item.title"}, interval_seconds=600,
            fire_cap_per_hour=7, connection=conn, state={"cursor": {"pos": 4}, "seen": ["a", "b"]},
            recent_events=[{"id": "b", "run": str(uuid.uuid4()), "at": last.isoformat()}],
            last_checked_at=last, last_event_at=last, error_count=2, last_error="boom",
            cap_window_start=last, cap_window_count=3, owner_plugin="shop", owner_key="orders",
            checking_since=timezone.now(),
        )
        off = self._watcher(name="Off", enabled=False, auto_disabled_at=last, error_count=10)
        data = BackupService.create_backup(include_runs=False)
        row = next(r for r in data["watchers"] if r["name"] == "Orders")
        self.assertNotIn("checking_since", row)
        self.assertEqual(row["script_id"], str(self.script.pk))

        result = BackupService.restore_backup(data, restore_runs=False, current_user=self.user)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["counts"]["watchers"], 2)
        w = Watcher.objects.get(pk=watcher.pk)
        self.assertEqual(
            (w.name, w.script_id, w.adapter, w.connection_id, w.config, w.input_map, w.interval_seconds,
             w.fire_cap_per_hour, w.enabled, w.state, w.recent_events, w.error_count, w.last_error,
             w.cap_window_count, w.owner_plugin, w.owner_key, w.created_by, w.targets),
            ("Orders", self.script.pk, "fake", conn.pk, {"tag": "x"}, {"title": "item.title"}, 600, 7, True,
             {"cursor": {"pos": 4}, "seen": ["a", "b"]}, watcher.recent_events, 2, "boom", 3, "shop", "orders",
             self.user, [str(self.script.pk)]),
        )
        self.assertEqual((w.last_checked_at, w.last_event_at, w.cap_window_start), (last, last, last))
        self.assertIsNone(w.checking_since)
        self.assertEqual(w.created_at, watcher.created_at)
        o = Watcher.objects.get(pk=off.pk)
        self.assertEqual((o.enabled, o.auto_disabled_at, o.error_count), (False, last, 10))

        # The restored watcher goes on exactly where it was: no run for old items.
        self.fake.items = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}, {"id": "e"}]
        Watcher.objects.filter(pk=w.pk).update(next_check_at=None)
        result = self._check(w)
        self.assertEqual(result.runs, 1)
        self.assertEqual([r.event["event_id"] for r in self._runs()], ["e"])

    def test_a_watcher_whose_connection_is_gone_comes_back_off(self):
        from core.models import Connection
        from core.services.backup_service import BackupService

        conn = Connection.objects.create(workspace=self.ws, name="feed", provider="http", auth_kind="bearer")
        self._watcher(name="Needs conn", connection=conn)
        data = BackupService.create_backup(include_runs=False)
        data["connections"] = []
        result = BackupService.restore_backup(data, restore_runs=False, current_user=self.user)
        self.assertTrue(result["success"], result)
        w = Watcher.objects.get()
        self.assertEqual((w.enabled, w.connection), (False, None))
        self.assertTrue(any('"Needs conn"' in m and "connection is gone" in m for m in result["warnings"]))

    def test_the_restore_preview_counts_the_watchers(self):
        # Test pass 6: a restore replaces every watcher, but its preview didn't say how many come back.
        from core.services.backup_service import BackupService

        self._watcher(name="One")
        self._watcher(name="Two", enabled=False)
        data = BackupService.create_backup(include_runs=False)
        self.assertEqual(BackupService.get_backup_preview(data)["counts"]["watchers"], 2)
        data.pop("watchers")
        self.assertEqual(BackupService.get_backup_preview(data)["counts"]["watchers"], 0)

    def test_an_older_backup_has_no_watchers(self):
        from core.services.backup_service import BackupService

        self._watcher(name="Old")
        data = BackupService.create_backup(include_runs=False)
        data.pop("watchers")
        result = BackupService.restore_backup(data, restore_runs=False, current_user=self.user)
        self.assertTrue(result["success"], result)
        self.assertNotIn("watchers", result["counts"])
        # Restore recreates scripts, and a watcher goes with its script.
        self.assertFalse(Watcher.objects.exists())
