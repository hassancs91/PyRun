"""
Tests for the SDK Showcase plugin.

Run in-tree with the normal Django test runner (so ``core.plugins.api`` is real),
imported into the suite by ``core/test_sdk_showcase_plugin.py`` — the same splice
shim pattern as ``examples/qdrant_backup/tests.py``. Coverage: idempotent
provisioning through the SDK, the counter read/write, the worker secret/config
contract, the vectors card (provision, index, filtered search, the worker's copy
of the toy embedding), form validation, and the inputs card (declared inputs,
``queue_run(inputs=)``, the worker's ``pyrunner_run`` side and the page itself).
"""

import runpy
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from . import provisioning as prov
from .forms import SECRET_FIELDS, SetupForm


# --------------------------------------------------------------------------- #
# Worker contract — secret/config names are wired to the standalone worker by
# convention; _worker_code() must fail loudly if they drift.
# --------------------------------------------------------------------------- #

class WorkerContractTests(SimpleTestCase):
    def test_worker_references_secret_and_config_keys(self):
        code = prov._worker_code()
        for token in list(SECRET_FIELDS.values()) + list(prov.CONFIG_KEYS):
            self.assertIn(token, code, f"worker_body.py missing reference to {token}")

    def test_drift_raises_loudly(self):
        with mock.patch.object(prov, "CONFIG_KEYS", prov.CONFIG_KEYS + ("zzz_unreferenced",)):
            with self.assertRaises(ValueError) as cm:
                prov._worker_code()
        self.assertIn("zzz_unreferenced", str(cm.exception))

    def test_worker_toy_embedding_matches_provisioning(self):
        worker = runpy.run_path(
            str(Path(prov.__file__).with_name("worker_body.py")), run_name="showcase_worker"
        )
        for text in ("Run a script every morning", "", "Ünïcode and 42 numbers"):
            self.assertEqual(worker["toy_embedding"](text), prov.toy_embedding(text), text)
        self.assertEqual(worker["VECTORS"].split(":", 1)[1], prov.VECTOR_KEY)


# --------------------------------------------------------------------------- #
# Setup form.
# --------------------------------------------------------------------------- #

class SetupFormTests(SimpleTestCase):
    ENVS = [SimpleNamespace(name="prod")]

    def _form(self, *, has_secret=False, **over):
        data = {"environment": "prod", "demo_token": "x", "message": "hi", "steps": "5"}
        data.update(over)
        return SetupForm(data, environments=self.ENVS, has_secret=has_secret)

    def test_valid(self):
        self.assertTrue(self._form().is_valid())

    def test_environment_required_when_available(self):
        self.assertFalse(self._form(environment="").is_valid())

    def test_steps_bounds_enforced(self):
        self.assertFalse(self._form(steps="0").is_valid())
        self.assertFalse(self._form(steps="99").is_valid())

    def test_token_optional(self):
        # The demo secret is always optional (blank keeps any existing value).
        self.assertTrue(self._form(has_secret=True, demo_token="").is_valid())
        self.assertTrue(self._form(demo_token="").is_valid())


# --------------------------------------------------------------------------- #
# Provisioning — one setup creates exactly the declared resources, idempotently.
# --------------------------------------------------------------------------- #

