"""
Scripts and runs over REST and MCP, read side (2.5 Stage 2, ticket T12).

What this pins, roughly in order of how much it would hurt to get wrong:

- A token bound to one script (``APIToken.script``) sees that script alone:
  ``scripts.list`` returns just it, and every ``runs.*`` call is scoped to it.
  Another script's id, another workspace's and an archived one all answer the
  same 404.
- Token callers see who started a manual run by user id only, never the email
  (Hasan's decision on #12); the console keeps showing the email.
- Script code (and a run's code snapshot) is never returned.
- ``runs.output`` returns exactly the bytes the console run page shows, read
  through the one shared helper (``core.services.run_output``), with the run's
  secrets masked; a real subprocess run proves it. REST answers text/plain.
- Inputs and the event are masked with the run page's helpers, and withheld
  (named in ``withheld``) when a secret can't be resolved.
- ``scripts:read``, ``runs:read`` and ``runs:output`` are selectable, URL
  tokens may hold them (they guard read endpoints only) but no run permission.
- The token form binds a script; ``_catalog``, OpenAPI, ``tools/list`` and the
  Connect page list the new endpoints; a restore preview names script-bound
  tokens (they go with the script, FK cascade).
"""

import json
import sys
import uuid
from datetime import timedelta
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from core.api.mcp import LEGACY
from core.api.registry import registry
from core.executor import execute_run
from core.models import (
    APIToken,
    Brain,
    Environment,
    GlobalSettings,
    Run,
    Script,
    ScriptSchedule,
    Secret,
    Tag,
    User,
    Workspace,
)
from core.services import run_output
from core.services.backup_service import BackupService
from core.services.encryption_service import EncryptionService
from core.services.run_events import manual_event, plugin_event, webhook_event
from core.services.secret_backends import SecretResolutionError
from core.test_plugin_api import _mock_setup

ACCEPT = "application/json, text/event-stream"
READ_ALL = ["scripts:read", "runs:read", "runs:output"]
SLUGS = ("scripts.list", "scripts.get", "runs.list", "runs.get", "runs.output")
CODE = "print('the secret sauce')  # CODE-MARKER-7f3a"
SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string", "default": "Beirut"}, "days": {"type": "integer"}},
}
SECRET_VALUE = "s3cr3t-api-value-91"
EMAIL = "sara@acme.example"


class _Fixture:
    def setUp(self):
        _mock_setup(self)
        self.ws = Workspace.get_default()
        self.other = Workspace.objects.create(name="Other T12 workspace")
        self.env = Environment.objects.create(name="t12env", path=f"t12env{uuid.uuid4().hex[:6]}")
        self.a = self._script("Alpha report", description="Daily sales report",
                              inputs_schema=SCHEMA)
        self.b = self._script("Beta sync", is_enabled=False)
        self.archived = self._script("Gamma old", archived_at=timezone.now())
        self.foreign = self._script("Delta foreign", workspace=self.other)
        tag = Tag.objects.create(name="Reports")
        self.a.tags.add(tag)
        self.full_raw, self.full = self._token(READ_ALL)
        self.bound_raw, self.bound = self._token(READ_ALL, script=self.a)

    def _script(self, name, **kwargs):
        kwargs.setdefault("workspace", self.ws)
        kwargs.setdefault("code", CODE)
        return Script.objects.create(name=name, environment=self.env, **kwargs)

    def _run(self, script, status=Run.Status.SUCCESS, **kwargs):
        return Run.objects.create(script=script, workspace=script.workspace, status=status,
                                  **kwargs)

    def _token(self, permissions, **kwargs):
        raw = APIToken.generate_token(scope="api", kind=kwargs.get("kind", "bearer"))
        token = APIToken.objects.create(
            name=f"t{uuid.uuid4().hex[:6]}", scope="api", token=raw,
            workspace=kwargs.pop("workspace", self.ws), permissions=permissions, **kwargs,
        )
        return raw, token

    def rest(self, slug, params=None, raw=None, method="get"):
        auth = "Bearer " + (raw or self.full_raw)
        if method == "get":
            return self.client.get("/api/v1/" + slug, params or {}, HTTP_AUTHORIZATION=auth)
        return self.client.post("/api/v1/" + slug, params or {},
                                content_type="application/json", HTTP_AUTHORIZATION=auth)

    def data(self, slug, params=None, raw=None, status=200):
        response = self.rest(slug, params, raw)
        self.assertEqual(response.status_code, status, response.content)
        return response.json()["data"] if status == 200 else response.json()["error"]

    def rpc(self, method="tools/list", params=None, raw=None, path="/mcp"):
        return self.client.post(
            path, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
            content_type="application/json", HTTP_ACCEPT=ACCEPT,
            HTTP_MCP_PROTOCOL_VERSION=LEGACY[0],
            **({"HTTP_AUTHORIZATION": "Bearer " + (raw or self.full_raw)} if path == "/mcp" else {}),
        )

    def call(self, tool, arguments, raw=None):
        result = self.rpc("tools/call", {"name": tool, "arguments": arguments}, raw).json()
        return result["result"]


