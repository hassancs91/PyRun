"""
Workspace inventory and system health on the unified registry, for REST, MCP and
Py AI (2.5 Stage 3, ticket T18).

What this pins, roughly in order of how much it would hurt to get wrong:

- No endpoint returns a secret value, a provider key or base URL, a channel's
  bot token, inbound token or signing secret, or a library's code: one sweep
  calls all nine with every permission and looks for each of them.
- ``scripts.code`` needs ``scripts:read_code``, masks the values of the secrets
  the script gets (and withholds the code when a secret can't be resolved), and
  is console-only: Py AI on a chat channel can neither list nor call it. Nor
  ``secrets.names``, ``providers.list`` or ``channels.list``.
- ``secrets.names`` names the secrets each script gets with the run resolver's
  selection (All mode, Selected mode's attached and plugin-owned secrets, the
  rows that belong to no workspace), and with ``script`` answers "does script X
  have a SERPER key?". A script-bound token names its own script only.
- ``system.health`` says what needs attention: no worker, schedules paused, a
  maintenance window on, a failed backup, a plugin that didn't load, failed runs;
  and ``ok`` when there is nothing.
- Each list is the caller's workspace's where the thing belongs to one
  (channels, libraries, schedules, usage), the instance's otherwise.
- The two acceptance questions through the console bridge.
- ``providers.list`` and ``plugins.list`` list the whole instance, so (like the
  console) they answer only a token a superuser created; ``environments.list``
  answers any permitted token (Hasan's decision 4 after run 4).

No test calls a model; tools are called through their handlers.
"""

import datetime
import json
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from core.api.context import CallContext
from core.api.errors import ApiError
from core.api.mcp import LEGACY
from core.api.registry import registry
from core.api.tools import agent_context, build_tools, permissions_for
from core.models import (
    AIProvider,
    APIToken,
    Brain,
    Channel,
    ChannelMember,
    ClaudeUsage,
    EmbeddingProvider,
    Environment,
    GlobalSettings,
    Library,
    MaintenanceWindow,
    Plugin,
    Run,
    Script,
    ScriptLibrary,
    ScriptSchedule,
    Secret,
    SecretGrant,
    Workspace,
)
from core.services import EncryptionService
from core.services.pyai import runtime
from core.test_plugin_api import _mock_setup
from core.test_pyai_registry_tools import call, names

ACCEPT = "application/json, text/event-stream"
CONSOLE, CHANNEL = "pyai", "telegram:18"
SERPER = "t18-serper-value-0042"
OPENAI = "t18-openai-value-0043"
ACME = "t18-acme-value-0044"
LEGACY_VALUE = "t18-legacy-value-0045"
FOREIGN_VALUE = "t18-foreign-value-0046"
AI_KEY = "sk-ant-t18-provider-key"
AI_URL = "http://10.20.30.40:11434"
EMBED_KEY = "t18-embedding-key-0047"
BOT_TOKEN = "123456:t18-bot-token-ABCDEF"
SIGNING = "t18-inbound-signing-secret"
LIB_CODE = "def helper():\n    return 'LIB-CODE-MARKER-18'\n"
CODE = f"import os\nKEY = '{SERPER}'  # CODE-MARKER-18\nprint(os.environ['SERPER_API_KEY'])\n"
SLUGS = {
    "providers.list": ("providers:read", "console"),
    "secrets.names": ("secrets:read_names", "console"),
    "channels.list": ("channels:read", "console"),
    "plugins.list": ("plugins:read", "channel"),
    "environments.list": ("environments:read", "channel"),
    "libraries.list": ("libraries:read", "channel"),
    "schedules.list": ("schedules:read", "channel"),
    "scripts.code": ("scripts:read_code", "console"),
    "system.health": ("system:read", "channel"),
}
ALL = sorted({p for p, _ in SLUGS.values()})
CONSOLE_ONLY = {"scripts-code", "secrets-names", "providers-list", "channels-list"}
CHANNEL_TOO = {"plugins-list", "environments-list", "libraries-list", "schedules-list",
               "system-health"}


