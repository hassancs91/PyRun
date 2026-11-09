"""
Scripts as MCP tools (2.5 Stage 2, ticket T14).

What this pins, roughly in order of how much it would hurt to get wrong:

- Only a token that may run a script sees it as a tool: ``scripts:run``, the
  token's workspace and its script binding. Disabled, archived and unexposed
  scripts aren't listed; a call to a tool the token can't see is "unknown".
- Calling the tool is ``scripts.run`` with ``wait``: the same inputs check (no
  run on bad inputs), the same 30-a-minute count as REST ``scripts.run``, the
  same audit row (slug ``scripts.run``), the result when the run ends in time,
  else the run id with a hint to poll ``runs.get``.
- Each tool's ``inputSchema`` is the script's schema as stored (declared
  order), or an empty object schema; every listed tool has the shape MCP
  clients validate (object schema, name in [A-Za-z0-9_.-]) and no tool name
  can clash with a registry tool.
- The editor's switch and tool name: a blank name comes from the script's
  name, an exposed name is unique per workspace (form check + DB constraints).
- ``_catalog`` lists the tools under ``script_tools`` with ``kind: "script"``
  and leaves ``endpoints`` as it was; Connect explains the feature.
- Backups keep the switch and the name; a fork is never a tool.
- End to end: an MCP client over real HTTP initializes, lists the tool, calls
  it with inputs, and a real script's (masked) result comes back.
"""

import json
import re
import sys
import urllib.error
import urllib.request
import uuid
from unittest import mock

from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.test import LiveServerTestCase, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.api import script_tools
from core.api.context import token_context
from core.api.mcp import LEGACY, META, MODERN
from core.api.registry import registry
from core.executor import execute_run
from core.forms import ScriptForm
from core.models import (
    ApiCall,
    APIToken,
    Environment,
    GlobalSettings,
    Run,
    Script,
    User,
    Workspace,
    WorkspaceMembership,
)
from core.services import run_wait
from core.services.backup_service import BackupService
from core.services.script_service import fork_script
from core.test_plugin_api import _mock_setup
from core.test_run_reports import _ai_off, _secrets

ACCEPT = "application/json, text/event-stream"
# Declared in an order jsonb would change (it sorts keys by length, then bytes).
SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string", "default": "Beirut", "description": "Where"},
        "days": {"type": "integer", "minimum": 1},
    },
    "required": ["days"],
    "additionalProperties": False,
}
RUNNER = ["scripts:read", "scripts:run", "runs:read"]
# What an MCP client accepts as a tool name (MCP spec) and as an input schema.
MCP_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class NameRuleTests(SimpleTestCase):
    def test_default_names_come_from_the_script_name(self):
        cases = {
            "Send weekly report!": "send_weekly_report",
            "  Héllo   wörld ": "hello_world",
            "3D render": "script_3d_render",
            "fetch-prices v2": "fetch_prices_v2",
            "日本語": "",
            "": "",
        }
        for name, expected in cases.items():
            self.assertEqual(script_tools.default_name(name), expected, name)
        long = script_tools.default_name("word " * 30)
        self.assertLessEqual(len(long), Script.TOOL_NAME_MAX_LENGTH)
        self.assertFalse(long.endswith("_"))
        self.assertTrue(script_tools.NAME_RE.fullmatch(long))

    def test_no_tool_name_can_clash_with_a_registry_tool(self):
        # Registry tools are <namespace>-<name>; a script tool name has no hyphen.
        names = [spec.mcp_name for spec in registry._endpoints.values()]
        self.assertIn("scripts-run", names)
        for name in names:
            self.assertFalse(script_tools.NAME_RE.fullmatch(name), name)
        for bad in ("Send", "send-report", "_x", "1x", "x y", "x\n", ""):
            self.assertFalse(script_tools.NAME_RE.fullmatch(bad), bad)


