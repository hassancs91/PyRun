"""
Bulk requirements install (Environments -> Packages -> Bulk install).

The form tests pin what the bulk form accepts compared with pip itself. The
rest of the suite mocks pip; ``RealPipBulkInstallTests`` runs the real path
(form -> view -> ``execute_package_operation`` -> pip in a fresh venv)
offline, against a wheel built here and served through PIP_FIND_LINKS.
"""

import codecs
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from core.forms import BulkInstallForm
from core.models import (
    Environment,
    GlobalSettings,
    PackageOperation,
    User,
    Workspace,
    WorkspaceMembership,
)
from core.services.environment_service import EnvironmentService
from core.tasks import execute_package_operation


def _upload(raw: bytes) -> BulkInstallForm:
    f = SimpleUploadedFile("requirements.txt", raw, content_type="text/plain")
    return BulkInstallForm(data={}, files={"requirements_file": f})


class BulkRequirementsFormTests(TestCase):
    def test_accepts_environment_markers(self):
        for line in (
            'numpy>=1.20; python_version >= "3.8"',
            'numpy>=1.20 ; python_version >= "3.8"',
            'pywin32;sys_platform=="win32"',
        ):
            with self.subTest(line=line):
                form = BulkInstallForm(data={"requirements": line})
                self.assertTrue(form.is_valid(), form.errors)

    def test_marker_does_not_excuse_a_bad_spec(self):
        for line in ('bad$name; python_version >= "3"', '; python_version >= "3"'):
            with self.subTest(line=line):
                form = BulkInstallForm(data={"requirements": line})
                self.assertFalse(form.is_valid())
                self.assertIn("Invalid package specification", str(form.errors))

    def test_upload_is_decoded_by_bom(self):
        body = "# pinned\r\nrequests==2.32.3\r\nsix\r\n"
        for label, raw in {
            "utf-8": body.encode("utf-8"),
            "utf-8 BOM (Notepad)": body.encode("utf-8-sig"),
            "utf-16 LE (PowerShell 5.1 pip freeze >)": codecs.BOM_UTF16_LE + body.encode("utf-16-le"),
            "utf-16 BE": codecs.BOM_UTF16_BE + body.encode("utf-16-be"),
        }.items():
            with self.subTest(encoding=label):
                form = _upload(raw)
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(
                    form.cleaned_data["requirements"].split(),
                    ["#", "pinned", "requests==2.32.3", "six"],
                )

    def test_undecodable_upload_is_refused(self):
        form = _upload(b"requests\n\xc3\x28\n")
        self.assertFalse(form.is_valid())
        self.assertIn("Could not read file", str(form.errors))


def _build_probe_wheel(folder: Path) -> None:
    """A minimal pure-Python wheel, ``pyrun-bulk-probe==1.0``."""
    dist_info = "pyrun_bulk_probe-1.0.dist-info"
    files = {
        "pyrun_bulk_probe/__init__.py": "VALUE = 'probe'\n",
        f"{dist_info}/METADATA": "Metadata-Version: 2.1\nName: pyrun-bulk-probe\nVersion: 1.0\n",
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: pyrun-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    files[f"{dist_info}/RECORD"] = "".join(f"{name},,\n" for name in [*files, f"{dist_info}/RECORD"])
    with zipfile.ZipFile(folder / "pyrun_bulk_probe-1.0-py3-none-any.whl", "w") as wheel:
        for name, content in files.items():
            wheel.writestr(name, content)


class RealPipBulkInstallTests(TestCase):
    """Bulk install through the console with real pip into a real venv."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        tmp = Path(tempfile.mkdtemp(prefix="pyrun-bulk-"))
        cls.addClassCleanup(shutil.rmtree, tmp, ignore_errors=True)
        settings_override = override_settings(ENVIRONMENTS_ROOT=str(tmp / "envs"))
        settings_override.enable()
        cls.addClassCleanup(settings_override.disable)

        wheels = tmp / "wheels"
        wheels.mkdir()
        _build_probe_wheel(wheels)
        # Offline: pip resolves only from the local wheel folder.
        offline = mock.patch.dict(
            os.environ,
            {
                "PIP_NO_INDEX": "1",
                "PIP_FIND_LINKS": str(wheels),
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            },
        )
        offline.start()
        cls.addClassCleanup(offline.stop)

        cls.venv_ok, cls.venv_message = EnvironmentService.create_environment(
            sys.executable, "bulk-venv"
        )

    def setUp(self):
        if not self.venv_ok:
            self.skipTest(f"cannot create a venv here: {self.venv_message}")
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()
        self.env = Environment.objects.create(
            name="Default Environment", path="bulk-venv", is_default=True
        )
        user = User.objects.create(email="bulk@example.com", is_staff=True, is_superuser=True)
        WorkspaceMembership.ensure(user, Workspace.get_default(), role=WorkspaceMembership.ROLE_OWNER)
        self.client.force_login(user)

    def _bulk_install(self, data) -> PackageOperation:
        with mock.patch("core.views.environments.async_task", return_value="task-1") as enqueue:
            self.client.post(reverse("cpanel:bulk_install", args=[self.env.pk]), data)
        enqueue.assert_called_once()
        operation_id = enqueue.call_args.args[1]
        execute_package_operation(operation_id)  # what the worker runs
        return PackageOperation.objects.get(pk=operation_id)

    @staticmethod
    def _imports(python: str) -> bool:
        return subprocess.run([python, "-c", "import pyrun_bulk_probe"], capture_output=True).returncode == 0

    def test_upload_installs_into_the_venv_only(self):
        body = '# probe\npyrun-bulk-probe==1.0\npyrun-bulk-absent>=1.0;python_version<"3"\n'
        operation = self._bulk_install(
            {"requirements_file": SimpleUploadedFile("requirements.txt", body.encode("utf-16"))}
        )

        self.assertEqual(operation.status, PackageOperation.Status.SUCCESS, operation.error)
        self.assertTrue(self._imports(self.env.get_python_executable()))
        self.assertFalse(self._imports(sys.executable))
        self.env.refresh_from_db()
        self.assertIn("pyrun-bulk-probe==1.0", self.env.requirements)

    def test_unresolvable_requirement_fails_the_operation(self):
        operation = self._bulk_install({"requirements": "pyrun-bulk-probe==9.9\n"})

        self.assertEqual(operation.status, PackageOperation.Status.FAILED)
        self.assertIn("No matching distribution", operation.error)
        self.env.refresh_from_db()
        self.assertEqual(self.env.requirements, "")