class DeclarationTests(SimpleTestCase):
    def test_endpoints_are_read_class_with_their_own_permissions(self):
        expected = {"scripts.list": "scripts:read", "scripts.get": "scripts:read",
                    "runs.list": "runs:read", "runs.get": "runs:read", "runs.output": "runs:output"}
        for slug, permission in expected.items():
            spec = registry.get(slug)
            self.assertEqual((spec.access, spec.permission), ("read", permission), slug)
        self.assertEqual(registry.get("runs.output").text, "text")
        self.assertTrue({"scripts:read", "runs:read", "runs:output"} <= registry.read_permissions())
        # brain:write guards a run endpoint, so no URL token may hold it.
        self.assertNotIn("brain:write", registry.read_permissions())
        offered = {p for p, _ in APIToken.API_PERMISSIONS}
        self.assertTrue({"scripts:read", "runs:read", "runs:output"} <= offered)

    def test_tail_never_splits_a_character(self):
        text = "line one\nend: é€😀"
        self.assertEqual(run_output.tail(text, 1000), (text, False))
        self.assertEqual(run_output.tail(text, 4), ("😀", True))
        self.assertEqual(run_output.tail(text, 5), ("😀", True))  # half of € dropped
        self.assertEqual(run_output.tail(text, 7), ("€😀", True))
        self.assertTrue(run_output.is_truncated("a\n[OUTPUT TRUNCATED: 5 bytes cut]\nb"))
        with self.assertRaises(ValueError):
            run_output.output_text(Run(stdout="x"), "code_snapshot")


class ListingTests(_Fixture, TestCase):
    def test_catalog_openapi_and_tools_list_the_new_endpoints(self):
        catalog = {e["slug"]: e for e in self.data("_catalog")["endpoints"]}
        for slug in SLUGS:
            self.assertEqual(catalog[slug]["access"], "read")
        self.assertEqual(catalog["runs.output"]["rest_response"], "text/plain")
        self.assertNotIn("rest_response", catalog["runs.get"])
        openapi = self.rest("_openapi.json").json()["paths"]
        self.assertIn("text/plain", openapi["/runs.output"]["get"]["responses"]["200"]["content"])
        self.assertIn("get", openapi["/scripts.list"])
        tools = {t["name"]: t for t in self.rpc().json()["result"]["tools"]}
        for slug in SLUGS:
            self.assertTrue(tools[slug.replace(".", "-")]["annotations"]["readOnlyHint"])
        # A token without the permissions sees none of them.
        brain_raw, _ = self._token(["brain:read"])
        names = {e["slug"] for e in self.data("_catalog", raw=brain_raw)["endpoints"]}
        self.assertFalse(names & set(SLUGS))
        self.assertEqual(self.data("scripts.list", raw=brain_raw, status=403)["code"], "forbidden")
        # runs:read alone does not reach the output.
        reader_raw, _ = self._token(["runs:read"])
        run = self._run(self.a, stdout="hello")
        error = self.data("runs.output", {"run": str(run.id)}, raw=reader_raw, status=403)
        self.assertEqual(error["code"], "forbidden")


