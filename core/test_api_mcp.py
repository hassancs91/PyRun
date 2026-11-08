"""MCP protocol, transport parity, URL credentials, docs and console boundaries."""

import json
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.api.errors import ApiError
from core.api.mcp import LEGACY, META, MODERN
from core.api.registry import endpoint, registry
from core.models import APIToken, GlobalSettings, User, Workspace
from core.test_plugin_api import _mock_setup

ACCEPT = "application/json, text/event-stream"


class MCPTests(TestCase):
    def setUp(self):
        _mock_setup(self)
        self.ws = Workspace.get_default()
        self.raw = APIToken.generate_token(scope="api")
        self.token = APIToken.objects.create(
            name="MCP reader",
            scope="api",
            token=self.raw,
            permissions=["brain:read"],
            workspace=self.ws,
        )
        saved = dict(registry._endpoints)
        self.addCleanup(lambda: setattr(registry, "_endpoints", saved))

        @endpoint(
            "fixture.echo",
            title="Echo",
            permission="brain:read",
            input={
                "type": "object",
                "properties": {"n": {"type": "integer"}},
                "required": ["n"],
            },
        )
        def echo(ctx, params):
            return {"n": params["n"], "workspace": str(ctx.workspace.pk)}

        @endpoint(
            "fixture.write",
            title="Write",
            permission="fixture:write",
            access="write",
            input={"type": "object"},
        )
        def write(ctx, params):
            raise AssertionError("Read-only caller must never reach a write")

        @endpoint("fixture.error", title="Error", permission="brain:read", input={"type": "object"})
        def error(ctx, params):
            raise ApiError("unknown", "Unknown note.", status=404)

    def rpc(
        self,
        method="tools/list",
        params=None,
        *,
        version=LEGACY[0],
        notification=False,
        path="/mcp",
        raw=None,
        **headers,
    ):
        params = dict(params or {})
        if version == MODERN:
            params.setdefault(
                "_meta", {META + "protocolVersion": MODERN, META + "clientCapabilities": {}}
            )
            headers.setdefault("HTTP_MCP_METHOD", method)
            if method == "tools/call":
                headers.setdefault("HTTP_MCP_NAME", params.get("name", ""))
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            body["id"] = "rpc-1"
        headers.setdefault("HTTP_MCP_PROTOCOL_VERSION", version)
        headers.setdefault("HTTP_AUTHORIZATION", "Bearer " + (self.raw if raw is None else raw))
        headers.setdefault("HTTP_ACCEPT", ACCEPT)
        return self.client.post(path, body, content_type="application/json", **headers)

    def test_initialize_negotiation_and_no_session(self):
        for requested, expected in [(v, v) for v in LEGACY] + [("1900-01-01", LEGACY[0])]:
            with self.subTest(version=requested):
                response = self.rpc(
                    "initialize",
                    {
                        "protocolVersion": requested,
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                )
                self.assertEqual(response.json()["result"]["protocolVersion"], expected)
                self.assertEqual(response["MCP-Protocol-Version"], expected)
                self.assertNotIn("Mcp-Session-Id", response)
        self.assertEqual(self.rpc("initialize").json()["error"]["code"], -32602)
        response = self.rpc("notifications/initialized", notification=True)
        self.assertEqual((response.status_code, response.content), (202, b""))
        self.assertEqual(self.rpc("ping").json()["result"], {})

    def test_current_discovery_inline_tools_and_required_metadata(self):
        response = self.rpc("server/discover", version=MODERN)
        result = response.json()["result"]
        self.assertIn(MODERN, result["supportedVersions"])
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(result["_meta"][META + "serverInfo"]["name"], "PyRun")
        # No initialize required; no state is shared between requests.
        self.assertEqual(self.rpc(version=MODERN).status_code, 200)
        self.assertEqual(self.rpc("ping", version=MODERN).json()["error"]["code"], -32601)
        for meta in ({}, {META + "protocolVersion": MODERN}):
            response = self.rpc(version=MODERN, params={"_meta": meta})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], -32602)

    def test_current_header_body_mismatches_rejected(self):
        for headers in (
            {"HTTP_MCP_METHOD": "ping"},
            {"HTTP_MCP_PROTOCOL_VERSION": LEGACY[0]},
            {"HTTP_MCP_NAME": "fixture-write"},
        ):
            with self.subTest(headers=headers):
                response = self.rpc(
                    "tools/call",
                    {"name": "fixture-echo", "arguments": {"n": 1}},
                    version=MODERN,
                    **headers,
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], -32020)
        response = self.rpc(
            "tools/call",
            {"name": "fixture-echo", "arguments": {"n": 1}},
            version=MODERN,
            HTTP_MCP_NAME="=?base64?Zml4dHVyZS1lY2hv?=",
        )
        self.assertEqual(response.status_code, 200)

    def test_modern_catalog_cache_metadata_is_private_and_immediately_expires(self):
        # Official client 2.3.0 rejects ListToolsResult without these required
        # fields; no shared catalog cache may cross token permission changes.
        for method in ("tools/list", "server/discover"):
            result = self.rpc(method, version=MODERN).json()["result"]
            self.assertEqual(result["cacheScope"], "private")
            self.assertEqual(result["ttlMs"], 0)

    def test_unsupported_version_and_unknown_method(self):
        with self.assertLogs("core.api.mcp", level="WARNING"):
            response = self.rpc(version="1900-01-01")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], -32022)
        response = self.rpc("not/a/method", version=MODERN)
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (404, -32601))

    def test_catalog_filtering_and_rest_call_parity(self):
        listed = self.rpc().json()["result"]["tools"]
        self.assertNotIn("fixture-write", [s["name"] for s in listed])
        spec = next(s for s in listed if s["name"] == "fixture-echo")
        self.assertEqual(spec["inputSchema"], registry.get("fixture.echo").input)
        rest = self.client.post(
            "/api/v1/fixture.echo",
            {"n": 7},
            content_type="application/json",
            HTTP_X_API_KEY=self.raw,
        ).json()["data"]
        for version in LEGACY + (MODERN,):
            response = self.rpc(
                "tools/call", {"name": "fixture-echo", "arguments": {"n": 7}}, version=version
            )
            result = response.json()["result"]
            self.assertEqual(result["structuredContent"], rest)
            self.assertEqual(json.loads(result["content"][0]["text"]), rest)
            self.assertFalse(result["isError"])
        self.token.refresh_from_db()
        self.assertIsNotNone(self.token.last_used_at)
        other = Workspace.objects.create(name="Other MCP workspace")
        raw = APIToken.generate_token(scope="api")
        APIToken.objects.create(
            name="Other MCP", scope="api", token=raw, permissions=["brain:read"], workspace=other
        )
        result = self.rpc(
            "tools/call", {"name": "fixture-echo", "arguments": {"n": 7}}, raw=raw
        ).json()["result"]
        self.assertEqual(result["structuredContent"]["workspace"], str(other.pk))

    def test_tool_errors_are_results_and_invisible_tools_unknown(self):
        result = self.rpc("tools/call", {"name": "fixture-error"}).json()["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["error"], {"code": "unknown", "message": "Unknown note."}
        )
        invalid = self.rpc("tools/call", {"name": "fixture-echo", "arguments": {"n": True}})
        self.assertTrue(invalid.json()["result"]["isError"])
        hidden = self.rpc("tools/call", {"name": "fixture-write"})
        missing = self.rpc("tools/call", {"name": "missing"})
        self.assertEqual(hidden.json(), missing.json())

    def test_protocol_errors_no_batches_no_null_ids_or_tool_notifications(self):
        bodies = [
            [],
            {},
            {"jsonrpc": "2.0", "id": None, "method": "ping"},
            {"jsonrpc": "2.0", "id": True, "method": "ping"},
            {"jsonrpc": "2.0", "id": 2, "result": {}},
            {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "fixture-write"}},
        ]
        for body in bodies:
            with self.subTest(body=body):
                response = self.client.post(
                    "/mcp",
                    body,
                    content_type="application/json",
                    HTTP_X_API_KEY=self.raw,
                    HTTP_ACCEPT=ACCEPT,
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], -32600)
        response = self.client.post(
            "/mcp",
            "{",
            content_type="application/json",
            HTTP_X_API_KEY=self.raw,
            HTTP_ACCEPT=ACCEPT,
        )
        self.assertEqual(response.json()["error"]["code"], -32700)

    def test_transport_origin_accept_and_methods(self):
        for method in ("get", "delete", "put"):
            self.assertEqual(getattr(self.client, method)("/mcp").status_code, 405)
        self.assertEqual(self.client.options("/mcp").status_code, 204)
        self.assertEqual(self.rpc(HTTP_ACCEPT="application/json").status_code, 406)
        self.assertEqual(self.rpc(HTTP_ORIGIN="https://attacker.test").status_code, 403)
        self.assertEqual(self.rpc(HTTP_ORIGIN="http://testserver").status_code, 200)
        with override_settings(MCP_ALLOWED_ORIGINS=["https://client.test"]):
            self.assertEqual(self.rpc(HTTP_ORIGIN="https://client.test").status_code, 200)

    @override_settings(API_AUTHFAIL_RATE_LIMIT=3)
    def test_constant_auth_failures_and_shared_per_ip_limit(self):
        bad = self.rpc(raw="bad")
        self.token.expires_at = timezone.now() - timedelta(seconds=1)
        self.token.save()
        self.assertEqual(self.rpc().json(), bad.json())
        self.token.is_active = False
        self.token.save()
        self.assertEqual(self.rpc().json(), bad.json())
        limited = self.rpc(raw="another")
        self.assertEqual(limited.status_code, 429)
        self.assertIn("Retry-After", limited)

    def test_url_toggle_expiry_kind_digest_and_read_only(self):
        raw = APIToken.generate_token(scope="api", kind="url")
        token = APIToken.objects.create(
            name="URL",
            scope="api",
            kind="url",
            token=raw,
            permissions=["brain:read"],
            workspace=self.ws,
            expires_at=timezone.now() + timedelta(days=1),
        )
        path = f"/mcp/k/{raw}/"
        self.assertEqual(self.rpc(path=path).status_code, 401)
        gs = GlobalSettings.get_settings()
        gs.mcp_url_tokens_enabled = True
        gs.save()
        self.assertEqual(self.rpc(path=path, raw="ignored").status_code, 200)
        self.assertEqual(self.rpc(raw=raw).status_code, 401)
        self.assertEqual(self.rpc(path=f"/mcp/k/{self.raw}/").status_code, 401)
        self.assertEqual(self.rpc(path=f"/mcp/k/{token.token}/").status_code, 401)
        APIToken.objects.filter(pk=token.pk).update(permissions=["brain:write"])
        self.assertEqual(self.rpc(path=path).status_code, 401)
        APIToken.objects.filter(pk=token.pk).update(
            permissions=["brain:read"], expires_at=timezone.now()
        )
        self.assertEqual(self.rpc(path=path).status_code, 401)

    def test_url_token_works_without_trailing_slash(self):
        """F125: claude.ai POSTs to /mcp/k/<token> (no slash) after initialize; it got a
        301 (APPEND_SLASH), followed it as a GET and failed on the 405."""
        raw = APIToken.generate_token(scope="api", kind="url")
        APIToken.objects.create(
            name="URL", scope="api", kind="url", token=raw, permissions=["brain:read"],
            workspace=self.ws, expires_at=timezone.now() + timedelta(days=1),
        )
        gs = GlobalSettings.get_settings()
        gs.mcp_url_tokens_enabled = True
        gs.save()
        for path in (f"/mcp/k/{raw}/", f"/mcp/k/{raw}"):
            response = self.rpc("initialize", {"protocolVersion": LEGACY[0], "capabilities": {},
                                "clientInfo": {"name": "c", "version": "1"}}, path=path, raw="")
            self.assertEqual(response.status_code, 200, path)
            self.assertEqual(self.rpc(path=path, raw="").status_code, 200, path)
            self.assertEqual(self.rpc("notifications/initialized", notification=True, path=path, raw="").status_code, 202)
            get = self.client.get(path, HTTP_ACCEPT="text/event-stream")
            self.assertEqual((get.status_code, get["Allow"]), (405, "POST, OPTIONS"))
        self.assertEqual(self.rpc(path="/mcp/k/not-a-token").status_code, 401)

    def test_shared_endpoint_limit_and_budget(self):
        original = registry.get("fixture.echo")
        from dataclasses import replace

        registry._endpoints[original.slug] = replace(original, rate=(1, 60))
        self.client.post(
            "/api/v1/fixture.echo",
            {"n": 7},
            content_type="application/json",
            HTTP_X_API_KEY=self.raw,
        )
        response = self.rpc("tools/call", {"name": "fixture-echo", "arguments": {"n": 7}})
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response)
        with override_settings(MCP_CALL_BUDGET_SECONDS=-1):
            result = self.rpc("tools/call", {"name": "fixture-error"}).json()["result"]
        self.assertEqual(result["structuredContent"]["error"]["code"], "timeout")

    def test_openapi_is_direct_document_filtered_and_schema_matches(self):
        response = self.client.get("/api/v1/_openapi.json", HTTP_X_API_KEY=self.raw)
        doc = response.json()
        self.assertEqual(doc["openapi"], "3.1.0")
        self.assertNotIn("/fixture.write", doc["paths"])
        schema = doc["paths"]["/fixture.echo"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]
        self.assertEqual(schema, registry.get("fixture.echo").input)
        self.assertIn("get", doc["paths"]["/fixture.echo"])
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_connect_workspace_filter_no_secrets_and_operator_toggle(self):
        owner = User.objects.create_superuser(
            username="connect", email="connect@test.local", password="test"
        )
        self.client.force_login(owner)
        other = Workspace.objects.create(name="Other")
        raw = APIToken.generate_token(scope="api")
        outsider = APIToken.objects.create(
            name="Outside", scope="api", token=raw, workspace=other, permissions=["brain:read"]
        )
        response = self.client.get("/cpanel/connect/", {"token": self.token.pk})
        self.assertContains(response, "fixture.echo")
        for secret in (self.raw, self.token.token, outsider.name, raw, outsider.token):
            self.assertNotContains(response, secret)
        self.assertEqual(
            self.client.get("/cpanel/connect/", {"token": outsider.pk}).status_code, 404
        )
        self.assertEqual(self.client.get("/cpanel/connect/", {"token": "bad"}).status_code, 404)
        self.client.post("/cpanel/connect/url-tokens/", {"enabled": "on"})
        self.assertTrue(GlobalSettings.get_settings().mcp_url_tokens_enabled)
        owner.is_superuser = False
        owner.save()
        self.assertEqual(self.client.post("/cpanel/connect/url-tokens/", {}).status_code, 403)
        self.assertTrue(GlobalSettings.get_settings().mcp_url_tokens_enabled)

    def test_unexpected_handler_error_is_generic(self):
        with mock.patch("core.api.registry.validate", side_effect=RuntimeError("SECRET")):
            with self.assertLogs("core.api.registry", level="ERROR"):
                response = self.rpc("tools/call", {"name": "fixture-echo", "arguments": {"n": 1}})
        self.assertNotIn("SECRET", response.content.decode())
        self.assertEqual(
            response.json()["result"]["structuredContent"]["error"]["code"], "internal_error"
        )

    def test_url_token_reveal_gives_url_instructions(self):
        owner = User.objects.create_superuser(
            username="url-owner", email="url@test.local", password="test"
        )
        self.client.force_login(owner)
        raw = APIToken.generate_token(scope="api", kind="url")
        token = APIToken.objects.create(
            name="URL",
            scope="api",
            kind="url",
            token=raw,
            permissions=["brain:read"],
            workspace=self.ws,
            expires_at=timezone.now() + timedelta(days=1),
        )
        session = self.client.session
        session["new_api_token"] = raw
        session["new_api_token_id"] = str(token.pk)
        session.save()
        response = self.client.get(reverse("cpanel:api_token_created", args=[token.pk]))
        self.assertContains(response, "/mcp/k/YOUR_URL_TOKEN/")
        self.assertNotContains(response, "Authorization: Bearer YOUR_TOKEN")