class DeclarationTests(SimpleTestCase):
    def test_reads_permissions_and_tiers(self):
        offered = {p for p, _ in APIToken.API_PERMISSIONS}
        for slug, (permission, tier) in SLUGS.items():
            spec = registry.get(slug)
            self.assertEqual((spec.access, spec.permission, spec.pyai_tier),
                             ("read", permission, tier), slug)
            self.assertIn(permission, offered, slug)
            self.assertIn(permission, registry.read_permissions(), slug)
        channel = permissions_for("channel")
        self.assertFalse({"scripts:read_code", "secrets:read_names", "providers:read",
                          "channels:read"} & channel)
        self.assertTrue({"plugins:read", "environments:read", "libraries:read",
                         "schedules:read", "system:read"} <= channel)
        self.assertTrue(set(ALL) <= permissions_for("console"))
        # A call with environment runs pip list: the slower rate of the heavy reads.
        self.assertEqual(registry.get("environments.list").rate, (30, 60))


class _Fixture:
    def setUp(self):
        _mock_setup(self)
        self.ws = Workspace.get_default() or Workspace.objects.create(name="T18", is_default=True)
        self.other = Workspace.objects.create(name="Other T18 workspace")
        self.env = Environment.objects.create(name="t18env", path=f"t18env{uuid.uuid4().hex[:6]}",
                                              python_version="3.12", requirements="requests\n# x\n")
        self.alpha = self._script("Alpha report", code=CODE)
        self.beta = self._script("Beta sync", injection_mode="selected")
        self.plugged = self._script("Acme job", injection_mode="selected", owner_plugin="acme")
        self.archived = self._script("Old one", archived_at=timezone.now())
        self.foreign = self._script("Foreign", workspace=self.other)
        self.serper = self._secret("SERPER_API_KEY", SERPER, description="Search API")
        self._secret("OPENAI_API_KEY", OPENAI)
        self._secret("ACME_TOKEN", ACME, owner_plugin="acme")
        self._secret("LEGACY_KEY", LEGACY_VALUE, workspace=None)
        self._secret("FOREIGN_KEY", FOREIGN_VALUE, workspace=self.other)
        SecretGrant.objects.create(script=self.beta, secret=self.serper)
        self.admin = get_user_model().objects.create_superuser(
            username=f"t18-{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@t18.test",
            password="x",
        )

    def _script(self, name, **kwargs):
        kwargs.setdefault("workspace", self.ws)
        kwargs.setdefault("code", "print(1)")
        return Script.objects.create(name=name, environment=self.env, **kwargs)

    def _secret(self, key, value, workspace="ws", **kwargs):
        return Secret.objects.create(
            key=key, workspace=self.ws if workspace == "ws" else workspace,
            encrypted_value=EncryptionService.encrypt(value), **kwargs)

    def api_token(self, permissions, **kwargs):
        kwargs.setdefault("created_by", self.admin)
        raw = APIToken.generate_token(scope="api")
        APIToken.objects.create(name=f"t{uuid.uuid4().hex[:6]}", scope="api", token=raw,
                                workspace=self.ws, permissions=permissions, **kwargs)
        return raw

    def rest(self, slug, params=None, raw=None):
        return self.client.get("/api/v1/" + slug, params or {},
                               HTTP_AUTHORIZATION="Bearer " + (raw or self.raw))

    def data(self, slug, params=None, raw=None):
        response = self.rest(slug, params, raw)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]

    def error(self, slug, params=None, raw=None):
        response = self.rest(slug, params, raw)
        return response.status_code, response.json()["error"]["code"]

    def rpc(self, method="tools/list", params=None, raw=None):
        return self.client.post(
            "/mcp", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
            content_type="application/json", HTTP_ACCEPT=ACCEPT,
            HTTP_MCP_PROTOCOL_VERSION=LEGACY[0], HTTP_AUTHORIZATION="Bearer " + (raw or self.raw),
        )