class ScriptsTests(_Fixture, TestCase):
    def test_list_is_workspace_scoped_skips_archived_and_filters(self):
        names = [s["name"] for s in self.data("scripts.list")["scripts"]]
        self.assertEqual(names, ["Alpha report", "Beta sync"])
        cases = {
            (("q", "SALES"),): ["Alpha report"],  # description, any case
            (("q", "sync"),): ["Beta sync"],
            (("tag", "reports"),): ["Alpha report"],
            (("enabled", "false"),): ["Beta sync"],
            (("enabled", "true"), ("tag", "Reports")): ["Alpha report"],
            (("q", "Gamma"),): [],  # archived
            (("q", "Delta"),): [],  # another workspace
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                got = self.data("scripts.list", dict(query))["scripts"]
                self.assertEqual([s["name"] for s in got], expected)
        page = self.data("scripts.list", {"limit": "1"})
        self.assertEqual(([s["name"] for s in page["scripts"]], page["next_offset"]),
                         (["Alpha report"], 1))
        page = self.data("scripts.list", {"limit": "1", "offset": "1"})
        self.assertEqual(([s["name"] for s in page["scripts"]], page["next_offset"]),
                         (["Beta sync"], None))
        alpha = self.data("scripts.list")["scripts"][0]
        self.assertEqual(alpha["tags"], ["Reports"])
        self.assertTrue(alpha["has_inputs"])
        self.assertTrue(alpha["enabled"])

    def test_get_returns_metadata_and_never_code(self):
        ScriptSchedule.objects.create(script=self.a, workspace=self.ws, run_mode="daily",
                                      daily_times=["09:00"], timezone="Asia/Tokyo",
                                      is_active=True)
        older = self._run(self.a)
        newest = self._run(self.a, status=Run.Status.FAILED)
        Run.objects.filter(pk=older.pk).update(created_at=timezone.now() - timedelta(hours=1))
        response = self.rest("scripts.get", {"script": str(self.a.id)})
        self.assertNotIn(b"CODE-MARKER", response.content)
        data = response.json()["data"]
        self.assertNotIn("code", data)
        self.assertEqual(data["id"], str(self.a.id))
        self.assertEqual(data["description"], "Daily sales report")
        self.assertEqual(data["inputs_schema"], SCHEMA)
        self.assertEqual(data["schedule"]["mode"], "daily")
        self.assertTrue(data["schedule"]["active"])
        self.assertEqual(data["schedule"]["summary"], "Daily at 09:00 (Asia/Tokyo)")
        self.assertEqual(data["last_run"]["id"], str(newest.id))
        self.assertEqual(data["last_run"]["status"], "failed")
        manual = self.data("scripts.get", {"script": str(self.b.id)})
        self.assertEqual(manual["schedule"], {"mode": "manual", "active": False,
                                              "summary": "Manual", "timezone": None,
                                              "next_run": None})
        self.assertIsNone(manual["last_run"])
        self.assertEqual(manual["inputs_schema"], {})

    def test_unknown_foreign_and_archived_scripts_look_the_same(self):
        for value in (str(uuid.uuid4()), "not-a-uuid", str(self.foreign.id),
                      str(self.archived.id)):
            with self.subTest(value=value):
                error = self.data("scripts.get", {"script": value}, status=404)
                self.assertEqual(error, {"code": "not_found", "message": "Unknown script."})
        error = self.data("scripts.get", status=400)
        self.assertEqual(error["code"], "invalid_input")


class BindingTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.run_a = self._run(self.a, stdout="alpha out")
        self.run_b = self._run(self.b, stdout="beta out")

    def test_a_bound_token_sees_its_script_alone(self):
        raw = self.bound_raw
        listed = self.data("scripts.list", raw=raw)["scripts"]
        self.assertEqual([s["id"] for s in listed], [str(self.a.id)])
        self.assertEqual(self.data("scripts.get", raw=raw)["id"], str(self.a.id))  # no param
        self.assertEqual(self.data("scripts.get", {"script": str(self.b.id)}, raw=raw,
                                   status=404)["code"], "not_found")
        runs = self.data("runs.list", raw=raw)["runs"]
        self.assertEqual([r["id"] for r in runs], [str(self.run_a.id)])
        self.assertEqual(self.data("runs.list", {"script": str(self.b.id)}, raw=raw,
                                   status=404)["code"], "not_found")
        for slug in ("runs.get", "runs.output"):
            with self.subTest(slug=slug):
                self.assertEqual(self.data(slug, {"run": str(self.run_b.id)}, raw=raw,
                                           status=404)["code"], "not_found")
                self.assertEqual(self.rest(slug, {"run": str(self.run_a.id)}, raw).status_code,
                                 200)
        # The same over MCP.
        result = self.call("runs-get", {"run": str(self.run_b.id)}, raw)
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "not_found")
        result = self.call("scripts-list", {}, raw)
        self.assertEqual([s["name"] for s in result["structuredContent"]["scripts"]],
                         ["Alpha report"])
        # Unbound, both scripts are there.
        self.assertEqual(len(self.data("runs.list")["runs"]), 2)

    def test_a_bound_token_whose_script_is_archived_sees_no_scripts(self):
        Script.objects.filter(pk=self.a.pk).update(archived_at=timezone.now())
        self.assertEqual(self.data("scripts.list", raw=self.bound_raw)["scripts"], [])
        self.assertEqual(self.data("scripts.get", raw=self.bound_raw, status=404)["code"],
                         "not_found")
        # Its runs stay readable, as on the console's Runs page.
        self.assertEqual(len(self.data("runs.list", raw=self.bound_raw)["runs"]), 1)

    def test_deleting_the_script_deletes_the_bound_token(self):
        # The archived script's delete confirmation says so first.
        Script.objects.filter(pk=self.a.pk).update(archived_at=timezone.now())
        self.client.force_login(User.objects.create(email="t12-delete@example.com"))
        page = self.client.get(reverse("cpanel:script_detail", args=[self.a.pk]))
        self.assertContains(page, "and the 1 API token bound to it.")
        self.assertNotContains(
            self.client.get(reverse("cpanel:script_detail", args=[self.archived.pk])),
            "API token bound to it",
        )
        self.a.delete()
        self.assertFalse(APIToken.objects.filter(pk=self.bound.pk).exists())
        self.assertTrue(APIToken.objects.filter(pk=self.full.pk).exists())