@override_settings(VECTORS_DIR=Path(tempfile.mkdtemp(prefix="pyrun-showcase-vectors-")))
class ProvisionTests(TestCase):
    def setUp(self):
        from core.models import Environment, Workspace

        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="prod", path="scenv", requirements="")
        patch = mock.patch("core.services.schedule_service.ScheduleService.sync_schedule")
        patch.start()
        self.addCleanup(patch.stop)

    def _data(self, **over):
        data = {"environment": "prod", "demo_token": "tok", "message": "hi", "steps": 5}
        data.update(over)
        return data

    def _counts(self):
        from core.models import DataStore, Script, ScriptSchedule, Secret, SecretGrant

        script = Script.objects.get(owner_plugin=prov.OWNER, owner_key=prov.SCRIPT_KEY)
        return {
            "scripts": Script.objects.filter(owner_plugin=prov.OWNER).count(),
            "secrets": Secret.objects.filter(owner_plugin=prov.OWNER).count(),
            "stores": DataStore.objects.filter(name=f"{prov.OWNER}:{prov.STORE_KEY}").count(),
            "grants": SecretGrant.objects.filter(script=script).count(),
            "schedules": ScriptSchedule.objects.filter(script=script).count(),
        }

    def test_provision_creates_all_declared_resources(self):
        from core.models import Script

        script, warnings = prov.provision(self._data())
        self.assertEqual(warnings, [])
        self.assertEqual(script.name, prov.SCRIPT_NAME)
        self.assertEqual(script.injection_mode, Script.InjectionMode.SELECTED)
        self.assertEqual(self._counts(), {"scripts": 1, "secrets": 1, "stores": 1, "grants": 1, "schedules": 1})
        self.assertEqual(prov.get_config()["message"], "hi")
        self.assertEqual(prov.get_counter(), 0)
        self.assertEqual(
            prov.owned_inventory(),
            {"datastores": 1, "secrets": 1, "scripts": 1, "schedules": 1, "vector_collections": 1},
        )

    def test_provision_is_idempotent(self):
        prov.provision(self._data())
        prov.provision(self._data(steps=9, message="changed"))
        self.assertEqual(self._counts(), {"scripts": 1, "secrets": 1, "stores": 1, "grants": 1, "schedules": 1})
        self.assertEqual(prov.get_config()["steps"], 9)

    def test_blank_token_keeps_existing(self):
        from core.models import Secret

        prov.provision(self._data())
        prov.provision(self._data(demo_token=""))
        secret = Secret.objects.get(owner_plugin=prov.OWNER, owner_key="DEMO_TOKEN")
        self.assertEqual(secret.get_decrypted_value(), "tok")
        self.assertEqual(Secret.objects.filter(owner_plugin=prov.OWNER).count(), 1)

    def test_counter_increment_and_reset(self):
        prov.provision(self._data())
        self.assertEqual(prov.increment_counter(), 1)
        self.assertEqual(prov.increment_counter(), 2)
        self.assertEqual(prov.get_counter(), 2)
        prov.reset_demo_data()
        self.assertEqual(prov.get_counter(), 0)

    def test_sync_schedule_switches_mode(self):
        prov.provision(self._data())
        self.assertTrue(prov.sync_schedule("interval"))
        sched = prov.get_schedule()
        self.assertEqual(sched.run_mode, "interval")
        self.assertEqual(sched.interval_minutes, 60)

    def test_unknown_environment_raises(self):
        with self.assertRaises(ValueError):
            prov.provision(self._data(environment="ghost"))

    def test_vectors_card_provision_index_and_search(self):
        prov.provision(self._data())
        prov.provision(self._data())  # idempotent: still one collection
        summary = prov.vector_summary()
        self.assertEqual(
            (summary["name"], summary["points"], summary["dimension"], summary["metric"]),
            (f"{prov.OWNER}:{prov.VECTOR_KEY}", 0, prov.VECTOR_DIM, "cosine"),
        )
        self.assertEqual(prov.owned_inventory()["vector_collections"], 1)
        self.assertEqual(prov.index_sample_docs(), len(prov.SAMPLE_DOCS))
        self.assertEqual(prov.index_sample_docs(), len(prov.SAMPLE_DOCS))  # upsert by id
        hits = prov.search_docs("store an api key for the script")
        self.assertEqual(hits[0]["payload"]["topic"], "secrets")
        self.assertEqual(hits, sorted(hits, key=lambda h: -h["score"]))
        filtered = prov.search_docs("store an api key for the script", topic="schedules")
        self.assertTrue(filtered)
        self.assertTrue(all(h["payload"]["topic"] == "schedules" for h in filtered))

    def test_vectors_skipped_with_a_warning_when_no_store_is_enabled(self):
        from core.models import VectorConnection

        VectorConnection.objects.update(enabled=False)
        _script, warnings = prov.provision(self._data())
        self.assertEqual(len(warnings), 1)
        self.assertIsNone(prov.vector_summary())
        with self.assertRaises(ValueError):
            prov.index_sample_docs()