class _Fixture:
    def setUp(self):
        _mock_setup(self)
        self.ws = Workspace.get_default()
        self.other = Workspace.objects.create(name="Other T14 workspace")
        self.env = Environment.objects.create(name="t14env", path=f"t14env{uuid.uuid4().hex[:6]}")
        self.report = self._script("Weekly report", inputs_schema=SCHEMA, expose_as_tool=True,
                                   tool_name="weekly_report", description="Builds the report.")
        self.ping = self._script("Ping", expose_as_tool=True, tool_name="ping")
        self.hidden = self._script("Hidden", tool_name="hidden")
        self.raw, self.token = self._token(RUNNER)
        settings = GlobalSettings.get_settings()
        settings.worker_heartbeat_at = timezone.now()
        settings.save(update_fields=["worker_heartbeat_at"])
        queued = mock.patch("core.tasks.async_task", return_value="t14-task")
        queued.start()
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

    def rpc(self, method, params=None, raw=None, version=LEGACY[0]):
        params = dict(params or {})
        headers = {}
        if version == MODERN:
            params["_meta"] = {META + "protocolVersion": MODERN, META + "clientCapabilities": {}}
            headers["HTTP_MCP_METHOD"] = method
            if method == "tools/call":
                headers["HTTP_MCP_NAME"] = params["name"]
        return self.client.post(
            "/mcp", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            content_type="application/json", HTTP_ACCEPT=ACCEPT,
            HTTP_MCP_PROTOCOL_VERSION=version,
            HTTP_AUTHORIZATION="Bearer " + (raw or self.raw), **headers,
        )

    def tools(self, raw=None):
        response = self.rpc("tools/list", raw=raw)
        self.assertEqual(response.status_code, 200, response.content)
        return {t["name"]: t for t in response.json()["result"]["tools"]}

    def call(self, name, arguments=None, raw=None, version=LEGACY[0]):
        return self.rpc("tools/call", {"name": name, "arguments": arguments or {}},
                        raw=raw, version=version)

    def finish_on_sleep(self, **fields):
        """A worker stand-in: the wait's first sleep ends the run (``fields``)."""

        def sleep(_seconds):
            Run.objects.filter(status__in=[Run.Status.PENDING, Run.Status.RUNNING]).update(
                status=Run.Status.SUCCESS, ended_at=timezone.now(), **fields
            )

        return mock.patch.object(run_wait, "_sleep", side_effect=sleep)


class ToolsListTests(_Fixture, TestCase):
    def test_exposed_scripts_are_listed_after_the_registry_tools(self):
        response = self.rpc("tools/list")
        names = [t["name"] for t in response.json()["result"]["tools"]]
        self.assertEqual(names[-2:], ["ping", "weekly_report"])
        self.assertIn("scripts-run", names)
        self.assertEqual(len(names), len(set(names)))
        tools = self.tools()
        self.assertNotIn("hidden", tools)
        report = tools["weekly_report"]
        self.assertEqual(report["title"], "Weekly report")
        self.assertEqual(report["description"], "Builds the report.")
        self.assertEqual(report["annotations"], {"readOnlyHint": False})
        # The schema as stored, in declared order (kept on both engines).
        self.assertEqual(report["inputSchema"], SCHEMA)
        self.assertEqual(list(report["inputSchema"]["properties"]), ["city", "days"])
        # No inputs: an object schema that takes none, and a description.
        self.assertEqual(tools["ping"]["inputSchema"],
                         {"type": "object", "properties": {}, "additionalProperties": False})
        self.assertEqual(tools["ping"]["description"], "Runs the PyRun script “Ping”.")
        for tool in tools.values():
            self.assertTrue(MCP_TOOL_NAME.fullmatch(tool["name"]), tool["name"])
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertIsInstance(tool["description"], str)

    def test_only_tokens_that_may_run_the_script_see_it(self):
        reader_raw, _ = self._token(["scripts:read", "runs:read"])
        self.assertFalse({"weekly_report", "ping"} & set(self.tools(raw=reader_raw)))
        bound_raw, _ = self._token(RUNNER, script=self.ping)
        bound = self.tools(raw=bound_raw)
        self.assertIn("ping", bound)
        self.assertNotIn("weekly_report", bound)
        foreign_raw, _ = self._token(RUNNER, workspace=self.other)
        self.assertFalse({"weekly_report", "ping"} & set(self.tools(raw=foreign_raw)))
        self._script("Theirs", workspace=self.other, expose_as_tool=True, tool_name="ping")
        self.assertIn("ping", self.tools(raw=foreign_raw))

    def test_disabled_and_archived_scripts_are_not_listed(self):
        Script.objects.filter(pk=self.ping.pk).update(is_enabled=False)
        Script.objects.filter(pk=self.report.pk).update(archived_at=timezone.now())
        self.assertFalse({"weekly_report", "ping"} & set(self.tools()))

    def test_modern_protocol_lists_and_calls_a_script_tool(self):
        response = self.rpc("tools/list", version=MODERN)
        self.assertIn("ping", [t["name"] for t in response.json()["result"]["tools"]])
        with self.finish_on_sleep():
            response = self.call("ping", version=MODERN)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.json()["result"]["isError"])
        self.assertEqual(response.json()["result"]["structuredContent"]["wait"], "finished")


