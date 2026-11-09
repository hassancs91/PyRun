"""
Registry endpoints as Py AI tools, with console/channel tiers (2.5 Stage 3, ticket T16).

What this pins, roughly in order of how much it would hurt to get wrong:

- A chat-channel agent (Telegram) never gets ``runs-output``: not as a tool,
  and not through the registry either, even with a context that holds
  ``runs:output`` (the registry checks the caller's Py AI tier on every call).
- Py AI stays read-only: no run or write endpoint becomes a tool, with API
  writes on or off.
- Each endpoint declares its tier and the registry refuses anything else;
  console Py AI gets every read endpoint, channels the ``channel`` ones.
- Tools answer with the endpoint's own result (masked the same way), scoped to
  the agent's workspace; a manual run's user is an id, never an email. An
  answer over ``MAX_RESULT_CHARS`` is a ``too_large`` tool error instead.
- Tool names, descriptions and schemas come from the registry; the system
  prompt names the new areas and still says read-only.
- The runtime hands the SDK the eight built-in tools plus the registry tools of
  the agent's tier (the eight are pinned in core/test_pyai.py, unchanged).

No test calls a model: the SDK's ``query`` is stubbed where the runtime runs.
"""

import json
import uuid
from unittest import mock

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, TestCase

from core.api.context import CallContext
from core.api.errors import ApiError
from core.api.registry import PYAI_TIERS, Endpoint, Registry, registry
from core.api.tools import (
    MAX_RESULT_CHARS,
    agent_context,
    build_tools,
    permissions_for,
    tier_for,
)
from core.models import (
    DataStore,
    DataStoreEntry,
    Environment,
    GlobalSettings,
    Run,
    Script,
    Secret,
    User,
    Workspace,
)
from core.services.encryption_service import EncryptionService
from core.services.pyai import runtime
from core.services.pyai import tools as pyai_tools
from core.services.run_events import manual_event

CONSOLE, CHANNEL = "pyai", "telegram:42"
SCRIPT_TOOLS = {"scripts-list", "scripts-get"}
RUN_TOOLS = {"runs-list", "runs-get"}
DATASTORE_TOOLS = {"datastores-list", "datastores-get", "datastores-entries", "datastores-entry"}
CHANNEL_TOOLS = SCRIPT_TOOLS | RUN_TOOLS | DATASTORE_TOOLS
CONSOLE_TOOLS = CHANNEL_TOOLS | {"runs-output"}
NEVER = {"scripts-run", "runs-stop", "datastores-set", "datastores-delete"}
CODE = "print('the secret sauce')  # CODE-MARKER-16"
SECRET_VALUE = "t16-s3cr3t-value"
EMAIL = "owner@t16.example"


def names(tools):
    return {t.name for t in tools}


def call(tools, name, params=None):
    """Call one bridge tool the way the SDK does; returns (is_error, payload)."""
    tool = next(t for t in tools if t.name == name)
    result = async_to_sync(tool.handler)(params or {})
    return bool(result.get("isError")), json.loads(result["content"][0]["text"])