# --------------------------------------------------------------------------- #
# Inputs & result card (PyRun 2.5): declared inputs, queue_run(inputs=), the
# worker's pyrunner_run side, and the page.
# --------------------------------------------------------------------------- #

class _FakeRunHelper:
    """Stands in for the ``pyrunner_run`` module inside the worker."""

    class RunError(Exception):
        pass

    def __init__(self, inputs):
        self._inputs = inputs
        self.progress_calls = []
        self.results = []

    def inputs(self):
        if isinstance(self._inputs, Exception):
            raise self._inputs
        return dict(self._inputs)

    def progress(self, pct=None, message=None):
        self.progress_calls.append((pct, message))

    def set_result(self, result):
        self.results.append(result)


class WorkerInputsTests(SimpleTestCase):
    def _run_worker(self, helper):
        worker = runpy.run_path(
            str(Path(prov.__file__).with_name("worker_body.py")), run_name="showcase_worker"
        )
        modules = {"pyrunner_run": helper} if helper is not None else {"pyrunner_run": None}
        with mock.patch.dict("sys.modules", modules), \
                mock.patch.dict(worker["main"].__globals__, {"_store": lambda: None}), \
                mock.patch("time.sleep"), mock.patch("builtins.print"):
            worker["main"]()

    def test_worker_uses_its_inputs_and_reports_progress_and_result(self):
        helper = _FakeRunHelper({"message": "from inputs", "steps": 2})
        self._run_worker(helper)
        self.assertEqual(len(helper.results), 1)
        result = helper.results[0]
        self.assertEqual((result["message"], result["steps"]), ("from inputs", 2))
        self.assertEqual(helper.progress_calls[0], (0, "Starting"))
        self.assertEqual(helper.progress_calls[-1], (100, "Done"))
        self.assertIn((50, "Step 2 of 2"), helper.progress_calls)

    def test_worker_falls_back_to_the_data_store_config_without_inputs(self):
        helper = _FakeRunHelper({})
        self._run_worker(helper)
        # _store() is None here, so the config defaults apply: 5 steps.
        self.assertEqual(helper.results[0]["steps"], 5)

    def test_worker_runs_standalone_without_the_helper(self):
        self._run_worker(None)  # import pyrunner_run fails: no crash

    def test_an_unreadable_run_file_is_logged_not_silent(self):
        helper = _FakeRunHelper(_FakeRunHelper.RunError("Could not read the run file"))
        with mock.patch("builtins.print") as printed:
            worker = runpy.run_path(
                str(Path(prov.__file__).with_name("worker_body.py")), run_name="showcase_worker"
            )
            with mock.patch.dict("sys.modules", {"pyrunner_run": helper}):
                self.assertEqual(worker["run_inputs"](), {})
        self.assertIn("Could not read the run file", str(printed.call_args_list))

    def test_a_wrong_progress_call_is_not_swallowed(self):
        worker = runpy.run_path(
            str(Path(prov.__file__).with_name("worker_body.py")), run_name="showcase_worker"
        )
        helper = _FakeRunHelper({})
        helper.progress = mock.Mock(side_effect=ValueError("pct must be a number from 0 to 100"))
        with mock.patch.dict("sys.modules", {"pyrunner_run": helper}),                 self.assertRaises(ValueError):
            worker["report_progress"](150, "x")