# --------------------------------------------------------------------------- secrets


class SecretNamesTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.raw = self.api_token(["secrets:read_names"])

    def using(self, data):
        return {s["name"]: sorted(x["name"] for x in s["scripts"]) for s in data["secrets"]}

    def test_names_owners_and_the_scripts_that_get_them(self):
        data = self.data("secrets.names")
        self.assertEqual(self.using(data), {
            # All mode: every user secret, unscoped legacy rows included.
            "OPENAI_API_KEY": ["Alpha report"],
            "LEGACY_KEY": ["Alpha report"],
            # Selected mode: what's attached; a plugin script also its plugin's own.
            "SERPER_API_KEY": ["Alpha report", "Beta sync"],
            "ACME_TOKEN": ["Acme job"],
        })
        serper = next(s for s in data["secrets"] if s["name"] == "SERPER_API_KEY")
        self.assertEqual((serper["owner_plugin"], serper["source"], serper["description"],
                          serper["script_count"]), (None, "local", "Search API", 2))
        acme = next(s for s in data["secrets"] if s["name"] == "ACME_TOKEN")
        self.assertEqual(acme["owner_plugin"], "acme")
        self.assertEqual(data["count"], 4)
        self.assertNotIn("script", data)

    def test_one_script_answers_whether_it_has_a_key(self):
        data = self.data("secrets.names", {"script": str(self.beta.id)})
        self.assertEqual([s["name"] for s in data["secrets"]], ["SERPER_API_KEY"])
        self.assertEqual(data["script"], {"id": str(self.beta.id), "name": "Beta sync",
                                          "injection_mode": "selected"})
        names_of = {str(self.plugged.id): ["ACME_TOKEN"],
                    str(self.alpha.id): ["LEGACY_KEY", "OPENAI_API_KEY", "SERPER_API_KEY"]}
        for script, expected in names_of.items():
            got = self.data("secrets.names", {"script": script})["secrets"]
            self.assertEqual([s["name"] for s in got], expected)
        for script in (self.foreign.id, self.archived.id, uuid.uuid4(), "nope"):
            self.assertEqual(self.error("secrets.names", {"script": str(script)}),
                             (404, "not_found"))

    def test_a_script_bound_token_names_its_own_script_only(self):
        raw = self.api_token(["secrets:read_names"], script=self.beta)
        data = self.data("secrets.names", raw=raw)
        self.assertEqual(self.using(data)["SERPER_API_KEY"], ["Beta sync"])
        self.assertEqual(self.using(data)["OPENAI_API_KEY"], [])
        self.assertEqual(self.error("secrets.names", {"script": str(self.alpha.id)}, raw),
                         (404, "not_found"))

    def test_nothing_is_injected_without_encryption(self):
        with mock.patch.object(EncryptionService, "is_configured", return_value=False):
            data = self.data("secrets.names")
        self.assertEqual({s["script_count"] for s in data["secrets"]}, {0})

    def test_the_permission_is_needed(self):
        raw = self.api_token(["scripts:read"])
        self.assertEqual(self.error("secrets.names", raw=raw), (403, "forbidden"))


# --------------------------------------------------------------------------- scripts.code


class ScriptCodeTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.raw = self.api_token(["scripts:read_code", "scripts:read"])

    def test_code_comes_back_with_secret_values_masked(self):
        with mock.patch("core.executor.ClaudeService.get_script_env", return_value={}):
            response = self.rest("scripts.code", {"script": str(self.alpha.id)})
        self.assertNotIn(SERPER.encode(), response.content)
        data = response.json()["data"]
        self.assertIn("CODE-MARKER-18", data["code"])
        self.assertIn("KEY = '[SERPER_API_KEY:MASKED]'", data["code"])
        self.assertEqual((data["name"], data["lines"], data["withheld"]), ("Alpha report", 3, []))
        # scripts.get still never has it.
        got = self.rest("scripts.get", {"script": str(self.alpha.id)})
        self.assertNotIn(b"CODE-MARKER", got.content)

    def test_a_secret_that_cant_be_resolved_withholds_the_code(self):
        from core.services.secret_backends import SecretResolutionError

        with mock.patch("core.executor.mask_set_for_display",
                        side_effect=SecretResolutionError("vault down")):
            data = self.data("scripts.code", {"script": str(self.alpha.id)})
        self.assertEqual((data["code"], data["lines"], data["withheld"]), (None, None, ["code"]))

    def test_permission_binding_and_workspace(self):
        raw = self.api_token(["scripts:read"])
        self.assertEqual(self.error("scripts.code", {"script": str(self.alpha.id)}, raw),
                         (403, "forbidden"))
        bound = self.api_token(["scripts:read_code"], script=self.beta)
        self.assertEqual(self.error("scripts.code", {"script": str(self.alpha.id)}, bound),
                         (404, "not_found"))
        with mock.patch("core.executor.ClaudeService.get_script_env", return_value={}):
            self.assertEqual(self.data("scripts.code", raw=bound)["name"], "Beta sync")
        for script in (self.foreign, self.archived):
            self.assertEqual(self.error("scripts.code", {"script": str(script.id)}),
                             (404, "not_found"))


# --------------------------------------------------------------------------- the inventory


class InstanceWideListTests(_Fixture, TestCase):
    """Providers and plugins answer only a token a superuser created."""

    def setUp(self):
        super().setUp()
        Plugin.objects.create(slug="acme", name="Acme", version="1.0.0", status="active", manifest={})
        self.member = get_user_model().objects.create_user(
            username=f"m-{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@t18.test", password="x"
        )

    def test_a_token_made_by_a_non_superuser_or_nobody_is_refused(self):
        for created_by in (self.member, None):
            raw = self.api_token(ALL, created_by=created_by)
            for slug in ("providers.list", "plugins.list"):
                with self.subTest(slug=slug, created_by=created_by):
                    response = self.rest(slug, raw=raw)
                    self.assertEqual(response.status_code, 403)
                    error = response.json()["error"]
                    self.assertEqual(error["code"], "forbidden")
                    self.assertIn("created by a superuser", error["message"])
                    self.assertIn(slug, error["message"])
            # environments.list stays open to any permitted token.
            self.assertTrue(self.data("environments.list", raw=raw)["environments"])

    def test_a_superusers_token_answers_until_its_creator_is_demoted(self):
        raw = self.api_token(ALL)
        self.assertEqual([p["slug"] for p in self.data("plugins.list", raw=raw)["plugins"]], ["acme"])
        self.assertIn("ai", self.data("providers.list", raw=raw))
        get_user_model().objects.filter(pk=self.admin.pk).update(is_superuser=False)
        self.assertEqual(self.error("plugins.list", raw=raw), (403, "forbidden"))

    def test_over_mcp_too(self):
        raw = self.api_token(ALL, created_by=self.member)
        response = self.rpc("tools/call", {"name": "plugins-list", "arguments": {}}, raw=raw)
        self.assertEqual(response.status_code, 200)
        body = response.json()["result"]
        self.assertTrue(body["isError"])
        self.assertIn("created by a superuser", json.dumps(body))

    def test_py_ai_keeps_its_tiers(self):
        for tools in (build_tools(self.ws), build_tools(self.ws, agent=CHANNEL)):
            error, data = call(tools, "plugins-list")
            self.assertFalse(error, data)
            self.assertEqual([p["slug"] for p in data["plugins"]], ["acme"])


class InventoryTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.raw = self.api_token(ALL)

    def test_providers_never_keys_or_urls_and_usage_is_this_workspaces_today(self):
        ollama = AIProvider(provider_type="ollama", name="Local GLM", base_url=AI_URL,
                            default_model="glm-5", credential_encrypted=EncryptionService.encrypt(AI_KEY))
        ollama.save()
        AIProvider.objects.create(provider_type="anthropic", name="Claude", default_model="")
        gs = GlobalSettings.get_settings()
        gs.active_ai_provider = ollama
        gs.claude_enabled = True
        gs.save()
        embed = EmbeddingProvider(provider_type="openai", name="Small", model="text-embedding-3-small",
                                  base_url="https://api.openai.com", dimension=1536)
        embed.set_credential(EMBED_KEY)
        embed.save()
        brain = Brain.objects.create(workspace=self.ws, name="B", slug="b-t18")
        for script_id, brain_id, tokens in ((self.alpha.pk, None, 10), (None, brain.pk, 5),
                                           (self.foreign.pk, None, 1000), (None, None, 2000)):
            ClaudeUsage.objects.create(script_id=script_id, brain_id=brain_id, model="glm-5",
                                       provider="ollama", input_tokens=tokens, output_tokens=1)
        old = ClaudeUsage.objects.create(script_id=self.alpha.pk, model="glm-5", input_tokens=500)
        ClaudeUsage.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=2))
        response = self.rest("providers.list")
        for leak in (AI_KEY, AI_URL, EMBED_KEY, "api.openai.com"):
            self.assertNotIn(leak.encode(), response.content, leak)
        data = response.json()["data"]
        ai = data["ai"]
        self.assertEqual((ai["enabled"], ai["default"]), (True, "Local GLM"))
        self.assertEqual([(p["name"], p["type"], p["model"], p["default"]) for p in ai["providers"]],
                         [("Claude", "anthropic", None, False), ("Local GLM", "ollama", "glm-5", True)])
        usage = ai["usage_today"]
        self.assertEqual((usage["calls"], usage["input_tokens"], usage["output_tokens"]), (2, 15, 2))
        self.assertEqual(usage["by_model"], [{"provider": "ollama", "model": "glm-5", "calls": 2,
                                              "input_tokens": 15, "output_tokens": 2}])
        self.assertEqual(data["embedding"]["providers"], [{
            "name": "Small", "type": "openai", "model": "text-embedding-3-small",
            "dimension": 1536, "enabled": True, "tested_at": None}])

    def test_channels_never_tokens_and_this_workspaces_only(self):
        ops = self._channel("Ops", self.ws, inbound_handler="script",
                            inbound_target_id=self.alpha.pk, daily_reply_cap=10,
                            last_error=f"POST https://api.telegram.org/bot{BOT_TOKEN}/sendMessage")
        for status in ("approved", "approved", "pending"):
            ChannelMember.objects.create(channel=ops, sender_id=uuid.uuid4().hex, status=status)
        self._channel("Elsewhere", self.other)
        response = self.rest("channels.list")
        for leak in (BOT_TOKEN, ops.inbound_token, SIGNING):
            self.assertNotIn(leak.encode(), response.content, leak)
        [channel] = response.json()["data"]["channels"]
        self.assertEqual(channel["name"], "Ops")
        self.assertEqual(channel["inbound"], {
            "enabled": True, "handler": "script", "access": "approval",
            "script": {"id": str(self.alpha.pk), "name": "Alpha report"}})
        self.assertEqual(channel["members"], {"pending": 1, "approved": 2, "blocked": 0})
        self.assertEqual((channel["configured"], channel["daily_reply_cap"],
                          channel["daily_cap_reached"]), (True, 10, False))
        self.assertIn("[bot_token:MASKED]", channel["last_error"])

    def test_plugins_environments_and_packages(self):
        Plugin.objects.create(slug="acme", name="Acme", version="1.2.0", status="active",
                              manifest={"summary": "Does acme things", "author": "Acme Inc"})
        other_env = Environment.objects.create(name="t18env", path=f"t18b{uuid.uuid4().hex[:6]}")
        plugins = self.data("plugins.list")["plugins"]
        self.assertEqual([(p["slug"], p["version"], p["status"], p["summary"], p["author"])
                          for p in plugins], [("acme", "1.2.0", "active", "Does acme things",
                                               "Acme Inc")])
        listed = {e["id"]: e for e in self.data("environments.list")["environments"]}
        mine = listed[str(self.env.pk)]
        # Alpha, Beta and Acme job: not the archived script, not the other workspace's.
        self.assertEqual((mine["requirements"], mine["scripts"], mine["python_version"]),
                         (["requests"], 3, "3.12"))
        self.assertNotIn("packages", mine)
        packages = [{"name": "requests", "version": "2.32.0"}, {"name": "Babel", "version": "2"}]
        with mock.patch("core.services.environment_service.EnvironmentService"
                        ".get_installed_packages", return_value=packages) as pip:
            self.assertEqual(self.error("environments.list", {"environment": "t18env"}),
                             (400, "invalid_input"))
            [one] = self.data("environments.list", {"environment": str(other_env.pk)})["environments"]
        self.assertEqual([p["name"] for p in one["packages"]], ["Babel", "requests"])
        pip.assert_called_once_with(other_env)
        self.assertEqual(self.error("environments.list", {"environment": "missing"}),
                         (404, "not_found"))

    def test_libraries_modules_and_attached_scripts_never_code(self):
        lib = Library.objects.create(key="t18_lib", name="T18 lib", workspace=self.ws)
        lib.save_revision({"helpers.py": LIB_CODE, "__init__.py": "# t18"})
        ScriptLibrary.objects.create(script=self.alpha, library=lib)
        ScriptLibrary.objects.create(script=self.foreign, library=lib)
        Library.objects.create(key="t18_hidden", name="Hidden", workspace=self.other)
        response = self.rest("libraries.list")
        self.assertNotIn(b"LIB-CODE-MARKER", response.content)
        [entry] = response.json()["data"]["libraries"]
        self.assertEqual((entry["key"], entry["version"], entry["modules"], entry["scripts"]),
                         ("t18_lib", 1, ["__init__.py", "helpers.py"],
                          [{"id": str(self.alpha.pk), "name": "Alpha report"}]))

    def test_schedules_paused_and_filtered(self):
        ScriptSchedule.objects.create(script=self.alpha, workspace=self.ws, run_mode="daily",
                                      daily_times=["09:00"], timezone="Asia/Tokyo", is_active=True)
        ScriptSchedule.objects.create(script=self.beta, workspace=self.ws, run_mode="interval",
                                      interval_minutes=30, is_active=False)
        ScriptSchedule.objects.create(script=self.plugged, workspace=self.ws, run_mode="manual")
        ScriptSchedule.objects.create(script=self.foreign, workspace=self.other, run_mode="daily",
                                      daily_times=["09:00"], is_active=True)
        data = self.data("schedules.list")
        self.assertEqual([(s["script"]["name"], s["mode"], s["active"]) for s in data["schedules"]],
                         [("Alpha report", "daily", True), ("Beta sync", "interval", False)])
        self.assertEqual(data["schedules"][0]["timezone"], "Asia/Tokyo")
        self.assertEqual((data["paused"], data["paused_at"]), (False, None))
        active = self.data("schedules.list", {"active": "true"})["schedules"]
        self.assertEqual([s["script"]["name"] for s in active], ["Alpha report"])
        GlobalSettings.objects.filter(pk=1).update(schedules_paused=True,
                                                   schedules_paused_at=timezone.now())
        self.assertTrue(self.data("schedules.list")["paused"])

    def test_settings_and_catalog_name_them(self):
        from django.urls import reverse

        from core.models import User

        catalog = self.client.get("/api/v1/_catalog", HTTP_AUTHORIZATION="Bearer " + self.raw)
        slugs = {e["slug"] for e in catalog.json()["data"]["endpoints"]}
        self.assertTrue(set(SLUGS) <= slugs, set(SLUGS) - slugs)
        self.client.force_login(
            User.objects.create(email="t18-settings@example.com", is_superuser=True))
        page = self.client.get(reverse("cpanel:settings"))
        self.assertContains(page, "vectors.*, providers.*, secrets.*, channels.*, connections.*, plugins.*, "
                                  "environments.*, libraries.*, schedules.*, system.*")

    def test_every_endpoint_over_mcp_and_no_secret_anywhere(self):
        """The negative sweep: every value, key and token in the fixture stays out."""
        AIProvider(provider_type="custom", name="Custom", base_url=AI_URL,
                   credential_encrypted=EncryptionService.encrypt(AI_KEY)).save()
        embed = EmbeddingProvider(provider_type="openai", name="Small", model="m",
                                  base_url="https://api.openai.com")
        embed.set_credential(EMBED_KEY)
        embed.save()
        channel = self._channel("Ops", self.ws, last_error=f"bad {BOT_TOKEN}")
        lib = Library.objects.create(key="t18_lib", name="T18 lib", workspace=self.ws)
        lib.save_revision({"helpers.py": LIB_CODE})
        leaks = (SERPER, OPENAI, ACME, LEGACY_VALUE, FOREIGN_VALUE, AI_KEY, AI_URL, EMBED_KEY,
                 BOT_TOKEN, SIGNING, channel.inbound_token, "LIB-CODE-MARKER")
        tools = {t["name"] for t in self.rpc().json()["result"]["tools"]}
        self.assertTrue({s.replace(".", "-") for s in SLUGS} <= tools)
        with mock.patch("core.executor.ClaudeService.get_script_env", return_value={}), \
             mock.patch("core.services.environment_service.EnvironmentService"
                        ".get_installed_packages", return_value=[]):
            for slug in SLUGS:
                params = {"script": str(self.alpha.pk)} if slug == "scripts.code" else {}
                rest = self.rest(slug, params)
                self.assertEqual(rest.status_code, 200, (slug, rest.content))
                mcp = self.rpc("tools/call", {"name": slug.replace(".", "-"), "arguments": params})
                self.assertFalse(mcp.json()["result"].get("isError"), slug)
                for leak in leaks:
                    self.assertNotIn(leak.encode(), rest.content, (slug, leak))
                    self.assertNotIn(leak.encode(), mcp.content, (slug, leak))

    def _channel(self, name, workspace, **kwargs):
        channel = Channel(workspace=workspace, provider="telegram", name=name,
                          inbound_enabled=True, **kwargs)
        channel.set_credentials({"bot_token": BOT_TOKEN}, identity=f"{name}{BOT_TOKEN}")
        channel.ensure_inbound_token()
        channel.set_inbound_secret(SIGNING)
        channel.save()
        return channel


