"""
Per-script environment variables (2.5 Stage 1, ticket T09).

What this pins, roughly in order of how much it would hurt to get wrong:

- Precedence: system environment < env vars < inputs (``INPUT_<NAME>``) <
  secrets, checked with the same key in two layers, and end to end in a real
  subprocess. PyRun's own variables (the run ids) can't be replaced.
- Names are validated (letters, digits, underscores; not PATH and the other
  reserved names; not PYRUNNER_*), values can't hold NUL, and a stored value
  the editor would refuse never reaches a run.
- The editor's Env vars tab: rows saved and shown sorted, blank rows ignored,
  a refused save keeps what was typed and opens the tab, a form posted without
  the tab keeps the saved variables, the help text says values are unmasked.
- A key that matches a secret the script gets is warned about (on the row and
  after saving), following the injection mode and the workspace.
- Forks copy the variables; backups round-trip them.
"""

import os
import sys
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.executor import _build_script_environment, execute_run
from core.forms import ScriptForm, SecretCreateForm
from core.models import (
    Environment,
    Run,
    Script,
    Secret,
    SecretGrant,
    Workspace,
    WorkspaceMembership,
)
from core.services.backup_service import BackupService
from core.services.script_env import (
    MAX_BYTES,
    MAX_VARS,
    EnvVarsError,
    check_env_vars,
    editor_secret_names,
    env_var_collisions,
    secret_names_visible_to,
    usable_env_vars,
)
from core.services.script_service import fork_script

User = get_user_model()


def _secret(key, value, workspace, **kwargs):
    s = Secret(key=key, workspace=workspace, **kwargs)
    s.set_value(value)
    s.save()
    return s


def _setup_wizard_off(test):
    for target in (
        "core.services.setup_service.SetupService.is_setup_needed",
        "core.services.setup_service.SetupService.needs_admin_setup",
    ):
        p = mock.patch(target, return_value=False)
        p.start()
        test.addCleanup(p.stop)


class _Fixture:
    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="evenv", path=f"evenv{uuid.uuid4().hex[:6]}")

    def _script(self, env_vars=None, **kwargs):
        kwargs.setdefault("code", "pass")
        kwargs.setdefault("workspace", self.ws)
        return Script.objects.create(
            name=f"s{uuid.uuid4().hex[:6]}",
            environment=self.env,
            env_vars=env_vars or {},
            **kwargs,
        )

    def _run(self, script, inputs=None):
        return Run.objects.create(
            script=script, workspace=script.workspace, status=Run.Status.PENDING, inputs=inputs
        )


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