class TierDeclarationTests(SimpleTestCase):
    INPUT = {"type": "object", "properties": {}, "additionalProperties": False}

    def spec(self, slug="t16.read", tier="console", **kwargs):
        return Endpoint(slug, lambda c, p: {}, self.INPUT, "T", "", kwargs.get("access", "read"),
                        kwargs.get("permission", "t16:read"), (60, 60), "", "", tier)

    def test_registry_refuses_an_unknown_tier(self):
        for tier in ("", "public", "Channel", None):
            with self.subTest(tier=tier), self.assertRaises(ValueError):
                Registry().register(self.spec(tier=tier))
        for tier in PYAI_TIERS:
            Registry().register(self.spec(tier=tier))

    def test_default_tier_is_console(self):
        r = Registry()
        r.register(Endpoint("t16.plain", lambda c, p: {}, self.INPUT, "T", "", "read",
                            "t16:read", (60, 60)))
        self.assertEqual(r.get("t16.plain").pyai_tier, "console")

    def test_allows_tier(self):
        console, channel = self.spec(tier="console"), self.spec(tier="channel")
        for tier in (None, "console"):
            self.assertTrue(console.allows_tier(tier))
            self.assertTrue(channel.allows_tier(tier))
        self.assertFalse(console.allows_tier("channel"))
        self.assertTrue(channel.allows_tier("channel"))
        self.assertFalse(console.allows_tier("something-else"))

    def test_real_endpoints_declare_the_planned_tiers(self):
        tiers = {e.slug: e.pyai_tier for e in registry.reads()}
        # The safe subset (plan Stage 3: no code, output, databases.query, secret names).
        for slug in ("scripts.list", "scripts.get", "runs.list", "runs.get", "datastores.list",
                     "datastores.get", "datastores.entries", "datastores.entry"):
            self.assertEqual(tiers[slug], "channel", slug)
        self.assertEqual(tiers["runs.output"], "console")
        # Brain reads stay open to channel agents: their own Brain grants decide.
        self.assertTrue(all(t == "channel" for s, t in tiers.items() if s.startswith("brain.")))
        self.assertEqual(registry.get("brain.propose-feed").pyai_tier, "channel")

    def test_agent_tiers(self):
        self.assertEqual(tier_for("pyai"), "console")
        for agent in ("telegram:1", "slack:x", "", "PYAI"):
            self.assertEqual(tier_for(agent), "channel", agent)

    def test_channel_context_never_holds_output_or_brain(self):
        self.assertIn("runs:output", permissions_for("console"))
        channel = permissions_for("channel")
        self.assertNotIn("runs:output", channel)
        self.assertTrue({"scripts:read", "runs:read", "datastores:read"} <= channel)
        for tier in PYAI_TIERS:
            self.assertFalse({"brain:read", "brain:write"} & permissions_for(tier))
            # No run or write permission, ever.
            self.assertFalse({"scripts:run", "runs:stop", "datastores:write"}
                             & permissions_for(tier))


class _Fixture:
    def setUp(self):
        self.ws = Workspace.get_default()
        self.other = Workspace.objects.create(name="Other T16 workspace")
        self.env = Environment.objects.create(name="t16env", path=f"t16env{uuid.uuid4().hex[:6]}")
        self.alpha = Script.objects.create(name="Alpha report", code=CODE, environment=self.env,
                                           workspace=self.ws, description="Daily sales")
        self.foreign = Script.objects.create(name="Foreign", code=CODE, environment=self.env,
                                             workspace=self.other)
        self.user = User.objects.create_user(username="t16owner", email=EMAIL, password="x")
        self.run = Run.objects.create(
            script=self.alpha, workspace=self.ws, status=Run.Status.FAILED, exit_code=2,
            stdout="hello from alpha\n", stderr="Traceback\nValueError: t16",
            result={"rows": 3}, event=manual_event(self.user), triggered_by=self.user,
        )
        self.foreign_run = Run.objects.create(script=self.foreign, workspace=self.other,
                                              status=Run.Status.SUCCESS, stdout="foreign out")
        self.store = DataStore.objects.create(name="cfg", workspace=self.ws)
        entry = DataStoreEntry(datastore=self.store, key="limits")
        entry.set_value({"daily": 5})
        entry.save()
        DataStore.objects.create(name="hidden", workspace=self.other)