# --------------------------------------------------------------------------- system.health


class HealthTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.raw = self.api_token(["system:read"])
        GlobalSettings.objects.filter(pk=GlobalSettings.get_settings().pk).update(
            worker_heartbeat_at=timezone.now())

    def test_all_clear(self):
        data = self.data("system.health")
        self.assertEqual((data["ok"], data["issues"]), (True, []))
        self.assertTrue(data["worker"]["alive"])
        self.assertFalse(data["schedules"]["paused"])
        self.assertFalse(data["maintenance"]["active"])
        self.assertEqual(data["runs"], {"running": 0, "waiting": 0, "failed_last_24h": 0})
        from pyrunner.version import __version__
        self.assertEqual((data["version"]["running"], data["version"]["update_available"]),
                         (__version__, False))

    def test_everything_that_needs_attention(self):
        GlobalSettings.objects.filter(pk=GlobalSettings.get_settings().pk).update(
            worker_heartbeat_at=timezone.now() - datetime.timedelta(hours=1),
            schedules_paused=True, schedules_paused_at=timezone.now(),
            s3_backup_enabled=True, s3_backup_last_status="failed",
            s3_backup_last_error="secret bucket detail", update_latest_version="999.0.0")
        MaintenanceWindow.objects.create(
            name="Upgrade", kind=MaintenanceWindow.Kind.ONE_OFF, blocks=["scheduled"],
            starts_at=timezone.now() - datetime.timedelta(minutes=5),
            ends_at=timezone.now() + datetime.timedelta(minutes=30))
        Plugin.objects.create(slug="broken", name="Broken", status="errored",
                              error_message="Traceback detail")
        Run.objects.create(script=self.alpha, workspace=self.ws, status=Run.Status.FAILED)
        Run.objects.create(script=self.alpha, workspace=self.ws, status=Run.Status.PENDING)
        Run.objects.create(script=self.foreign, workspace=self.other, status=Run.Status.FAILED)
        response = self.rest("system.health")
        for detail in (b"secret bucket detail", b"Traceback detail"):
            self.assertNotIn(detail, response.content)
        data = response.json()["data"]
        self.assertFalse(data["ok"])
        self.assertFalse(data["worker"]["alive"])
        self.assertEqual(data["maintenance"]["windows"], ["Upgrade"])
        self.assertEqual(data["backup"]["last_status"], "failed")
        self.assertEqual((data["version"]["latest"], data["version"]["update_available"]),
                         ("999.0.0", True))
        self.assertEqual(data["plugins"]["errored"], ["Broken"])
        self.assertEqual(data["runs"], {"running": 0, "waiting": 1, "failed_last_24h": 1})
        issues = " ".join(data["issues"])
        for words in ("No worker is running", "Schedules are paused", 'window Upgrade',
                      "scheduled runs", "backup failed", "Broken", "1 run failed"):
            self.assertIn(words, issues)
        self.assertEqual(len(data["issues"]), 6)