class CheckEnvVarsTests(TestCase):
    def test_valid_rows_become_a_dict(self):
        rows = [("REGION", "eu-west-1"), (" MODE ", "  fast "), ("http_proxy", ""), ("", "")]
        self.assertEqual(
            check_env_vars(rows),
            {"REGION": "eu-west-1", "MODE": "  fast ", "http_proxy": ""},
        )

    def test_a_dict_and_nothing_are_accepted(self):
        self.assertEqual(check_env_vars({"A": "1"}), {"A": "1"})
        self.assertEqual(check_env_vars(None), {})
        self.assertEqual(check_env_vars([]), {})

    def test_refusals(self):
        cases = {
            "dash in the name": [("MY-VAR", "x")],
            "starts with a digit": [("1ST", "x")],
            "space inside": [("MY VAR", "x")],
            "too long": [("A" * 101, "x")],
            "reserved": [("PATH", "/tmp")],
            "reserved in lower case": [("pythonpath", "/tmp")],
            "PyRun's prefix": [("PYRUNNER_RUN_ID", "x")],
            "PyRun's prefix in lower case": [("pyrunner_db_path", "x")],
            "listed twice": [("A", "1"), ("A", "2")],
            "a value with no name": [("", "orphan")],
            "NUL in the value": [("A", "x\x00y")],
            "a lone surrogate": [("A", "\ud800")],
            "not text": {"A": 3},
            "too many": [(f"V{i}", "x") for i in range(MAX_VARS + 1)],
            "too big": [("A", "x" * MAX_BYTES)],
        }
        for label, rows in cases.items():
            with self.subTest(label), self.assertRaises(EnvVarsError):
                check_env_vars(rows)

    def test_every_problem_is_listed(self):
        with self.assertRaises(EnvVarsError) as caught:
            check_env_vars([("BAD-ONE", "x"), ("PATH", "y"), ("OK", "z")])
        self.assertEqual(len(caught.exception.messages), 2)
        self.assertIn("BAD-ONE", caught.exception.messages[0])
        self.assertIn("reserved", caught.exception.messages[1])

    def test_usable_env_vars_keeps_only_what_the_editor_accepts(self):
        stored = {"GOOD": "1", "BAD-NAME": "x", "PATH": "/tmp", "PYRUNNER_RUN_ID": "spoof",
                  "NUM": 3, "NUL": "a\x00b", " PADDED": "x"}
        self.assertEqual(usable_env_vars(stored), {"GOOD": "1"})
        for not_a_dict in (None, [], "A=1", 5):
            self.assertEqual(usable_env_vars(not_a_dict), {})

    def test_usable_env_vars_stops_at_the_limits(self):
        many = {f"V{i:03d}": "x" for i in range(MAX_VARS + 5)}
        self.assertEqual(len(usable_env_vars(many)), MAX_VARS)
        big = {"A": "x" * (MAX_BYTES - 10), "B": "y" * 100}
        self.assertEqual(list(usable_env_vars(big)), ["A"])

    def test_secret_keys_share_the_reserved_names(self):
        form = SecretCreateForm({"key": "PATH", "value": "x", "source": "local"}, workspace=Workspace.get_default())
        self.assertFalse(form.is_valid())
        self.assertIn("reserved", str(form.errors["key"]))


# --------------------------------------------------------------------------- #
# Executor: injection and precedence
# --------------------------------------------------------------------------- #


class InjectionTests(_Fixture, TestCase):
    def test_env_vars_beat_the_system_environment(self):
        run = self._run(self._script({"REGION": "from-script"}))
        with mock.patch.dict(os.environ, {"REGION": "from-system"}):
            env = _build_script_environment(run=run, secrets={})
        self.assertEqual(env["REGION"], "from-script")

    def test_inputs_beat_env_vars(self):
        run = self._run(self._script({"INPUT_CITY": "from-env-var", "INPUT_DAYS": "7"}),
                        inputs={"city": "Paris"})
        env = _build_script_environment(run=run, secrets={})
        self.assertEqual(env["INPUT_CITY"], "Paris")
        self.assertEqual(env["INPUT_DAYS"], "7")  # no such input: the env var shows through

    def test_secrets_beat_env_vars_and_inputs(self):
        run = self._run(self._script({"API_KEY": "plain", "INPUT_CITY": "env"}), inputs={"city": "Paris"})
        env = _build_script_environment(
            run=run, secrets={"API_KEY": "from-secret", "INPUT_CITY": "from-secret"}
        )
        self.assertEqual(env["API_KEY"], "from-secret")
        self.assertEqual(env["INPUT_CITY"], "from-secret")

    def test_pyrunner_variables_cant_be_replaced(self):
        # A row the editor refuses (hand-edited in the database) never reaches a run.
        script = self._script()
        Script.objects.filter(pk=script.pk).update(
            env_vars={"PYRUNNER_RUN_ID": "spoof", "PATH": "/nowhere", "OK": "1"}
        )
        run = self._run(Script.objects.get(pk=script.pk))
        env = _build_script_environment(run=run, secrets={})
        self.assertEqual(env["PYRUNNER_RUN_ID"], run.id.hex)
        self.assertNotEqual(env.get("PATH"), "/nowhere")
        self.assertEqual(env["OK"], "1")

    def test_no_env_vars_leaves_the_environment_as_before(self):
        run = self._run(self._script())
        with mock.patch.dict(os.environ, {"REGION": "from-system"}):
            env = _build_script_environment(run=run, secrets={})
        self.assertEqual(env["REGION"], "from-system")

    @mock.patch("core.executor._validate_environment", return_value=sys.executable)
    def test_a_real_subprocess_sees_the_layers_in_order(self, _val):
        _secret("SHARED_KEY", "secret-value", self.ws)
        code = (
            "import os\n"
            "print('region=' + os.environ['REGION'])\n"
            "print('shared=' + os.environ['SHARED_KEY'])\n"
            "print('city=' + os.environ['INPUT_CITY'])\n"
        )
        script = self._script(
            {"REGION": "eu-west-1", "SHARED_KEY": "plain-value", "INPUT_CITY": "env-city"},
            code=code,
            inputs_schema={"type": "object", "properties": {"city": {"type": "string"}}},
        )
        run = self._run(script, inputs={"city": "Paris"})
        execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.SUCCESS, run.stderr)
        self.assertIn("region=eu-west-1", run.stdout)
        self.assertIn("city=Paris", run.stdout)
        # The secret won; its value is masked in the output like any secret.
        self.assertIn("shared=[SHARED_KEY:MASKED]", run.stdout)
        self.assertNotIn("plain-value", run.stdout)


