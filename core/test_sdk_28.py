"""
PyRun 2.5 T47 — plugin SDK 2.8.

The version bump itself, the SDK changelog in docs/plugins.md naming every
2.8 class and kwarg, every example plugin declaring its minimum ``api`` and
passing the doctor, the doctor's ``api >= 2.8`` rule for the 2.5 surfaces
(``requires_connections``, ``triggers.py``, provisioned tables), and install /
activation refusing a plugin that needs a newer SDK than this PyRun's.
"""

import inspect
import json
import re
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from core.models import Plugin
from core.plugins import api as sdk
from core.services import PluginInstallError, PluginService
from core.services.plugin_doctor import FAIL, run_doctor
from core.test_plugin_update import SLUG, _Dir, demo_zip

EXAMPLES = Path(settings.BASE_DIR) / "examples"
DOCS = Path(settings.BASE_DIR) / "docs" / "plugins.md"

# What 2.8 added since 2.7: (owner, kwarg or attribute) or a class name.
NEW_CLASSES = (
    "InputsError", "ConnectionAPI", "ConnectionView", "ConnectionRequirement", "ConnectionUnavailable",
    "WatcherAPI", "TriggerAdapter", "TriggerContext", "TriggerError", "TableAPI", "TableHandle", "TableError",
)
NEW_KWARGS = (
    ("ScriptAPI.upsert", "inputs_schema"),
    ("ScriptAPI.upsert", "concurrency_policy"),
    ("ScriptAPI.queue_run", "inputs"),
    ("ScheduleAPI.sync", "times"),
    ("ScheduleAPI.sync", "weekdays"),
    ("ScheduleAPI.sync", "month_days"),
)
NEW_RUNVIEW_FIELDS = ("inputs", "result", "progress", "cancel_reason", "is_held", "stop_requested_at")


def _changelog():
    text = DOCS.read_text(encoding="utf-8")
    match = re.search(r"^## SDK changelog\n(.*?)(?=^## )", text, re.S | re.M)
    assert match, "docs/plugins.md has no '## SDK changelog' section"
    return match.group(1)


class VersionTests(SimpleTestCase):
    def test_the_sdk_is_2_8(self):
        self.assertEqual(sdk.API_VERSION, "2.8")

    def test_every_2_8_name_exists_in_the_sdk(self):
        for name in NEW_CLASSES:
            self.assertTrue(inspect.isclass(getattr(sdk, name, None)), name)
        for owner, kwarg in NEW_KWARGS:
            cls, method = owner.split(".")
            params = inspect.signature(getattr(getattr(sdk, cls), method)).parameters
            self.assertIn(kwarg, params, owner)
        fields = {f for f in sdk.RunView.__dataclass_fields__}
        for name in NEW_RUNVIEW_FIELDS:
            self.assertIn(name, fields)

    def test_the_changelog_names_every_2_8_addition(self):
        section = _changelog()
        self.assertIn("### 2.8", section)
        added = section.split("### 2.8", 1)[1].split("###", 1)[0]
        for name in NEW_CLASSES:
            self.assertIn(f"`{name}`", added)
        for owner, kwarg in NEW_KWARGS:
            self.assertIn(f"`{owner}(", added)
            self.assertIn(f"{kwarg}=", added)
        for name in NEW_RUNVIEW_FIELDS:
            self.assertIn(f"`{name}`", added)
        for name in ("requires_connections", "triggers.py", "lifecycle.py"):
            self.assertIn(name, added)

    def test_the_changelog_lists_every_version(self):
        section = _changelog()
        for version in ("2.1", "2.2", "2.3", "2.4", "2.5", "2.6", "2.7", "2.8"):
            self.assertIn(f"### {version}", section)