# --------------------------------------------------------------------------- Py AI


class PyAITests(_Fixture, TestCase):
    def test_console_gets_all_nine_and_channels_the_safe_ones(self):
        console = names(build_tools(self.ws))
        channel = names(build_tools(self.ws, agent=CHANNEL))
        self.assertTrue(CONSOLE_ONLY | CHANNEL_TOO <= console)
        self.assertTrue(CHANNEL_TOO <= channel)
        self.assertFalse(CONSOLE_ONLY & channel)

    def test_the_registry_refuses_a_channel_even_with_the_permission(self):
        for slug in ("scripts.code", "secrets.names", "providers.list", "channels.list"):
            permission = SLUGS[slug][0]
            for ctx in (agent_context(self.ws, CHANNEL),
                        CallContext(workspace=self.ws, principal="agent", principal_id=CHANNEL,
                                    permissions=frozenset({permission}), pyai_tier="channel")):
                with self.assertRaises(ApiError) as caught:
                    registry.call(slug, ctx, {"script": str(self.alpha.pk)}
                                  if slug == "scripts.code" else {})
                self.assertEqual(caught.exception.code, "forbidden", slug)

    def test_acceptance_does_script_x_have_a_serper_key(self):
        tools = build_tools(self.ws)
        error, data = call(tools, "secrets-names", {"script": str(self.beta.pk)})
        self.assertFalse(error, data)
        self.assertEqual([s["name"] for s in data["secrets"]], ["SERPER_API_KEY"])
        error, data = call(tools, "secrets-names", {"script": str(self.plugged.pk)})
        self.assertNotIn("SERPER_API_KEY", [s["name"] for s in data["secrets"]])
        self.assertNotIn(SERPER, json.dumps(data))

    def test_acceptance_is_everything_ok(self):
        GlobalSettings.objects.filter(pk=GlobalSettings.get_settings().pk).update(
            worker_heartbeat_at=timezone.now(), schedules_paused=True)
        error, data = call(build_tools(self.ws, agent=CHANNEL), "system-health")
        self.assertFalse(error, data)
        self.assertFalse(data["ok"])
        self.assertEqual(data["issues"], ["Schedules are paused (Pause all): no schedule starts runs."])

    def test_prompt_names_the_new_areas_and_stays_read_only(self):
        prompt = runtime.base_system_prompt(self.ws)
        for words in ("scripts-code", "secrets-names", "system-health", "libraries",
                      "environments", "plugins", "AI providers", "chat channels",
                      "You can only READ", "never see secret values or provider keys"):
            self.assertIn(words, prompt)