class RunsTests(_Fixture, TestCase):
    def test_list_filters_and_order(self):
        now = timezone.now()
        old = self._run(self.a, status=Run.Status.FAILED)
        Run.objects.filter(pk=old.pk).update(created_at=now - timedelta(days=2))
        mid = self._run(self.b)
        Run.objects.filter(pk=mid.pk).update(created_at=now - timedelta(hours=1))
        new = self._run(self.a)
        foreign = self._run(self.foreign)
        archived = self._run(self.archived)
        ids = [r["id"] for r in self.data("runs.list")["runs"]]
        self.assertNotIn(str(foreign.id), ids)
        self.assertEqual(set(ids), {str(new.id), str(mid.id), str(old.id), str(archived.id)})
        self.assertEqual(ids[-2:], [str(mid.id), str(old.id)])
        self.assertEqual([r["id"] for r in self.data("runs.list", {"status": "failed"})["runs"]],
                         [str(old.id)])
        by_script = self.data("runs.list", {"script": str(self.a.id)})["runs"]
        self.assertEqual([r["id"] for r in by_script], [str(new.id), str(old.id)])
        # An archived script's runs can be filtered too.
        by_archived = self.data("runs.list", {"script": str(self.archived.id)})["runs"]
        self.assertEqual([r["id"] for r in by_archived], [str(archived.id)])
        since = (now - timedelta(hours=3)).replace(tzinfo=None).isoformat()  # naive = UTC
        recent = {r["id"] for r in self.data("runs.list", {"since": since})["runs"]}
        self.assertEqual(recent, {str(new.id), str(mid.id), str(archived.id)})
        aware = (now - timedelta(minutes=30)).isoformat()
        recent = {r["id"] for r in self.data("runs.list", {"since": aware})["runs"]}
        self.assertEqual(recent, {str(new.id), str(archived.id)})
        self.assertEqual(len(self.data("runs.list", {"limit": "1"})["runs"]), 1)
        for bad in ("yesterday", "2026-13-45T10:00:00"):  # the second parses, then fails
            with self.subTest(since=bad):
                self.assertEqual(self.data("runs.list", {"since": bad}, status=400)["code"],
                                 "invalid_input")
        self.assertEqual(self.data("runs.list", {"status": "done"}, status=400)["code"],
                         "invalid_input")
        self.assertEqual(self.data("runs.list", {"script": str(self.foreign.id)},
                                   status=404)["code"], "not_found")
        item = self.data("runs.list", {"status": "failed"})["runs"][0]
        self.assertEqual(item["script"], {"id": str(self.a.id), "name": "Alpha report"})
        self.assertEqual(item["status"], "failed")

    def test_get_shows_metadata_result_and_no_code(self):
        run = self._run(self.a, result={"rows": 3}, progress={"pct": 100, "message": "done"},
                        code_snapshot=CODE, stdout="x" * 10, stderr="warn",
                        inputs={"city": "Paris"}, event=manual_event())
        response = self.rest("runs.get", {"run": str(run.id)})
        self.assertNotIn(b"CODE-MARKER", response.content)
        data = response.json()["data"]
        self.assertNotIn("code_snapshot", data)
        self.assertEqual(data["result"], {"rows": 3})
        self.assertEqual(data["progress"]["pct"], 100)
        self.assertEqual(data["inputs"], {"city": "Paris"})
        self.assertEqual(data["event"]["kind"], "manual")
        self.assertEqual(data["withheld"], [])
        self.assertEqual(data["output"], {"stdout": {"chars": 10, "truncated": False},
                                          "stderr": {"chars": 4, "truncated": False}})
        self.assertTrue(data["is_finished"])
        for value in (str(uuid.uuid4()), "nope", str(self._run(self.foreign).id)):
            with self.subTest(value=value):
                error = self.data("runs.get", {"run": value}, status=404)
                self.assertEqual(error, {"code": "not_found", "message": "Unknown run."})

    def test_inputs_and_event_are_masked_or_withheld(self):
        Secret.objects.create(workspace=self.ws, key="API_KEY",
                              encrypted_value=EncryptionService.encrypt(SECRET_VALUE))
        event = webhook_event({"body": f"token={SECRET_VALUE}", "method": "POST"})
        run = self._run(self.a, inputs={"city": SECRET_VALUE}, event=event)
        with mock.patch("core.executor.ClaudeService.get_script_env", return_value={}):
            response = self.rest("runs.get", {"run": str(run.id)})
        self.assertNotIn(SECRET_VALUE.encode(), response.content)
        data = response.json()["data"]
        self.assertEqual(data["inputs"], {"city": "[API_KEY:MASKED]"})
        self.assertEqual(data["event"]["body"], "token=[API_KEY:MASKED]")
        with mock.patch("core.executor.mask_set_for_display",
                        side_effect=SecretResolutionError("vault down")):
            data = self.data("runs.get", {"run": str(run.id)})
        self.assertEqual((data["inputs"], data["event"]), (None, None))
        self.assertEqual(sorted(data["withheld"]), ["event", "inputs"])