# --------------------------------------------------------------------------- #
# Which secrets a script gets (names only)
# --------------------------------------------------------------------------- #


class VisibleSecretTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.other = Workspace.objects.create(name="Other env-vars ws")
        _secret("IN_WS", "a", self.ws)
        _secret("LEGACY", "b", None)
        _secret("OTHER_WS", "c", self.other)
        _secret("PLUGIN_OWN", "d", self.ws, owner_plugin="demo", owner_key="own")

    def test_all_mode_gets_the_workspace_and_unassigned_user_secrets(self):
        script = self._script()
        self.assertEqual(secret_names_visible_to(script), {"IN_WS", "LEGACY"})

    def test_selected_mode_gets_only_attached_and_own_plugin_secrets(self):
        user_script = self._script(injection_mode=Script.InjectionMode.SELECTED)
        self.assertEqual(secret_names_visible_to(user_script), set())
        SecretGrant.objects.create(script=user_script, secret=Secret.objects.get(key="IN_WS"), active=True)
        self.assertEqual(secret_names_visible_to(user_script), {"IN_WS"})

        plugin_script = self._script(injection_mode=Script.InjectionMode.SELECTED, owner_plugin="demo")
        self.assertEqual(secret_names_visible_to(plugin_script), {"PLUGIN_OWN"})

    def test_names_match_what_a_run_is_given(self):
        from core.executor import resolve_secrets_for_run

        for mode in (Script.InjectionMode.ALL, Script.InjectionMode.SELECTED):
            with self.subTest(mode=mode):
                script = self._script(injection_mode=mode, owner_plugin="demo")
                SecretGrant.objects.create(script=script, secret=Secret.objects.get(key="LEGACY"), active=True)
                self.assertEqual(
                    secret_names_visible_to(script), set(resolve_secrets_for_run(self._run(script)))
                )

    def test_editor_names(self):
        script = self._script(injection_mode=Script.InjectionMode.SELECTED, owner_plugin="demo")
        names = editor_secret_names(script, self.ws.pk)
        self.assertEqual(names, {"all": ["IN_WS", "LEGACY"], "owned": ["PLUGIN_OWN"], "visible": ["PLUGIN_OWN"]})
        # A new script: what All gives.
        self.assertEqual(editor_secret_names(None, self.ws.pk)["visible"], ["IN_WS", "LEGACY"])

    def test_collisions(self):
        script = self._script({"IN_WS": "x", "OTHER_WS": "y", "MINE": "z"})
        self.assertEqual(env_var_collisions(script), ["IN_WS"])

    def test_no_encryption_means_no_secrets(self):
        with mock.patch("core.services.EncryptionService.is_configured", return_value=False):
            self.assertEqual(secret_names_visible_to(self._script()), set())
            self.assertEqual(editor_secret_names(None, self.ws.pk), {"all": [], "owned": [], "visible": []})