class ExamplePluginTests(SimpleTestCase):
    def _manifests(self):
        paths = sorted(EXAMPLES.glob("*/plugin.json"))
        self.assertGreaterEqual(len(paths), 7)
        return {p.parent: json.loads(p.read_text(encoding="utf-8")) for p in paths}

    def test_every_example_declares_an_api_this_sdk_serves(self):
        current = tuple(int(p) for p in sdk.API_VERSION.split("."))
        for folder, manifest in self._manifests().items():
            with self.subTest(plugin=folder.name):
                api = manifest.get("api")
                self.assertRegex(api or "", r"^2\.\d+$")
                self.assertLessEqual(tuple(int(p) for p in api.split(".")), current)

    def test_examples_using_2_5_surfaces_declare_2_8(self):
        for slug in ("brand_tracker", "sdk_showcase", "yt_comments"):
            manifest = json.loads((EXAMPLES / slug / "plugin.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["api"], "2.8", slug)

    def test_every_example_passes_the_doctor(self):
        for folder in self._manifests():
            with self.subTest(plugin=folder.name):
                report = run_doctor(folder)
                self.assertTrue(report.ok, report.format())


class DoctorRuleTests(SimpleTestCase):
    def _folder(self, manifest, files=None):
        root = Path(tempfile.mkdtemp(prefix="t47-doctor-"))
        self.addCleanup(shutil.rmtree, root, True)
        folder = root / "t47demo"
        folder.mkdir()
        (folder / "plugin.json").write_text(json.dumps({"slug": "t47demo", "version": "1.0.0", **manifest}),
                                            encoding="utf-8")
        (folder / "__init__.py").write_text("", encoding="utf-8")
        (folder / "apps.py").write_text(
            "from core.plugins import PluginAppConfig\n\n\nclass C(PluginAppConfig):\n"
            "    name = 'plugins.t47demo'\n    label = 't47demo'\n",
            encoding="utf-8",
        )
        for name, content in (files or {}).items():
            (folder / name).write_text(content, encoding="utf-8")
        return folder

    def _fails(self, folder, check):
        return [f.message for f in run_doctor(folder).findings if f.rule == check and f.severity == FAIL]

    def test_requires_connections_needs_2_8(self):
        conn = {"requires_connections": [{"provider": "github", "key": "gh"}]}
        for api in (None, "2.7"):
            with self.subTest(api=api):
                failures = self._fails(self._folder({**conn, "api": api}), "connections")
                self.assertTrue(any('"api" must be >= "2.8"' in f for f in failures), failures)
        self.assertEqual(self._fails(self._folder({**conn, "api": "2.8"}), "connections"), [])

    def test_triggers_py_needs_2_8(self):
        triggers = (
            "from core.plugins.api import TriggerAdapter\n\n\nclass A(TriggerAdapter):\n"
            "    key = 'a'\n    title = 'A'\n\n    def check(self, ctx, config, state):\n        return []\n\n"
            "    def test(self, ctx, config):\n        return []\n"
        )
        failures = self._fails(self._folder({"api": "2.7"}, {"triggers.py": triggers}), "triggers")
        self.assertTrue(any("triggers.py" in f and '"2.8"' in f for f in failures), failures)
        self.assertEqual(self._fails(self._folder({"api": "2.8"}, {"triggers.py": triggers}), "triggers"), [])

    def test_provisioned_tables_need_2_8(self):
        failures = self._fails(self._folder({"api": "2.7", "provisions": {"tables": 1}}), "metadata")
        self.assertTrue(any("provision tables" in f for f in failures), failures)
        self.assertEqual(self._fails(self._folder({"api": "2.8", "provisions": {"tables": 1}}), "metadata"), [])
        self.assertEqual(self._fails(self._folder({"api": "2.0", "provisions": {"tables": 0}}), "metadata"), [])

    def test_a_plugin_without_2_5_surfaces_keeps_its_old_api(self):
        report = run_doctor(self._folder({"api": "2.1", "provisions": {"scripts": 1}}))
        self.assertTrue(report.ok, report.format())


class SdkGateTests(_Dir):
    def test_install_refuses_a_newer_sdk(self):
        with self.assertRaises(PluginInstallError) as caught:
            PluginService.upload(demo_zip(api="2.9"))
        self.assertIn("needs plugin SDK 2.9", str(caught.exception))
        self.assertFalse(Plugin.objects.filter(slug=SLUG).exists())
        self.assertFalse((self.root / SLUG).exists())

    def test_install_takes_this_sdk(self):
        plugin, _update, _ = PluginService.upload(demo_zip(api="2.8"))
        self.assertEqual(plugin.slug, SLUG)

    def test_activation_refuses_a_newer_sdk(self):
        plugin = self.install(api="2.8")
        with mock.patch("core.plugins.api.API_VERSION", "2.7"), \
                mock.patch.object(PluginService, "_run_preflight") as preflight:
            ok, message = PluginService.activate(plugin)
        self.assertFalse(ok)
        self.assertIn("needs plugin SDK 2.8", message)
        preflight.assert_not_called()