class ToolsCallTests(_Fixture, TestCase):
    def test_a_call_runs_the_script_with_inputs_and_returns_the_result(self):
        with self.finish_on_sleep(result={"total": 42}, exit_code=0):
            response = self.call("weekly_report", {"days": 3})
        self.assertEqual(response.status_code, 200, response.content)
        result = response.json()["result"]
        self.assertFalse(result["isError"])
        data = result["structuredContent"]
        self.assertEqual((data["status"], data["is_finished"], data["wait"], data["result"]),
                         ("success", True, "finished", {"total": 42}))
        self.assertEqual(json.loads(result["content"][0]["text"]), data)
        run = Run.objects.get(pk=data["run_id"])
        self.assertEqual(run.script, self.report)
        self.assertEqual(run.inputs, {"city": "Beirut", "days": 3})
        self.assertEqual(run.trigger_type, Run.TriggerType.API)
        self.assertEqual({k: run.event[k] for k in ("kind", "token_id", "via")},
                         {"kind": "api", "token_id": str(self.token.pk), "via": "mcp"})
        row = ApiCall.objects.get()
        self.assertEqual((row.slug, row.ok, row.status), ("scripts.run", True, 200))

    def test_a_run_past_the_wait_returns_its_id_and_says_to_poll(self):
        clock = [0.0]

        def sleep(seconds):  # time passes, the run doesn't end
            clock[0] += seconds

        with mock.patch.object(run_wait, "_sleep", side_effect=sleep), \
                mock.patch.object(run_wait, "_clock", side_effect=lambda: clock[0]):
            response = self.call("ping")
        # The MCP budget: 55 s at most, and 3 s before the call's own budget.
        self.assertAlmostEqual(clock[0], run_wait.budget(None, 60), places=3)
        data = response.json()["result"]["structuredContent"]
        self.assertFalse(response.json()["result"]["isError"])
        self.assertEqual((data["status"], data["is_finished"], data["wait"]),
                         ("pending", False, "timeout"))
        self.assertIn("runs.get", data["next"])
        self.assertTrue(Run.objects.filter(pk=data["run_id"]).exists())
        self.assertEqual(ApiCall.objects.get().status, 202)

    def test_bad_inputs_are_a_tool_error_and_no_run(self):
        for arguments in ({}, {"days": 0}, {"days": 1, "extra": True}):
            result = self.call("weekly_report", arguments).json()["result"]
            self.assertTrue(result["isError"], arguments)
            self.assertEqual(result["structuredContent"]["error"]["code"], "invalid_input")
        result = self.call("ping", {"x": 1}).json()["result"]
        self.assertIn("doesn't declare", result["structuredContent"]["error"]["message"])
        self.assertFalse(Run.objects.exists())

    def test_tools_the_token_cannot_see_are_unknown(self):
        reader_raw, _ = self._token(["scripts:read", "runs:read"])
        bound_raw, _ = self._token(RUNNER, script=self.ping)
        foreign_raw, _ = self._token(RUNNER, workspace=self.other)
        archived = self._script("Old", expose_as_tool=True, tool_name="old",
                                archived_at=timezone.now())
        cases = [("weekly_report", reader_raw), ("weekly_report", bound_raw),
                 ("ping", foreign_raw), ("hidden", None), ("old", None), ("nope", None),
                 ("Weekly_Report", None), (archived.tool_name, bound_raw)]
        for name, raw in cases:
            response = self.call(name, raw=raw)
            self.assertEqual(response.json()["error"]["code"], -32602, (name, response.content))
        self.assertFalse(Run.objects.exists())

    def test_a_disabled_script_answers_script_disabled(self):
        Script.objects.filter(pk=self.ping.pk).update(is_enabled=False)
        result = self.call("ping").json()["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "script_disabled")

    def test_tool_calls_share_the_scripts_run_rate_limit(self):
        for _ in range(30):
            response = self.client.post(
                "/api/v1/scripts.run", {"script": str(self.ping.id)},
                content_type="application/json", HTTP_AUTHORIZATION="Bearer " + self.raw)
            self.assertEqual(response.status_code, 202)
        response = self.call("ping")
        self.assertEqual(response.status_code, 429)
        self.assertTrue(response["Retry-After"])
        result = response.json()["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "rate_limited")
        self.assertEqual(ApiCall.objects.filter(code="rate_limited").get().slug, "scripts.run")


class CatalogAndConnectTests(_Fixture, TestCase):
    def catalog(self, raw=None):
        return self.client.get("/api/v1/_catalog",
                               HTTP_AUTHORIZATION="Bearer " + (raw or self.raw)).json()["data"]

    def test_catalog_lists_script_tools_with_kind_script(self):
        data = self.catalog()
        self.assertEqual([t["mcp_name"] for t in data["script_tools"]], ["ping", "weekly_report"])
        entry = data["script_tools"][1]
        self.assertEqual(entry, {
            "kind": "script", "mcp_name": "weekly_report", "title": "Weekly report",
            "description": "Builds the report.", "inputSchema": SCHEMA,
            "script": str(self.report.id), "rest": "scripts.run", "access": "run",
            "permission": "scripts:run",
        })
        # The registry's endpoints are listed as before, without the scripts.
        slugs = [e["slug"] for e in data["endpoints"]]
        self.assertEqual(slugs, [e.slug for e in registry.visible(token_context(self.token))])
        self.assertTrue(all("kind" not in e for e in data["endpoints"]))
        reader_raw, _ = self._token(["scripts:read"])
        self.assertEqual(self.catalog(raw=reader_raw)["script_tools"], [])

    def test_connect_explains_the_feature_and_previews_a_tokens_tools(self):
        user = User.objects.create_superuser(username="t14", email="t14@example.com",
                                             password="x")
        WorkspaceMembership.objects.update_or_create(
            user=user, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_OWNER})
        self.client.force_login(user)
        response = self.client.get(reverse("cpanel:connect"))
        self.assertContains(response, 'id="script-tools"')
        self.assertContains(response, "Expose as MCP tool")
        self.assertContains(response, "mcp__pyrun__weekly_report")
        self.assertContains(response, 'claude "Run my weekly_report tool')
        self.assertContains(response, "The call waits up to 55 seconds")
        with override_settings(MCP_CALL_BUDGET_SECONDS=20):
            response = self.client.get(reverse("cpanel:connect"))
        self.assertContains(response, "The call waits up to 17 seconds")
        preview = self.client.get(reverse("cpanel:connect"), {"token": str(self.token.pk)})
        self.assertEqual([s.tool_name for s in preview.context["script_tools"]],
                         ["ping", "weekly_report"])
        self.assertContains(preview, "script tool")


class EditorTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create(email="t14-editor@example.com", username="t14-editor")
        WorkspaceMembership.objects.update_or_create(
            user=self.user, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_OWNER})
        self.client.force_login(self.user)

    def _data(self, script, **overrides):
        data = {
            "name": script.name, "description": "", "code": "pass", "inputs_schema": "",
            "environment": str(self.env.pk), "timeout_seconds": "3600", "is_enabled": "on",
            "isolation_mode": "inherit", "injection_mode": "all", "notify_on": "never",
            "run_mode": "manual", "timezone": "UTC",
        }
        data.update(overrides)
        return data

    def form(self, script, **overrides):
        script = Script.objects.get(pk=script.pk)  # a fresh instance per form
        return ScriptForm(self._data(script, **overrides), instance=script, workspace=self.ws)

    def test_the_editor_and_the_script_page_show_the_switch_and_name(self):
        response = self.client.get(reverse("cpanel:script_edit", args=[self.report.pk]))
        self.assertContains(response, 'id="mcp-tool"')
        self.assertContains(response, 'name="expose_as_tool"')
        self.assertContains(response, 'name="tool_name"')
        self.assertContains(response, 'value="weekly_report"')
        self.assertContains(response, 'maxlength="40"')
        page = self.client.get(reverse("cpanel:script_detail", args=[self.report.pk]))
        self.assertContains(page, ">weekly_report</span>")
        page = self.client.get(reverse("cpanel:script_detail", args=[self.hidden.pk]))
        self.assertContains(page, "MCP tool")

    def test_saving_with_the_switch_on_and_no_name_uses_the_script_name(self):
        script = self._script("Fetch prices v2")
        response = self.client.post(reverse("cpanel:script_edit", args=[script.pk]),
                                    self._data(script, expose_as_tool="on", tool_name=""))
        self.assertEqual(response.status_code, 302, response.content[:2000])
        script.refresh_from_db()
        self.assertEqual((script.expose_as_tool, script.tool_name), (True, "fetch_prices_v2"))
        self.assertIn("fetch_prices_v2", self.tools())

    def test_create_with_a_typed_name(self):
        data = self._data(Script(name="Brand new"), expose_as_tool="on", tool_name="brand_new")
        response = self.client.post(reverse("cpanel:script_create"), data)
        self.assertEqual(response.status_code, 302, response.content[:2000])
        created = Script.objects.get(name="Brand new")
        self.assertEqual((created.expose_as_tool, created.tool_name, created.workspace),
                         (True, "brand_new", self.ws))

    def test_names_are_checked_and_unique_among_exposed_scripts_in_the_workspace(self):
        form = self.form(self.hidden, expose_as_tool="on", tool_name="Bad-Name")
        self.assertFalse(form.is_valid())
        self.assertIn("lowercase", form.errors["tool_name"][0])
        form = self.form(self.hidden, expose_as_tool="on", tool_name="x" * 41)
        self.assertFalse(form.is_valid())
        # Taken by an exposed script: refused, naming it.
        form = self.form(self.hidden, expose_as_tool="on", tool_name="ping")
        self.assertFalse(form.is_valid())
        self.assertIn('The script "Ping" already uses this tool name', form.errors["tool_name"][0])
        # A blank name made from the script's name is checked too.
        named = self._script("Ping")
        form = self.form(named, expose_as_tool="on", tool_name="")
        self.assertFalse(form.is_valid())
        self.assertIn('the tool name "ping"', form.errors["tool_name"][0])
        # A name nothing can be made from asks for one.
        form = self.form(self._script("日本語"), expose_as_tool="on", tool_name="")
        self.assertFalse(form.is_valid())
        self.assertIn("Give the tool a name", form.errors["tool_name"][0])
        # An archived holder still holds it (unarchiving must not collide).
        Script.objects.filter(pk=self.ping.pk).update(archived_at=timezone.now())
        form = self.form(self.hidden, expose_as_tool="on", tool_name="ping")
        self.assertIn("(archived)", form.errors["tool_name"][0])

    def test_a_name_kept_with_the_switch_off_holds_nothing(self):
        # Off: any name may stay, even one an exposed script uses.
        form = self.form(self.hidden, tool_name="ping")
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        # Turning the report's switch off frees its name for another script.
        form = self.form(self.report, tool_name="weekly_report")
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        form = self.form(self.hidden, expose_as_tool="on", tool_name="weekly_report")
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.hidden.refresh_from_db()
        self.assertEqual((self.hidden.expose_as_tool, self.hidden.tool_name),
                         (True, "weekly_report"))
        # The same name in another workspace is fine.
        self._script("Theirs", workspace=self.other, expose_as_tool=True,
                     tool_name="weekly_report")

    def test_the_database_enforces_the_rules(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._script("Dup", expose_as_tool=True, tool_name="ping")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._script("Nameless", expose_as_tool=True)
        self._script("Unexposed duplicate", tool_name="ping")


class CopyTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create(email="t14-copy@example.com", username="t14-copy")

    def test_a_fork_is_never_a_tool(self):
        fork = fork_script(self.report, user=self.user)
        self.assertEqual((fork.expose_as_tool, fork.tool_name), (False, ""))
        self.assertEqual(fork.inputs_schema, SCHEMA)

    def _restore(self, data):
        data = json.loads(json.dumps(data, cls=DjangoJSONEncoder))
        result = BackupService.restore_backup(data, current_user=self.user)
        self.assertTrue(result["success"], result)
        return result

    def test_a_backup_keeps_the_switch_and_the_name(self):
        data = BackupService.create_backup()
        exported = {s["name"]: s for s in data["scripts"]}
        self.assertEqual((exported["Weekly report"]["expose_as_tool"],
                          exported["Weekly report"]["tool_name"]), (True, "weekly_report"))
        self.assertEqual(self._restore(data)["warnings"], [])
        restored = {s.name: s for s in Script.objects.all()}
        self.assertEqual((restored["Weekly report"].expose_as_tool,
                          restored["Weekly report"].tool_name), (True, "weekly_report"))
        self.assertEqual((restored["Hidden"].expose_as_tool, restored["Hidden"].tool_name),
                         (False, "hidden"))
        self.assertIn("weekly_report", self.tools())

    def test_restore_drops_bad_names_and_never_exposes_a_name_twice(self):
        data = BackupService.create_backup()
        scripts = {s["name"]: s for s in data["scripts"]}
        scripts["Hidden"].update(expose_as_tool=True, tool_name="ping")  # clashes with Ping
        scripts["Weekly report"]["tool_name"] = "Bad-Name"
        # An older backup has neither key.
        scripts["Ping"].pop("expose_as_tool")
        scripts["Ping"].pop("tool_name")
        warnings = self._restore(data)["warnings"]
        # The exposed script that lost its tool is named; nothing else is.
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn('Script "Weekly report" was restored without its MCP tool', warnings[0])
        restored = {s.name: s for s in Script.objects.all()}
        self.assertEqual((restored["Ping"].expose_as_tool, restored["Ping"].tool_name),
                         (False, ""))
        self.assertEqual((restored["Hidden"].expose_as_tool, restored["Hidden"].tool_name),
                         (True, "ping"))
        self.assertEqual((restored["Weekly report"].expose_as_tool,
                          restored["Weekly report"].tool_name), (False, ""))

    def test_restore_keeps_the_first_of_two_scripts_exposing_one_name(self):
        data = BackupService.create_backup()
        scripts = {s["name"]: s for s in data["scripts"]}
        scripts["Hidden"].update(expose_as_tool=True, tool_name="ping")
        warnings = self._restore(data)["warnings"]
        self.assertEqual([w for w in warnings if "MCP tool" in w and '"Hidden"' in w], warnings)
        self.assertEqual(len(warnings), 1)
        holders = Script.objects.filter(expose_as_tool=True, tool_name="ping")
        self.assertEqual([s.name for s in holders], ["Ping"])
        self.assertEqual(Script.objects.get(name="Hidden").tool_name, "ping")


class MCPClient:
    """A minimal MCP client over real HTTP, as Claude Code talks to ``/mcp``:
    ``initialize``, ``notifications/initialized``, then ``tools/list`` and
    ``tools/call`` with the negotiated protocol version in a header."""

    def __init__(self, url, token):
        self.url, self.token, self.version, self.last_id = url, token, None, 0

    def _post(self, body):
        headers = {"Content-Type": "application/json", "Accept": ACCEPT,
                   "Authorization": f"Bearer {self.token}"}
        if self.version:
            headers["MCP-Protocol-Version"] = self.version
        request = urllib.request.Request(self.url, data=json.dumps(body).encode("utf-8"),
                                         headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raise AssertionError(f"HTTP {exc.code}: {exc.read()[:500]!r}") from None

    def request(self, method, params=None):
        self.last_id += 1
        _, body = self._post({"jsonrpc": "2.0", "id": self.last_id, "method": method,
                              "params": params or {}})
        assert body["id"] == self.last_id, body
        if "error" in body:
            raise RuntimeError(body["error"])
        return body["result"]

    def connect(self):
        result = self.request("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "t14-fixture", "version": "1.0"},
        })
        self.version = result["protocolVersion"]
        status, body = self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert (status, body) == (202, None), (status, body)
        return result


class EndToEndTests(LiveServerTestCase):
    """An MCP client lists a script tool, calls it, and a real script runs."""

    host = "127.0.0.1"  # the internal API (set_result) answers loopback only

    def setUp(self):
        _mock_setup(self)
        # An earlier TransactionTestCase's flush removes the migrated default
        # workspace; a token without a workspace is refused (403).
        self.ws = Workspace.get_default() or Workspace.objects.create(
            name="Default Workspace", is_default=True)
        self.env = Environment.objects.create(name="t14e2e", path=f"t14e2e{uuid.uuid4().hex[:6]}")
        self.script = Script.objects.create(
            name="Greeter", environment=self.env, workspace=self.ws, inputs_schema=SCHEMA,
            expose_as_tool=True, tool_name="greeter", description="Greets a city.",
            code=(
                "import os, pyrunner_run\n"
                "args = pyrunner_run.inputs()\n"
                "pyrunner_run.set_result({'greeting': 'Hello ' + args['city'],\n"
                "                         'days': args['days'], 'key': os.environ['API_KEY']})\n"
            ),
        )
        self.raw = APIToken.generate_token(scope="api", kind="bearer")
        APIToken.objects.create(name="e2e", scope="api", token=self.raw, workspace=self.ws,
                                permissions=RUNNER)
        settings = GlobalSettings.get_settings()
        settings.worker_heartbeat_at = timezone.now()
        settings.save(update_fields=["worker_heartbeat_at"])

    def test_list_the_tool_call_it_with_inputs_and_get_the_result(self):
        def worker(_seconds):
            # The worker stand-in: the wait's first poll runs the queued run
            # for real (subprocess + loopback set_result).
            for run in Run.objects.filter(status=Run.Status.PENDING):
                execute_run(run)

        client = MCPClient(self.live_server_url + "/mcp", self.raw)
        with mock.patch("core.executor._validate_environment", return_value=sys.executable), \
                mock.patch("core.tasks.async_task", return_value="t14-e2e"), \
                mock.patch.object(run_wait, "_sleep", side_effect=worker), \
                override_settings(PYRUNNER_INTERNAL_BASE_URL=self.live_server_url), \
                _secrets(), _ai_off():
            self.assertEqual(client.connect()["capabilities"], {"tools": {}})
            tools = {t["name"]: t for t in client.request("tools/list")["tools"]}
            tool = tools["greeter"]
            self.assertEqual(tool["description"], "Greets a city.")
            self.assertEqual(list(tool["inputSchema"]["properties"]), ["city", "days"])
            result = client.request("tools/call", {"name": "greeter",
                                                   "arguments": {"city": "Paris", "days": 3}})
        self.assertFalse(result["isError"], result)
        data = result["structuredContent"]
        expected = {"greeting": "Hello Paris", "days": 3, "key": "[API_KEY:MASKED]"}
        self.assertEqual((data["status"], data["wait"], data["result"]),
                         ("success", "finished", expected))
        self.assertEqual(json.loads(result["content"][0]["text"])["result"], expected)
        run = Run.objects.get(pk=data["run_id"])
        self.assertEqual((run.inputs, run.event["via"]), ({"city": "Paris", "days": 3}, "mcp"))