@override_settings(VECTORS_DIR=Path(tempfile.mkdtemp(prefix="pyrun-showcase-vectors-")))
class InputsCardTests(TestCase):
    def setUp(self):
        from core.models import Environment

        self.env = Environment.objects.create(name="prod", path="scinputs", requirements="")
        for target in (
            "core.services.schedule_service.ScheduleService.sync_schedule",
            "core.tasks.async_task",
        ):
            patch = mock.patch(target, return_value="task-1")
            patch.start()
            self.addCleanup(patch.stop)
        prov.provision({"environment": "prod", "demo_token": "tok", "message": "hi", "steps": 5})

    def _runs(self):
        from core.models import Run

        return Run.objects.filter(script=prov.get_script())

    def test_provision_declares_inputs_with_the_settings_as_defaults(self):
        schema = prov.get_script().inputs_schema
        # A set: PostgreSQL's jsonb doesn't keep object key order.
        self.assertEqual(set(schema["properties"]), set(prov.CONFIG_KEYS))
        self.assertEqual(schema["required"], list(prov.CONFIG_KEYS))
        self.assertEqual(schema["properties"]["message"]["default"], "hi")
        self.assertEqual(schema["properties"]["steps"]["default"], 5)
        prov.provision({"environment": "prod", "demo_token": "", "message": "changed", "steps": 9})
        schema = prov.get_script().inputs_schema
        self.assertEqual(schema["properties"]["message"]["default"], "changed")
        self.assertEqual(schema["properties"]["steps"]["default"], 9)

    def test_every_schedule_mode_still_syncs(self):
        # Every input has a default, so the Schedule card isn't refused.
        for mode in ("interval", "daily", "manual"):
            self.assertTrue(prov.sync_schedule(mode))

    def test_queue_with_inputs_stamps_them_and_defaults_fill_the_rest(self):
        run, error = prov.queue_demo_run(inputs={"steps": 3})
        self.assertIsNone(error)
        run.refresh_from_db()
        self.assertEqual(run.inputs, {"message": "hi", "steps": 3})
        plain, _ = prov.queue_demo_run()
        plain.refresh_from_db()
        self.assertEqual(plain.inputs, {"message": "hi", "steps": 5})

    def test_bad_inputs_raise_and_create_no_run(self):
        from core.plugins.api import InputsError

        with self.assertRaises(InputsError) as cm:
            prov.queue_demo_run(inputs={"steps": 99})
        self.assertEqual(set(cm.exception.field_errors), {"steps"})
        self.assertFalse(self._runs().exists())

    def test_run_inside_a_maintenance_window_says_so(self):
        # Found in the 2.5 test pass: the button said "Demo run queued" for a
        # run the window had already cancelled.
        import datetime

        from django.utils import timezone

        from core.models import MaintenanceWindow, Run

        now = timezone.now()
        MaintenanceWindow.objects.create(
            name="Upgrade", kind=MaintenanceWindow.Kind.ONE_OFF,
            starts_at=now - datetime.timedelta(minutes=5),
            ends_at=now + datetime.timedelta(hours=1), blocks=["manual"],
        )
        for inputs in (None, {"steps": 2}):
            run, error = prov.queue_demo_run(inputs=inputs)
            self.assertIsNone(run)
            self.assertEqual(error, "Not started: a maintenance window blocks manual runs right now.")
        self.assertEqual(
            list(self._runs().values_list("cancel_reason", flat=True)),
            [Run.CancelReason.MAINTENANCE] * 2,
        )

    def test_latest_run_report_reads_run_view(self):
        self.assertIsNone(prov.latest_run_report())
        run, _ = prov.queue_demo_run(inputs={"steps": 2})
        self._runs().filter(pk=run.pk).update(
            status="success", result={"steps": 2, "ok": True},
            progress={"pct": 100, "message": "Done", "updated_at": "2026-11-08T10:00:00+00:00"},
        )
        report = prov.latest_run_report()
        self.assertEqual(report["inputs"], {"message": "hi", "steps": 2})
        self.assertEqual(report["result"], {"steps": 2, "ok": True})
        self.assertEqual(report["progress"]["message"], "Done")
        self.assertTrue(report["is_finished"])
        self.assertIn('"ok": true', report["result_json"])

    def test_run_inputs_form_leaves_blank_fields_out(self):
        from .forms import RunInputsForm

        form = RunInputsForm({"message": "  ", "steps": "7"})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.inputs(), {"steps": 7})