class BridgeToolsTests(_Fixture, TestCase):
    def test_console_gets_every_read_and_channel_the_safe_subset(self):
        console, channel = names(build_tools(self.ws)), names(build_tools(self.ws, agent=CHANNEL))
        self.assertTrue(CONSOLE_TOOLS <= console, CONSOLE_TOOLS - console)
        self.assertTrue(CHANNEL_TOOLS <= channel, CHANNEL_TOOLS - channel)
        self.assertNotIn("runs-output", channel)
        self.assertEqual(console - channel, {"runs-output"})
        # Brain tools come from core.brain.tools (grant-checked), not from here.
        self.assertFalse(any(n.startswith("brain-") for n in console | channel))

    def test_no_run_or_write_endpoint_becomes_a_tool(self):
        for writes in (False, True):
            gs = GlobalSettings.get_settings()
            gs.api_writes_enabled = writes
            gs.save()
            for agent in (CONSOLE, CHANNEL):
                with self.subTest(writes=writes, agent=agent):
                    tools = build_tools(self.ws, agent=agent)
                    self.assertFalse(NEVER & names(tools))
                    for tool in tools:
                        spec = registry.get(tool.name.replace("-", ".", 1))
                        self.assertEqual(spec.access, "read")

    def test_names_descriptions_and_schemas_come_from_the_registry(self):
        for tool in build_tools(self.ws):
            spec = registry.get(tool.name.replace("-", ".", 1))
            self.assertEqual(tool.name, spec.mcp_name)
            self.assertEqual(tool.description, spec.description or spec.title)
            self.assertEqual(tool.input_schema, spec.input)

    def test_channel_cannot_call_run_output_even_with_the_permission(self):
        params = {"run": str(self.run.id)}
        with self.assertRaises(ApiError) as caught:
            registry.call("runs.output", agent_context(self.ws, CHANNEL), params)
        self.assertEqual((caught.exception.code, caught.exception.status), ("forbidden", 403))
        # The tier is checked on its own, not only through the permission set.
        forged = CallContext(workspace=self.ws, principal="agent", principal_id=CHANNEL,
                             permissions=frozenset({"runs:output"}), pyai_tier="channel")
        with self.assertRaises(ApiError) as caught:
            registry.call("runs.output", forged, params)
        self.assertEqual(caught.exception.code, "forbidden")
        # The same call is fine for the console tier.
        data = registry.call("runs.output", agent_context(self.ws, CONSOLE), params)
        self.assertEqual(data["text"], "hello from alpha\n")

    def test_console_calls_scripts_runs_output_and_datastores(self):
        tools = build_tools(self.ws)
        error, data = call(tools, "scripts-list")
        self.assertFalse(error)
        self.assertEqual([s["name"] for s in data["scripts"]], ["Alpha report"])
        error, data = call(tools, "scripts-get", {"script": str(self.alpha.id)})
        self.assertFalse(error)
        self.assertEqual(data["name"], "Alpha report")
        self.assertNotIn("code", data)
        self.assertNotIn("CODE-MARKER", json.dumps(data))
        error, data = call(tools, "runs-list")
        self.assertEqual([r["id"] for r in data["runs"]], [str(self.run.id)])
        error, data = call(tools, "runs-get", {"run": str(self.run.id)})
        self.assertFalse(error)
        self.assertEqual((data["status"], data["result"]), ("failed", {"rows": 3}))
        error, data = call(tools, "runs-output", {"run": str(self.run.id), "stream": "stderr"})
        self.assertFalse(error)
        self.assertEqual(data["text"], "Traceback\nValueError: t16")
        error, data = call(tools, "datastores-list")
        self.assertEqual([s["name"] for s in data["datastores"]], ["cfg"])
        error, data = call(tools, "datastores-entry", {"name": "cfg", "key": "limits"})
        self.assertFalse(error)
        self.assertEqual(data["value"], {"daily": 5})
        error, data = call(tools, "datastores-entries", {"name": "cfg"})
        self.assertEqual([e["key"] for e in data["entries"]], ["limits"])

    def test_channel_calls_the_safe_subset(self):
        tools = build_tools(self.ws, agent=CHANNEL)
        error, data = call(tools, "runs-get", {"run": str(self.run.id)})
        self.assertFalse(error)
        self.assertEqual(data["output"]["stdout"]["chars"], len("hello from alpha\n"))
        self.assertNotIn("hello from alpha", json.dumps(data))
        error, data = call(tools, "datastores-get", {"name": "cfg"})
        self.assertFalse(error)
        self.assertEqual(data["entry_count"], 1)
        error, data = call(tools, "scripts-get", {"script": str(self.alpha.id)})
        self.assertFalse(error)

    def test_tools_answer_the_handler_result_in_the_agent_workspace(self):
        for agent in (CONSOLE, CHANNEL):
            tools = build_tools(self.ws, agent=agent)
            with self.subTest(agent=agent):
                _, data = call(tools, "runs-get", {"run": str(self.run.id)})
                direct = registry.call("runs.get", agent_context(self.ws, agent),
                                       {"run": str(self.run.id)})
                self.assertEqual(data, json.loads(json.dumps(direct)))
                # Another workspace's script, run and store are unknown.
                for name, params in (
                    ("scripts-get", {"script": str(self.foreign.id)}),
                    ("runs-get", {"run": str(self.foreign_run.id)}),
                    ("datastores-get", {"name": "hidden"}),
                ):
                    error, payload = call(tools, name, params)
                    self.assertTrue(error, name)
                    self.assertEqual(payload["error"]["code"], "not_found")
        # The other workspace's agent sees its own data only.
        _, data = call(build_tools(self.other), "scripts-list")
        self.assertEqual([s["name"] for s in data["scripts"]], ["Foreign"])

    def test_bad_input_and_unknown_ids_are_tool_errors(self):
        tools = build_tools(self.ws)
        error, payload = call(tools, "runs-get", {})
        self.assertTrue(error)
        self.assertEqual(payload["error"]["code"], "invalid_input")
        error, payload = call(tools, "runs-get", {"run": str(uuid.uuid4())})
        self.assertEqual((error, payload["error"]["code"]), (True, "not_found"))

    def test_an_answer_too_long_for_the_model_is_a_tool_error_that_says_how_to_narrow_it(self):
        run = Run.objects.create(script=self.alpha, workspace=self.ws, status=Run.Status.SUCCESS,
                                 stdout="x" * (MAX_RESULT_CHARS + 10))
        tools = build_tools(self.ws)
        error, payload = call(tools, "runs-output", {"run": str(run.id)})
        self.assertTrue(error)
        self.assertEqual(payload["error"]["code"], "too_large")
        self.assertIn("tail", payload["error"]["message"])
        error, data = call(tools, "runs-output", {"run": str(run.id), "tail": 500})
        self.assertFalse(error)
        self.assertEqual((len(data["text"]), data["tail_cut"]), (500, True))

    def test_manual_run_user_is_an_id_never_an_email(self):
        for agent in (CONSOLE, CHANNEL):
            with self.subTest(agent=agent):
                _, data = call(build_tools(self.ws, agent=agent), "runs-get",
                               {"run": str(self.run.id)})
                self.assertEqual(data["event"]["user"], {"id": self.user.pk})
                self.assertNotIn(EMAIL, json.dumps(data))

    def test_inputs_are_masked_through_the_bridge(self):
        Secret.objects.create(workspace=self.ws, key="API_KEY",
                              encrypted_value=EncryptionService.encrypt(SECRET_VALUE))
        run = Run.objects.create(script=self.alpha, workspace=self.ws,
                                 status=Run.Status.SUCCESS, inputs={"city": SECRET_VALUE})
        with mock.patch("core.executor.ClaudeService.get_script_env", return_value={}):
            _, data = call(build_tools(self.ws, agent=CHANNEL), "runs-get", {"run": str(run.id)})
        self.assertEqual(data["inputs"], {"city": "[API_KEY:MASKED]"})
        self.assertNotIn(SECRET_VALUE, json.dumps(data))