# --------------------------------------------------------------------------- #
# The editor's Env vars tab
# --------------------------------------------------------------------------- #


class EditorTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        _setup_wizard_off(self)
        self.user = User.objects.create(email="envvars@example.com")
        WorkspaceMembership.objects.update_or_create(
            user=self.user, workspace=self.ws,
            defaults={"role": WorkspaceMembership.ROLE_OWNER},
        )
        self.client.force_login(self.user)

    def _payload(self, script, rows=None, **overrides):
        data = {
            "name": script.name,
            "description": "",
            "code": "pass",
            "inputs_schema": "",
            "environment": str(self.env.pk),
            "timeout_seconds": "3600",
            "is_enabled": "on",
            "isolation_mode": "inherit",
            "injection_mode": "all",
            "notify_on": "never",
            "run_mode": "manual",
            "timezone": "UTC",
        }
        if rows is not None:
            data["env_vars"] = "1"
            data["env_vars_name"] = [n for n, _ in rows]
            data["env_vars_value"] = [v for _, v in rows]
        data.update(overrides)
        return data

    def _post(self, script, rows=None, follow=False, **overrides):
        return self.client.post(
            reverse("cpanel:script_edit", args=[script.pk]),
            self._payload(script, rows, **overrides),
            follow=follow,
        )

    def test_the_editor_renders_the_tab_with_saved_rows_sorted(self):
        script = self._script({"ZONE": "b", "ALPHA": "a"})
        resp = self.client.get(reverse("cpanel:script_edit", args=[script.pk]))
        self.assertContains(resp, 'id="editor-tab-env"')
        self.assertContains(resp, 'name="env_vars" value="1"')
        body = resp.content.decode()
        self.assertLess(body.index('value="ALPHA"'), body.index('value="ZONE"'))
        self.assertContains(resp, "never masked in output")
        self.assertContains(resp, "js/script_env")
        self.assertContains(resp, 'id="env-secret-names"')

    def test_saving_rows(self):
        script = self._script()
        resp = self._post(script, [("REGION", "eu"), ("", ""), ("FLAG", "")])
        self.assertEqual(resp.status_code, 302)
        script.refresh_from_db()
        self.assertEqual(script.env_vars, {"REGION": "eu", "FLAG": ""})

    def test_removing_every_row_clears_them(self):
        script = self._script({"REGION": "eu"})
        self.assertEqual(self._post(script, []).status_code, 302)
        script.refresh_from_db()
        self.assertEqual(script.env_vars, {})

    def test_a_form_without_the_tab_keeps_the_saved_rows(self):
        script = self._script({"REGION": "eu"})
        self.assertEqual(self._post(script, rows=None).status_code, 302)
        script.refresh_from_db()
        self.assertEqual(script.env_vars, {"REGION": "eu"})

    def test_a_bad_name_is_refused_and_the_tab_opens(self):
        script = self._script({"REGION": "eu"})
        resp = self._post(script, [("MY-VAR", "typed value"), ("OK", "1")])
        self.assertEqual(resp.status_code, 200)
        self.assertIn("env_vars", resp.context["form"].errors)
        self.assertContains(resp, 'id="editor-tab-env" aria-controls="editor-panel-env" aria-selected="true"')
        self.assertContains(resp, 'value="typed value"')  # what was typed is kept
        self.assertNotContains(resp, 'value="REGION"')
        script.refresh_from_db()
        self.assertEqual(script.env_vars, {"REGION": "eu"})

    def test_the_create_form_saves_rows(self):
        form = ScriptForm(
            {
                "name": "new", "code": "pass", "environment": str(self.env.pk),
                "timeout_seconds": "60", "isolation_mode": "inherit", "notify_on": "never",
                "env_vars": "1", "env_vars_name": ["A"], "env_vars_value": ["1"],
            },
            workspace=self.ws,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["env_vars"], {"A": "1"})

    def test_saving_a_name_a_secret_has_warns(self):
        _secret("API_KEY", "s3cr3t", self.ws)
        script = self._script()
        resp = self._post(script, [("API_KEY", "plain"), ("REGION", "eu")], follow=True)
        self.assertContains(resp, "A secret this script gets has the same name as API_KEY")
        self.assertNotContains(resp, "s3cr3t")

    def test_selected_mode_warns_only_for_attached_secrets(self):
        _secret("API_KEY", "s3cr3t", self.ws)
        script = self._script(injection_mode=Script.InjectionMode.SELECTED)
        resp = self._post(script, [("API_KEY", "plain")], injection_mode="selected", follow=True)
        self.assertNotContains(resp, "has the same name as")

    def test_the_row_warning_shows_for_a_saved_clash(self):
        _secret("API_KEY", "s3cr3t", self.ws)
        script = self._script({"API_KEY": "plain", "REGION": "eu"})
        resp = self.client.get(reverse("cpanel:script_edit", args=[script.pk]))
        body = resp.content.decode()
        # Exactly one row warning is visible: the API_KEY row's.
        self.assertEqual(body.count('<p data-env-warning class="text-xs text-warn">'), 1)
        self.assertNotIn("s3cr3t", body)

    def test_the_detail_page_lists_the_names(self):
        script = self._script({"ZONE": "secretish-value", "ALPHA": "a"})
        resp = self.client.get(reverse("cpanel:script_detail", args=[script.pk]))
        self.assertContains(resp, "ALPHA, ZONE")
        self.assertNotContains(resp, "secretish-value")