class InputsPageTests(TestCase):
    """The card through the real page: plugin URLs and templates mounted the way
    an installed plugin gets them (the test runner only splices the import path)."""

    def setUp(self):
        import types

        from django.conf import settings
        from django.urls import include, path

        import pyrunner.urls
        from core.models import Environment, User, Workspace, WorkspaceMembership

        urlconf = types.ModuleType("sdk_showcase_test_urls")
        urlconf.urlpatterns = list(pyrunner.urls.urlpatterns) + [
            path("plugins/sdk_showcase/", include("plugins.sdk_showcase.urls")),
        ]
        templates = [dict(cfg) for cfg in settings.TEMPLATES]
        templates[0]["DIRS"] = list(templates[0].get("DIRS", [])) + [
            Path(__file__).resolve().parent / "templates"
        ]
        override = override_settings(
            ROOT_URLCONF=urlconf,
            TEMPLATES=templates,
            VECTORS_DIR=Path(tempfile.mkdtemp(prefix="pyrun-showcase-vectors-")),
        )
        override.enable()
        self.addCleanup(override.disable)
        for target in (
            "core.services.setup_service.SetupService.is_setup_needed",
            "core.services.setup_service.SetupService.needs_admin_setup",
            "core.services.schedule_service.ScheduleService.sync_schedule",
        ):
            patch = mock.patch(target, return_value=False)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch("core.tasks.async_task", return_value="task-1")
        patch.start()
        self.addCleanup(patch.stop)

        self.user = User.objects.create_superuser(
            username="showcase", email="showcase@x.test", password="pw-12345678!"
        )
        WorkspaceMembership.objects.update_or_create(
            user=self.user, workspace=Workspace.get_default(),
            defaults={"role": WorkspaceMembership.ROLE_OWNER},
        )
        self.client.force_login(self.user)
        Environment.objects.create(name="prod", path="scpage", requirements="")
        prov.provision({"environment": "prod", "demo_token": "tok", "message": "hi", "steps": 5})

    def _runs(self):
        from core.models import Run

        return Run.objects.filter(script=prov.get_script())

    def test_page_renders_the_card_with_the_latest_result_escaped(self):
        run, _ = prov.queue_demo_run(inputs={"steps": 2})
        self._runs().filter(pk=run.pk).update(
            status="success", result={"note": "<script>alert(1)</script>"},
            progress={"pct": 100, "message": "Done", "updated_at": "2026-11-08T10:00:00+00:00"},
        )
        response = self.client.get("/plugins/sdk_showcase/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('id="inputs"', html)
        self.assertIn("Run with inputs", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn('style="width: 100%"', html)

    def test_post_with_bad_inputs_shows_the_sdk_reason_on_the_field(self):
        response = self.client.post("/plugins/sdk_showcase/run/inputs/", {"message": "", "steps": "99"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Number is outside the allowed range.")
        self.assertFalse(self._runs().exists())

    def test_post_with_good_inputs_queues_a_run(self):
        response = self.client.post("/plugins/sdk_showcase/run/inputs/", {"message": "yo", "steps": "4"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].endswith("#inputs"))
        run = self._runs().get()
        self.assertEqual(run.inputs, {"message": "yo", "steps": 4})
        self.assertEqual(run.triggered_by_id, self.user.pk)

    def test_non_superuser_is_refused(self):
        from core.models import User

        member = User.objects.create(email="member@x.test", username="member")
        self.client.force_login(member)
        response = self.client.post("/plugins/sdk_showcase/run/inputs/", {"steps": "4"})
        self.assertNotEqual(response.status_code, 200)
        self.assertFalse(self._runs().exists())