class RuntimeTierTests(_Fixture, TestCase):
    """What the runtime hands the SDK, per tier (the model call is stubbed)."""

    def drive(self, agent):
        import claude_agent_sdk as sdk

        seen = {}

        async def query(prompt, options):
            seen["options"] = options
            if False:
                yield None

        with mock.patch.object(sdk, "query", query):
            async_to_sync(runtime.PyAIService._drive)(
                "p", "s", "", {"ANTHROPIC_API_KEY": "k"}, self.ws, agent=agent
            )
        allowed = seen["options"].allowed_tools
        prefix = f"mcp__{pyai_tools.SERVER_NAME}__"
        self.assertTrue(all(name.startswith(prefix) for name in allowed))
        return {name[len(prefix):] for name in allowed}

    def test_console_gets_the_eight_tools_and_every_registry_read(self):
        allowed = self.drive(CONSOLE)
        self.assertTrue(set(pyai_tools.TOOL_NAMES) <= allowed)
        self.assertTrue(CONSOLE_TOOLS <= allowed)
        self.assertFalse(NEVER & allowed)

    def test_channel_gets_the_safe_subset(self):
        allowed = self.drive(CHANNEL)
        self.assertTrue(CHANNEL_TOOLS <= allowed)
        self.assertNotIn("runs-output", allowed)
        self.assertNotIn("get_run_error", allowed)
        self.assertTrue(set(pyai_tools.TOOL_NAMES) - {"get_run_error"} <= allowed)
        self.assertFalse(NEVER & allowed)

    def test_prompt_names_the_areas_and_stays_read_only(self):
        for plain_text in (False, True):
            prompt = runtime.base_system_prompt(self.ws, plain_text=plain_text)
            self.assertIn("You can only READ", prompt)
            self.assertIn("no access to secrets", prompt)
            for area in ("scripts", "runs", "schedules", "datastores", "runs-output",
                         "never their code"):
                self.assertIn(area, prompt)