# --------------------------------------------------------------------------- #
# Fork and backup
# --------------------------------------------------------------------------- #


class ForkAndBackupTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create(email="admin-envvars@example.com", is_superuser=True)

    def test_fork_copies_the_env_vars(self):
        source = self._script({"REGION": "eu", "FLAG": "on"})
        fork = fork_script(source, user=self.user)
        fork.refresh_from_db()
        self.assertEqual(fork.env_vars, {"REGION": "eu", "FLAG": "on"})

    def test_backup_round_trips_the_env_vars(self):
        script = self._script({"REGION": "eu", "EMPTY": ""})
        plain = self._script()
        data = BackupService.create_backup(include_runs=False)
        exported = {s["id"]: s for s in data["scripts"]}
        self.assertEqual(exported[str(script.id)]["env_vars"], {"REGION": "eu", "EMPTY": ""})
        self.assertEqual(exported[str(plain.id)]["env_vars"], {})

        result = BackupService.restore_backup(data, restore_runs=False, current_user=self.user)
        self.assertTrue(result["success"], result)
        self.assertEqual(Script.objects.get(pk=script.pk).env_vars, {"REGION": "eu", "EMPTY": ""})
        self.assertEqual(Script.objects.get(pk=plain.pk).env_vars, {})

    def test_an_older_backup_restores_none(self):
        script = self._script({"REGION": "eu"})
        data = BackupService.create_backup(include_runs=False)
        for row in data["scripts"]:
            row.pop("env_vars")
        result = BackupService.restore_backup(data, restore_runs=False, current_user=self.user)
        self.assertTrue(result["success"], result)
        self.assertEqual(Script.objects.get(pk=script.pk).env_vars, {})

    def test_restore_drops_entries_the_editor_would_refuse(self):
        script = self._script({"REGION": "eu"})
        data = BackupService.create_backup(include_runs=False)
        row = next(s for s in data["scripts"] if s["id"] == str(script.id))
        row["env_vars"] = {"REGION": "eu", "PYRUNNER_DB_PATH": "/x", "BAD-NAME": "1", "N": 3}
        result = BackupService.restore_backup(data, restore_runs=False, current_user=self.user)
        self.assertTrue(result["success"], result)
        self.assertEqual(Script.objects.get(pk=script.pk).env_vars, {"REGION": "eu"})