class ManualRunUserTests(_Fixture, TestCase):
    """Hasan's decision on #12: API/MCP callers get ``user.id``, never the email."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create(email=EMAIL)
        self.manual = self._run(self.a, event=manual_event(self.user), triggered_by=self.user,
                                trigger_type=Run.TriggerType.MANUAL)
        self.plugin = self._run(self.a, event=plugin_event("demo", "job", user=self.user))

    def test_a_bound_token_never_sees_who_ran_it_by_email(self):
        for run in (self.manual, self.plugin):
            with self.subTest(kind=run.event["kind"]):
                response = self.rest("runs.get", {"run": str(run.id)}, self.bound_raw)
                self.assertNotIn(EMAIL.encode(), response.content)
                self.assertEqual(response.json()["data"]["event"]["user"], {"id": self.user.pk})
                mcp = self.rpc("tools/call", {"name": "runs-get", "arguments": {"run": str(run.id)}},
                               self.bound_raw)
                self.assertNotIn(EMAIL.encode(), mcp.content)
                self.assertEqual(mcp.json()["result"]["structuredContent"]["event"]["user"],
                                 {"id": self.user.pk})
        listing = self.rest("runs.list", raw=self.bound_raw)
        self.assertNotIn(EMAIL.encode(), listing.content)
        # The stored event keeps the email: scripts read it, and so does the console.
        self.manual.refresh_from_db()
        self.assertEqual(self.manual.event["user"]["email"], EMAIL)
        self.client.force_login(self.user)
        page = self.client.get(reverse("cpanel:run_detail", args=[self.manual.pk]))
        self.assertContains(page, EMAIL)


@mock.patch("core.executor._validate_environment", return_value=sys.executable)
class OutputTests(_Fixture, TestCase):
    def test_rest_answers_plain_text_and_mcp_a_dict(self, _val):
        run = self._run(self.a, stdout="first\nsecond é\n", stderr="oops\n")
        response = self.rest("runs.output", {"run": str(run.id)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(response.content.decode(), "first\nsecond é\n")
        self.assertTrue(response["X-Request-ID"])
        response = self.rest("runs.output", {"run": str(run.id), "stream": "stderr"}, method="post")
        self.assertEqual(response.content, b"oops\n")
        tail = self.rest("runs.output", {"run": str(run.id), "tail": "4"})
        self.assertEqual(tail.content.decode(), " é\n")  # 4 bytes: space, é (2), newline
        result = self.call("runs-output", {"run": str(run.id), "tail": 8})
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"], {
            "run": str(run.id), "status": "success", "stream": "stdout", "text": "cond é\n",
            "truncated": False, "tail_cut": True,
        })
        error = self.data("runs.output", {"run": str(run.id), "stream": "code"}, status=400)
        self.assertEqual(error["code"], "invalid_input")
        # Errors keep the JSON envelope.
        missing = self.rest("runs.output", {"run": str(uuid.uuid4())})
        self.assertEqual((missing.status_code, missing.json()["error"]["code"]),
                         (404, "not_found"))
        empty = self._run(self.a, status=Run.Status.RUNNING)
        self.assertEqual(self.rest("runs.output", {"run": str(empty.id)}).content, b"")

    def test_output_matches_the_console_byte_for_byte_after_a_real_run(self, _val):
        Secret.objects.create(workspace=self.ws, key="API_KEY",
                              encrypted_value=EncryptionService.encrypt(SECRET_VALUE))
        script = self._script("Printer", code=(
            "import os, sys\n"
            "print('key=' + os.environ['API_KEY'] + ' <b>&amp;</b> done')\n"
            "print('err ' + os.environ['API_KEY'], file=sys.stderr)\n"
        ))
        run = self._run(script, status=Run.Status.PENDING)
        with mock.patch("core.executor.ClaudeService.get_script_env", return_value={}):
            execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.SUCCESS, run.stderr)
        member = User.objects.create(email="t12-console@example.com")
        self.client.force_login(member)
        page = self.client.get(reverse("cpanel:run_detail", args=[run.pk]))
        for stream in run_output.STREAMS:
            with self.subTest(stream=stream):
                api = self.rest("runs.output", {"run": str(run.id), "stream": stream})
                self.assertEqual(api.content.decode("utf-8"), page.context[stream])
                self.assertIn("[API_KEY:MASKED]", page.context[stream])
                self.assertNotIn(SECRET_VALUE.encode(), api.content)
        self.assertIn("key=[API_KEY:MASKED] <b>&amp;</b> done", page.context["stdout"])


class UrlTokenTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        gs = GlobalSettings.get_settings()
        gs.mcp_url_tokens_enabled = True
        gs.save()
        self.expires = timezone.now() + timedelta(days=30)

    def test_url_tokens_may_hold_the_read_permissions_but_no_run_permission(self):
        raw, token = self._token(READ_ALL, kind="url", expires_at=self.expires,
                                 script=self.a)
        run = self._run(self.a, stdout="via url")
        path = f"/mcp/k/{raw}/"
        tools = {t["name"] for t in self.rpc(path=path).json()["result"]["tools"]}
        self.assertTrue({"scripts-list", "runs-get", "runs-output"} <= tools)
        result = self.client.post(
            path, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "runs-output", "arguments": {"run": str(run.id)}}},
            content_type="application/json", HTTP_ACCEPT=ACCEPT,
            HTTP_MCP_PROTOCOL_VERSION=LEGACY[0],
        ).json()["result"]
        self.assertEqual(result["structuredContent"]["text"], "via url")
        with self.assertRaises(ValidationError) as err:
            self._token(["runs:read", "brain:write"], kind="url", expires_at=self.expires)
        self.assertIn("URL tokens allow read permissions only.", str(err.exception))
        # A row changed behind the model's back is refused at authentication.
        APIToken.objects.filter(pk=token.pk).update(permissions=READ_ALL + ["brain:write"])
        self.assertEqual(self.rpc(path=path).status_code, 401)


class TokenFormTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.member = User.objects.create(email="t12-form@example.com")
        self.client.force_login(self.member)

    def post(self, **extra):
        data = {"name": "Bound", "scope": "api", "permissions": READ_ALL, "kind": "bearer",
                "brain_tier": "public", **extra}
        return self.client.post(reverse("cpanel:api_token_create"), data)

    def test_the_form_binds_one_live_script_of_this_workspace(self):
        page = self.client.get(reverse("cpanel:api_token_create"))
        choices = {str(c[0]) for c in page.context["form"].fields["script"].choices}
        self.assertIn(str(self.a.id), choices)
        self.assertNotIn(str(self.archived.id), choices)
        self.assertNotIn(str(self.foreign.id), choices)
        for label in ("Read scripts", "Read runs", "Read run output", "Bind to one script"):
            self.assertContains(page, label)
        response = self.post(script=str(self.a.id))
        self.assertEqual(response.status_code, 302, getattr(response, "context", None)
                         and response.context["form"].errors)
        token = APIToken.objects.get(name="Bound")
        self.assertEqual((token.script_id, sorted(token.permissions)),
                         (self.a.id, sorted(READ_ALL)))
        listing = self.client.get(reverse("cpanel:api_token_list"))
        self.assertContains(listing, "Script: <span class=\"text-text\">Alpha report</span>",
                            html=False)
        self.assertEqual(self.post(name="Foreign", script=str(self.foreign.id)).status_code, 200)
        self.assertFalse(APIToken.objects.filter(name="Foreign").exists())
        # A scope switch drops a stray script selection.
        self.post(name="Store", scope="datastores", script=str(self.a.id))
        self.assertIsNone(APIToken.objects.get(name="Store").script_id)

    def test_one_binding_at_most(self):
        brain = Brain.objects.create(name="B", slug="b-t12", workspace=self.ws)
        with self.assertRaises(ValidationError) as err:
            self._token(["brain:read", "runs:read"], brain=brain, script=self.a)
        self.assertIn("not both", str(err.exception))
        with self.assertRaises(ValidationError):
            self._token(["runs:read"], script=self.foreign)
        # The database refuses what the model would: a script on a non-API token
        # or next to a brain.
        for kwargs in ({"scope": "datastores", "permissions": []},
                       {"brain": brain}):
            with self.subTest(kwargs=kwargs), self.assertRaises(IntegrityError), \
                    transaction.atomic():
                APIToken.objects.filter(pk=self.full.pk).update(script=self.a, **kwargs)

    def test_connect_page_has_scripts_and_runs_snippets(self):
        page = self.client.get(reverse("cpanel:connect"))
        self.assertContains(page, "Scripts and runs")
        api_url = page.context["api_url"]
        self.assertContains(page, f"{api_url}/runs.output?run=RUN_ID&amp;stream=stderr")
        self.assertContains(page, f"{api_url}/scripts.list")
        self.assertContains(page, "runs-output")
        # The token preview lists the new endpoints for a token that holds them.
        preview = self.client.get(reverse("cpanel:connect"), {"token": str(self.bound.pk)})
        self.assertEqual({e.slug for e in preview.context["endpoints"]}, set(SLUGS))

    def test_settings_api_reference_names_scripts_and_runs(self):
        # Settings -> API & MCP lists the Unified API's method families; it
        # named only brain.* after T12 added scripts.* and runs.* (F2.5b-05).
        self.client.force_login(
            User.objects.create(email="t12-settings@example.com", is_superuser=True)
        )
        page = self.client.get(reverse("cpanel:settings"))
        self.assertContains(page, "/api/v1/brain.*, scripts.*, runs.*")


class RestorePreviewTests(_Fixture, TestCase):
    def test_script_bound_tokens_are_named_as_removed_by_a_restore(self):
        removed = BackupService.tokens_removed_by_restore()
        self.assertEqual(removed, [{"name": self.bound.name, "bound_to": "script Alpha report"}])
        # A real restore deletes every script, so the bound token goes with it.
        admin = User.objects.create(email="t12-restore@example.com", is_superuser=True)
        backup = BackupService.create_backup(include_runs=False)
        BackupService.restore_backup(json.loads(json.dumps(backup)), restore_runs=False,
                                     current_user=admin)
        self.assertFalse(APIToken.objects.filter(pk=self.bound.pk).exists())
        self.assertTrue(APIToken.objects.filter(pk=self.full.pk).exists())
