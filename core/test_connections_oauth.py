"""
OAuth 2.0 connections with refresh and paste-a-token (2.5 Stage 5, ticket T24).

What this pins, roughly in order of how much it would hurt to get wrong:

- A script only ever gets the access token: the loopback reply, the run's
  environment and its output never hold the refresh token or the app's client
  secret, and a token refreshed during the run is masked like the first one.
- The connect flow: a signed state (workspace, user, provider, connection,
  nonce; 10 minutes) whose private half (the PKCE verifier) stays in the
  browser's session; a tampered, expired, replayed or foreign reply is refused
  and saves nothing; a good one exchanges the code, calls whoami and saves ok.
- Paste a token: a refresh token is refreshed with the app and checked with
  whoami before anything is saved; a lone access token is checked too.
- Lazy refresh, 60 seconds early and single-flight: Postgres locks the row (two
  threads, one refresh), SQLite claims it with a conditional UPDATE and waits
  for whoever has it. A refused refresh marks the connection expired and emails
  its creator once; an unreachable provider leaves it alone (503).
- OAuth apps: the workspace's own app wins over the instance default, which
  only superusers set; backups carry both and the connections' links to them.

No test reaches a real provider: an in-process server plays the token endpoint
and the whoami, and the presets are the real ones with its URLs swapped in.
"""

import base64
import dataclasses
import hashlib
import html
import http.server
import json
import re
import sys
import threading
import time
import urllib.parse
import uuid
from datetime import timedelta
from unittest import mock, skipUnless

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.db import connection as db_connection
from django.test import LiveServerTestCase, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.executor import execute_run
from core.integrations import presets as presets_module
from core.integrations.presets import OAUTH2, Whoami, auth_material, get_preset, mask_values
from core.models import Connection, ConnectionGrant, OAuthClient, Run, RunMask, Workspace, WorkspaceMembership
from core.services import connections as svc
from core.services import oauth
from core.services.backup_service import BackupService
from core.services.datastore_token import mint_datastore_token
from core.test_connections import _ai_off, _Fixture, _setup_wizard_off

User = get_user_model()

CLIENT_ID = "client-t24.apps.googleusercontent.com"
CLIENT_SECRET = "t24-client-secret-0001"
REFRESH = "rt-t24-pasted-refresh-0002"
SHEETS = "https://www.googleapis.com/auth/spreadsheets"
REAL_PRESETS = dict(presets_module.presets())
OAUTH_KEYS = ("google", "microsoft", "github", "slack", "notion")


def handoff_url(resp) -> str:
    """The provider URL a Connect / Reconnect POST sends the browser to.

    Run 7 B2 fix 2: the POST answers 200 with a page of its own, not a redirect
    (browsers check form-action 'self' on every redirect after a form POST, the
    provider's own hops included). Its meta refresh and its fallback link both
    hold the exact authorize URL the oauth service built, and it's never cached.
    """
    assert resp.status_code == 200, resp.status_code
    assert not resp.has_header("Location")
    page = resp.content.decode()
    refresh = re.search(r'<meta http-equiv="refresh" content="0;url=([^"<>]*)">', page)
    link = re.search(r'<a href="([^"<>]*)" data-oauth-handoff', page)
    assert refresh and link, page
    url = html.unescape(refresh.group(1))
    assert html.unescape(link.group(1)) == url == resp.context["authorize_url"]
    assert "no-store" in resp["Cache-Control"], resp["Cache-Control"]
    return url


# --------------------------------------------------------------------------- #
# An in-process provider: a token endpoint and a whoami
# --------------------------------------------------------------------------- #


class _ProviderState:
    def __init__(self):
        self.lock = threading.Lock()
        self.codes = {}
        self.access = set()
        self.refresh = {REFRESH}
        self.n = 0
        self.refresh_calls = 0
        self.token_requests = []
        self.fail = ""  # "" | refuse | down
        self.rotate = False
        self.delay = 0.0
        self.expires_in = 3600

    def issue(self, with_refresh):
        with self.lock:
            self.n += 1
            out = {
                "access_token": f"at-t24-{self.n:04d}-access",
                "token_type": "Bearer",
                "expires_in": self.expires_in,
                "scope": f"openid email {SHEETS}",
            }
            self.access.add(out["access_token"])
            if with_refresh:
                out["refresh_token"] = f"rt-t24-{self.n:04d}-issued"
                self.refresh.add(out["refresh_token"])
            return out

    def consent(self, authorize_url):
        """What the consent screen does: a code for this request, and the callback's query."""
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(authorize_url).query))
        code = f"code-{uuid.uuid4().hex[:8]}"
        self.codes[code] = {"redirect_uri": query["redirect_uri"], "challenge": query.get("code_challenge")}
        return {"code": code, "state": query["state"]}, query


class _Provider(http.server.BaseHTTPRequestHandler):
    def _reply(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802 - http.server API
        st = self.server.st
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if "json" in self.headers.get("Content-Type", ""):
            form = json.loads(raw)
        else:
            form = dict(urllib.parse.parse_qsl(raw.decode()))
        st.token_requests.append((dict(self.headers), form))
        if st.delay:
            time.sleep(st.delay)
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            client_id, _sep, secret = base64.b64decode(auth[6:]).decode().partition(":")
        else:
            client_id, secret = form.get("client_id"), form.get("client_secret")
        if (client_id, secret) != (CLIENT_ID, CLIENT_SECRET):
            return self._reply(401, {"error": "invalid_client"})
        if st.fail == "down":
            return self._reply(503, {"error": "temporarily_unavailable"})
        if st.fail == "page":  # a proxy's or a maintenance page, sent with HTTP 200
            body = b"<html><body>Down for maintenance</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return None
        if form.get("grant_type") == "authorization_code":
            entry = st.codes.pop(form.get("code"), None)
            if entry is None or entry["redirect_uri"] != form.get("redirect_uri"):
                return self._reply(400, {"error": "invalid_grant"})
            if entry["challenge"]:
                digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
                if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != entry["challenge"]:
                    return self._reply(400, {"error": "invalid_grant", "error_description": "PKCE"})
            return self._reply(200, st.issue(with_refresh=True))
        if form.get("grant_type") == "refresh_token":
            if st.fail == "echo":  # a provider that quotes what it got back
                return self._reply(400, {"error": "invalid_grant", "error_description":
                                         f"{form.get('refresh_token')} / {secret}"})
            if st.fail == "refuse" or form.get("refresh_token") not in st.refresh:
                return self._reply(400, {"error": "invalid_grant", "error_description": "Token has been revoked."})
            with st.lock:
                st.refresh_calls += 1
            return self._reply(200, st.issue(with_refresh=st.rotate))
        return self._reply(400, {"error": "unsupported_grant_type"})

    def do_GET(self):  # noqa: N802 - http.server API
        st = self.server.st
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if self.path.startswith("/whoami") and token in st.access:
            return self._reply(200, {"email": "robot@example.com", "sub": "1"})
        return self._reply(401, {"error": "invalid_token"})

    def log_message(self, *args):
        pass


class _ProviderMixin:
    """A running provider and the real presets pointed at it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
        threading.Thread(target=cls.provider.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.provider.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.provider.shutdown()
        cls.provider.server_close()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.st = self.provider.st = _ProviderState()
        fakes = {key: self._fake(REAL_PRESETS[key]) for key in ("google", "notion")}
        patcher = mock.patch.object(presets_module, "presets", return_value={**REAL_PRESETS, **fakes})
        patcher.start()
        self.addCleanup(patcher.stop)
        # The provider is on 127.0.0.1, which the SSRF guard refuses otherwise.
        private = override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=True)
        private.enable()
        self.addCleanup(private.disable)

    def _fake(self, preset):
        return dataclasses.replace(
            preset,
            base_url=self.base,
            whoami=Whoami("/whoami", label_keys=("email",)),
            oauth=dataclasses.replace(
                preset.oauth, authorize_url=self.base + "/authorize", token_url=self.base + "/token"
            ),
        )

    def _app(self, workspace="own", provider="google"):
        app = OAuthClient(
            workspace=self.ws if workspace == "own" else workspace, provider=provider, client_id=CLIENT_ID
        )
        app.set_secret(CLIENT_SECRET)
        app.save()
        return app

    def _oauth(self, name="google", refresh_token=REFRESH, access_token="at-t24-old-0000", expires_in=3600,
               app=None, provider="google", **kwargs):
        credentials = {"access_token": access_token}
        if refresh_token:
            credentials["refresh_token"] = refresh_token
        self.st.access.add(access_token)
        c = self._connection(
            name=name, provider=provider, auth_kind=OAUTH2, credentials=credentials,
            config={"scope_sets": ["sheets"], "extra_scopes": ""}, oauth_client=app,
            token_expires_at=timezone.now() + timedelta(seconds=expires_in) if expires_in is not None else None,
            **kwargs,
        )
        return c


# --------------------------------------------------------------------------- #
# Presets
# --------------------------------------------------------------------------- #


class OAuthPresetTests(SimpleTestCase):
    def test_every_oauth_preset_is_complete(self):
        for key in OAUTH_KEYS:
            preset = get_preset(key)
            with self.subTest(preset=key):
                self.assertEqual(preset.auth_kinds, (OAUTH2,))
                self.assertTrue(preset.oauth.authorize_url.startswith("https://"))
                self.assertTrue(preset.oauth.token_url.startswith("https://"))
                self.assertTrue(preset.base_url.startswith("https://"))
                self.assertIsNotNone(preset.whoami)
                secret = {f.name for f in preset.secret_fields(OAUTH2)}
                self.assertEqual(secret, {"refresh_token", "access_token"})
                keys = [s.key for s in preset.oauth.scope_sets]
                self.assertEqual(len(keys), len(set(keys)))
                self.assertLessEqual(set(preset.oauth.default_scope_sets), set(keys))
        self.assertTrue(get_preset("google").oauth.pkce)
        self.assertIn(("access_type", "offline"), get_preset("google").oauth.authorize_params)

    def test_scopes_and_the_microsoft_tenant(self):
        google = get_preset("google").oauth
        self.assertEqual(
            google.scopes_for(["sheets"], "email https://x.test/extra"),
            ["openid", "email", SHEETS, "https://x.test/extra"],
        )
        ms = get_preset("microsoft").oauth
        self.assertIn("/common/oauth2/v2.0/authorize", ms.urls({})[0])
        self.assertIn("/contoso.example/oauth2/v2.0/token", ms.urls({"tenant": "contoso.example"})[1])

    def test_auth_material_carries_the_access_token_only(self):
        creds = {"access_token": "at-1234", "refresh_token": REFRESH}
        material = auth_material(get_preset("google"), OAUTH2, {}, creds)
        self.assertEqual(material["headers"], {"Authorization": "Bearer at-1234"})
        self.assertEqual(material["token"], "at-1234")
        self.assertNotIn(REFRESH, json.dumps(material))
        masks = mask_values("g", get_preset("google"), OAUTH2, {}, creds)
        self.assertIn("at-1234", masks.values())
        self.assertIn(REFRESH, masks.values())  # masked too, should it ever show

    def test_a_fixed_header_is_sent_but_never_masked(self):
        notion = get_preset("notion")
        material = auth_material(notion, OAUTH2, {}, {"access_token": "ntn-secret-1"})
        self.assertEqual(material["headers"]["Notion-Version"], "2022-06-28")
        masks = mask_values("n", notion, OAUTH2, {}, {"access_token": "ntn-secret-1"})
        self.assertNotIn("2022-06-28", masks.values())
        self.assertIn("Bearer ntn-secret-1", masks.values())


class DocsTests(SimpleTestCase):
    def test_help_search_finds_the_google_consent_screen_walkthrough(self):
        from django.conf import settings

        from core.services import help_docs

        index = help_docs.build_index(settings.BASE_DIR)
        for question in ("google consent screen testing refresh token 7 days", "slack app redirect url bot token"):
            with self.subTest(question=question):
                best = help_docs.search(question, index=index)["results"][0]
                self.assertEqual(best["doc"], "docs/integrations/oauth-apps.md")


class StateTests(SimpleTestCase):
    def test_a_changed_or_old_state_is_refused(self):
        value = oauth.sign_state({"w": "1", "u": "2", "p": "google", "c": "3", "n": "x"})
        self.assertEqual(oauth.read_state(value)["p"], "google")
        with self.assertRaisesMessage(oauth.OAuthError, "was changed"):
            oauth.read_state(value[:-2] + ("AA" if not value.endswith("AA") else "BB"))
        with self.assertRaisesMessage(oauth.OAuthError, "was changed"):
            oauth.read_state(signing.dumps({"p": "google"}, salt="another.salt"))
        later = time.time() + oauth.STATE_MAX_AGE + 1
        with mock.patch("django.core.signing.time.time", return_value=later):
            with self.assertRaisesMessage(oauth.OAuthError, "longer than 10 minutes"):
                oauth.read_state(value)


# --------------------------------------------------------------------------- #
# Connect
# --------------------------------------------------------------------------- #


class ConnectFlowTests(_ProviderMixin, _Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.app = self._app()

    def _connect(self, **extra):
        data = {"provider": "google", "auth_kind": OAUTH2, "name": "sheets", "scope_sets": ["sheets"],
                "extra_scopes": "", "action": "connect", **extra}
        return self.client.post(reverse("cpanel:connection_create"), data)

    def test_connect_redirects_to_the_provider_with_pkce_and_a_signed_state(self):
        url = handoff_url(self._connect())
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.assertTrue(url.startswith(self.base + "/authorize?"))
        self.assertEqual(query["client_id"], CLIENT_ID)
        self.assertEqual(query["redirect_uri"], "http://testserver/oauth/callback/")
        self.assertEqual(query["response_type"], "code")
        self.assertEqual(query["scope"].split(" "), ["openid", "email", SHEETS])
        self.assertEqual((query["code_challenge_method"], query["access_type"], query["prompt"]),
                         ("S256", "offline", "consent"))
        state = oauth.read_state(query["state"])
        self.assertEqual((state["w"], state["u"], state["p"]), (str(self.ws.pk), str(self.user.pk), "google"))
        self.assertNotIn(CLIENT_SECRET, url)
        flow = self.client.session[oauth.SESSION_KEY][state["n"]]
        self.assertNotIn(flow["verifier"], url)  # the verifier never leaves the server
        self.assertFalse(Connection.objects.exists())  # nothing saved before the provider answers

    def test_the_whole_flow_saves_an_ok_connection(self):
        callback, _query = self.st.consent(handoff_url(self._connect(grant_all_scripts="on")))
        resp = self.client.get(reverse("oauth_callback"), callback)
        c = Connection.objects.get(name="sheets")
        self.assertRedirects(resp, reverse("cpanel:connection_edit", kwargs={"pk": c.pk}))
        self.assertEqual((c.status, c.account_label, c.auth_kind, c.oauth_client), ("ok", "robot@example.com", OAUTH2, self.app))
        self.assertEqual((c.workspace, c.created_by, c.grant_all_scripts), (self.ws, self.user, True))
        creds = c.get_credentials()
        self.assertEqual(set(creds), {"access_token", "refresh_token"})
        self.assertIn(creds["access_token"], self.st.access)
        self.assertAlmostEqual((c.token_expires_at - timezone.now()).total_seconds(), 3600, delta=30)
        self.assertEqual(c.scopes, ["openid", "email", SHEETS])
        self.assertIsNotNone(c.last_tested_at)
        _headers, form = self.st.token_requests[-1]
        self.assertEqual((form["grant_type"], form["client_secret"]), ("authorization_code", CLIENT_SECRET))
        self.assertTrue(form["code_verifier"])  # and the provider checked it against the challenge

    def test_a_replayed_reply_is_refused(self):
        callback, _query = self.st.consent(handoff_url(self._connect()))
        self.client.get(reverse("oauth_callback"), callback)
        resp = self.client.get(reverse("oauth_callback"), callback, follow=True)
        self.assertContains(resp, "already used")
        self.assertEqual(Connection.objects.count(), 1)

    def test_a_tampered_or_expired_state_saves_nothing(self):
        callback, _query = self.st.consent(handoff_url(self._connect()))
        forged = {**callback, "state": callback["state"][:-3] + "abc"}
        self.assertContains(self.client.get(reverse("oauth_callback"), forged, follow=True), "was changed")
        later = time.time() + oauth.STATE_MAX_AGE + 5
        with mock.patch("django.core.signing.time.time", return_value=later):
            resp = self.client.get(reverse("oauth_callback"), callback, follow=True)
        self.assertContains(resp, "longer than 10 minutes")
        self.assertFalse(Connection.objects.exists())
        self.assertEqual(self.st.token_requests, [])  # the code was never exchanged

    def test_a_reply_in_another_browser_or_for_another_user_is_refused(self):
        callback, _query = self.st.consent(handoff_url(self._connect()))
        other = self.client_class()
        other.force_login(self.user)  # same user, another session: no flow
        self.assertContains(other.get(reverse("oauth_callback"), callback, follow=True), "this browser session")
        intruder = User.objects.create_user(username="t24x", email="x@t24.test", password="pw")
        WorkspaceMembership.objects.update_or_create(user=intruder, workspace=self.ws,
                                                     defaults={"role": WorkspaceMembership.ROLE_MEMBER})
        session = self.client.session
        flows = dict(session[oauth.SESSION_KEY])
        self.client.force_login(intruder)  # a new session for the intruder ...
        session = self.client.session
        session[oauth.SESSION_KEY] = flows  # ... even holding the flow
        session.save()
        resp = self.client.get(reverse("oauth_callback"), callback, follow=True)
        self.assertContains(resp, "another user")
        self.assertFalse(Connection.objects.exists())

    def test_the_provider_saying_no_saves_nothing_and_shows_no_forged_text(self):
        _callback, query = self.st.consent(handoff_url(self._connect()))
        resp = self.client.get(reverse("oauth_callback"), {
            "error": "access_denied", "error_description": "Call +1-555 to restore your account", "state": query["state"],
        }, follow=True)
        self.assertContains(resp, "access_denied")
        self.assertNotContains(resp, "555")
        self.assertFalse(Connection.objects.exists())

    def test_reconnect_updates_the_same_connection(self):
        c = self._oauth(app=self.app, status=Connection.Status.EXPIRED, last_error="old")
        resp = self.client.post(reverse("cpanel:connection_edit", kwargs={"pk": c.pk}), {
            "name": "google", "scope_sets": ["sheets", "calendar"], "extra_scopes": "", "action": "connect",
        })
        callback, query = self.st.consent(handoff_url(resp))
        self.assertIn("auth/calendar", query["scope"])
        self.client.get(reverse("oauth_callback"), callback)
        c.refresh_from_db()
        self.assertEqual((c.status, c.last_error, Connection.objects.count()), ("ok", "", 1))
        self.assertTrue(c.get_credentials()["refresh_token"].startswith("rt-t24-"))
        self.assertEqual(c.config["scope_sets"], ["sheets", "calendar"])

    def test_connect_needs_an_app(self):
        self.app.delete()
        resp = self._connect()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Add a Google OAuth app first")
        self.assertContains(resp, reverse("cpanel:oauth_app_create"))


class ConnectHandoffTests(_Fixture, TestCase):
    """Run 7 B2 fix 2: Connect and Reconnect hand off to the provider from a page of their own.

    Notion's authorize URL redirects from api.notion.com to app.notion.com, and a
    browser checks form-action on every redirect after a form POST, so a 302 from
    the POST never reached it (fix 1 allowed only the first host). With the real
    presets (no provider is called): the POST answers 200 with a page whose meta
    refresh and link lead to the authorize URL, and every page, this one and the
    Connect pages included, keeps exactly the console's form-action 'self'.
    """

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def _app(self, provider):
        app = OAuthClient(workspace=self.ws, provider=provider, client_id=CLIENT_ID)
        app.set_secret(CLIENT_SECRET)
        app.save()
        return app

    def _connect(self, key, **extra):
        preset = get_preset(key)
        data = {"provider": key, "auth_kind": OAUTH2, "name": f"{key}-c", "extra_scopes": "",
                "scope_sets": list(preset.oauth.default_scope_sets), "action": "connect", **extra}
        return self.client.post(reverse("cpanel:connection_create"), data)

    def test_each_providers_connect_hands_off_to_its_authorize_url(self):
        for key in OAUTH_KEYS:
            preset = get_preset(key)
            self._app(key)
            with self.subTest(provider=key):
                page = self.client.get(reverse("cpanel:connection_create") + "?provider=" + key)
                self.assertContains(page, f"Connect with {preset.label}")
                self.assertEqual(page["Content-Security-Policy"], settings.CONTENT_SECURITY_POLICY)
                resp = self._connect(key)
                url = handoff_url(resp)
                self.assertTrue(url.startswith(preset.oauth.urls({})[0] + "?"), url)
                self.assertContains(resp, f"Opening {preset.label}")
                self.assertContains(resp, f"Continue to {preset.label}")
                self.assertEqual(resp["Content-Security-Policy"], settings.CONTENT_SECURITY_POLICY)
                # The flow waits in the session as before; nothing is saved yet.
                query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
                state = oauth.read_state(query["state"])
                self.assertEqual(state["p"], key)
                self.assertIn(state["n"], self.client.session[oauth.SESSION_KEY])
                self.assertNotIn(CLIENT_SECRET, url)
        self.assertFalse(Connection.objects.exists())

    def test_reconnect_on_the_edit_page_hands_off_too(self):
        self._app("microsoft")
        c = self._connection(name="ms", provider="microsoft", auth_kind=OAUTH2,
                             credentials={"access_token": "at-ms"}, config={"scope_sets": ["mail"]})
        edit = reverse("cpanel:connection_edit", kwargs={"pk": c.pk})
        page = self.client.get(edit)
        self.assertContains(page, "Reconnect with Microsoft 365")
        self.assertEqual(page["Content-Security-Policy"], settings.CONTENT_SECURITY_POLICY)
        resp = self.client.post(edit, {"name": "ms", "scope_sets": ["mail"], "extra_scopes": "", "action": "connect"})
        url = handoff_url(resp)
        self.assertTrue(url.startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize?"), url)
        self.assertEqual(resp["Content-Security-Policy"], settings.CONTENT_SECURITY_POLICY)

    def test_a_form_with_errors_is_shown_again_without_a_handoff(self):
        self._app("github")
        resp = self._connect("github", name="")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "data-oauth-handoff")
        self.assertNotContains(resp, 'http-equiv="refresh"')
        self.assertContains(resp, "Connect with GitHub")
        self.assertContains(resp, "This field is required")
        self.assertFalse(self.client.session.get(oauth.SESSION_KEY))

    def test_the_url_is_escaped_in_the_page(self):
        # The authorize URL is an attribute value: a quote or a tag in it can't end the attribute.
        self._app("github")
        nasty = 'https://p.example/auth?a=1&b="><script>alert(1)</script>&c=\'x\'&d=<i>'
        with mock.patch("core.views.connections.oauth.start", return_value=nasty):
            resp = self._connect("github")
        self.assertEqual(handoff_url(resp), nasty)
        page = resp.content.decode()
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<i>", page)
        self.assertIn("a=1&amp;b=&quot;&gt;&lt;script&gt;", page)

    def test_every_page_keeps_form_action_self(self):
        for key in OAUTH_KEYS:
            self._app(key)
        api_key = self._connection()
        oauth_row = self._connection(name="g", provider="google", auth_kind=OAUTH2, credentials={"access_token": "at"})
        urls = [reverse("cpanel:connection_create") + "?provider=" + key for key in (*OAUTH_KEYS, "airtable")]
        urls += [reverse("cpanel:connection_list"), reverse("cpanel:connection_create"),
                 reverse("cpanel:connection_edit", kwargs={"pk": api_key.pk}),
                 reverse("cpanel:connection_edit", kwargs={"pk": oauth_row.pk}),
                 reverse("cpanel:oauth_app_list"), reverse("cpanel:dashboard")]
        for url in urls:
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp["Content-Security-Policy"], settings.CONTENT_SECURITY_POLICY)
        self.assertIn("form-action 'self'", settings.CONTENT_SECURITY_POLICY)
        self.assertNotIn("https:", settings.CONTENT_SECURITY_POLICY)


# --------------------------------------------------------------------------- #
# Paste a token
# --------------------------------------------------------------------------- #


class PasteTests(_ProviderMixin, _Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def _paste(self, **fields):
        data = {"provider": "google", "auth_kind": OAUTH2, "name": "sheets", "scope_sets": ["sheets"],
                "extra_scopes": "", "action": "save", **fields}
        return self.client.post(reverse("cpanel:connection_create"), data)

    def test_a_pasted_refresh_token_is_refreshed_and_checked_before_saving(self):
        app = self._app()
        resp = self._paste(refresh_token=REFRESH)
        c = Connection.objects.get(name="sheets")
        self.assertRedirects(resp, reverse("cpanel:connection_edit", kwargs={"pk": c.pk}))
        self.assertEqual(self.st.refresh_calls, 1)
        creds = c.get_credentials()
        self.assertEqual(creds["refresh_token"], REFRESH)  # Google sends no new one: kept
        self.assertIn(creds["access_token"], self.st.access)
        self.assertEqual((c.status, c.account_label, c.oauth_client), ("ok", "robot@example.com", app))
        self.assertIsNotNone(c.token_expires_at)

    def test_a_bad_refresh_token_saves_nothing(self):
        self._app()
        resp = self._paste(refresh_token="rt-not-known")
        self.assertContains(resp, "didn&#x27;t accept this token")
        self.assertContains(resp, "invalid_grant")
        self.assertNotContains(resp, "rt-not-known")
        self.assertFalse(Connection.objects.exists())

    def test_a_refusal_quoting_the_app_secret_is_masked_on_the_form(self):
        self._app()
        self.st.fail = "echo"
        resp = self._paste(refresh_token=REFRESH)
        self.assertContains(resp, "invalid_grant")
        self.assertNotContains(resp, CLIENT_SECRET)
        self.assertNotContains(resp, REFRESH)

    def test_a_refresh_token_needs_an_app_but_an_access_token_does_not(self):
        self.assertContains(self._paste(refresh_token=REFRESH), "Add a Google OAuth app first")
        self.st.access.add("at-long-lived-0007")
        self._paste(access_token="at-long-lived-0007")
        c = Connection.objects.get(name="sheets")
        self.assertEqual(c.get_credentials(), {"access_token": "at-long-lived-0007"})
        self.assertEqual((c.token_expires_at, c.oauth_client, c.status), (None, None, "ok"))

    def test_nothing_pasted_is_an_error_on_create_and_keeps_the_tokens_on_edit(self):
        self.assertContains(self._paste(), "paste a refresh token or an access token")
        c = self._oauth()
        before = c.credentials_encrypted
        self.client.post(reverse("cpanel:connection_edit", kwargs={"pk": c.pk}), {
            "name": "renamed", "scope_sets": ["sheets"], "extra_scopes": "", "action": "save",
        })
        c.refresh_from_db()
        self.assertEqual((c.name, c.credentials_encrypted), ("renamed", before))


# --------------------------------------------------------------------------- #
# Refresh
# --------------------------------------------------------------------------- #


class RefreshTests(_ProviderMixin, _Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.app = self._app()
        self.script = self._script()
        self.run = self._run(self.script)

    def _issue(self, c):
        return svc.issue(self.run, Connection.objects.get(pk=c.pk))

    def test_refresh_starts_sixty_seconds_before_expiry(self):
        c = self._oauth(app=self.app)
        expiry = c.token_expires_at
        with mock.patch("core.services.oauth._now", return_value=expiry - timedelta(seconds=61)):
            self.assertEqual(self._issue(c)["token"], "at-t24-old-0000")
        self.assertEqual(self.st.refresh_calls, 0)
        with mock.patch("core.services.oauth._now", return_value=expiry - timedelta(seconds=59)):
            material = self._issue(c)
        self.assertEqual(self.st.refresh_calls, 1)
        self.assertNotEqual(material["token"], "at-t24-old-0000")
        self.assertIn(material["token"], svc.issued_masks(self.run.pk).values())
        c.refresh_from_db()
        self.assertEqual(c.get_credentials(), {"access_token": material["token"], "refresh_token": REFRESH})
        self.assertGreater(material["expires_in"], 3000)

    def test_a_rotated_refresh_token_replaces_the_old_one(self):
        self.st.rotate = True
        c = self._oauth(app=self.app, expires_in=10)
        self._issue(c)
        c.refresh_from_db()
        self.assertTrue(c.get_credentials()["refresh_token"].startswith("rt-t24-0001"))

    def test_a_refused_refresh_marks_expired_and_emails_the_creator_once(self):
        self.st.fail = "refuse"
        c = self._oauth(app=self.app, expires_in=10, account_label="robot@example.com")
        with mock.patch("core.services.notification_service.NotificationService.send_email") as send:
            for _ in range(2):
                with self.assertRaises(svc.ConnectionLookupError) as ctx:
                    self._issue(c)
                self.assertEqual((ctx.exception.code, ctx.exception.status), ("CONNECTION_EXPIRED", 409))
        c.refresh_from_db()
        self.assertEqual(c.status, "expired")
        self.assertIn("invalid_grant", c.last_error)
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["to"], self.user.email)
        self.assertIn('"google"', send.call_args.args[1])
        self.assertNotIn(REFRESH, send.call_args.args[1])

    def test_an_unreachable_provider_leaves_the_connection_alone(self):
        self.st.fail = "down"
        c = self._oauth(app=self.app, expires_in=10)
        with mock.patch("core.services.notification_service.NotificationService.send_email") as send:
            with self.assertRaises(svc.ConnectionLookupError) as ctx:
                self._issue(c)
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("CONNECTION_REFRESH_FAILED", 503))
        c.refresh_from_db()
        self.assertEqual(c.status, "ok")
        send.assert_not_called()

    def test_what_the_provider_quotes_back_is_masked(self):
        self.st.fail = "echo"
        c = self._oauth(app=self.app, expires_in=10)
        with mock.patch("core.services.notification_service.NotificationService.send_email") as send:
            with self.assertRaises(svc.ConnectionLookupError) as ctx:
                self._issue(c)
        c.refresh_from_db()
        for text in (c.last_error, ctx.exception.message, send.call_args.args[1]):
            self.assertNotIn(REFRESH, text)
            self.assertNotIn(CLIENT_SECRET, text)
        self.assertIn("[oauth_app.client_secret:MASKED]", c.last_error)

    def test_a_broken_reply_is_a_transient_failure_not_a_crash(self):
        import http.client

        c = self._oauth(app=self.app, expires_in=10)
        broken = mock.Mock()
        broken.open.side_effect = http.client.IncompleteRead(b"")
        with mock.patch("core.netguard.guarded_opener", return_value=broken):
            with self.assertRaises(svc.ConnectionLookupError) as ctx:
                self._issue(c)
        self.assertEqual(ctx.exception.code, "CONNECTION_REFRESH_FAILED")
        self.assertEqual(Connection.objects.get(pk=c.pk).status, "ok")

    def test_a_reply_with_neither_tokens_nor_an_error_doesnt_expire_it(self):
        # Test pass 5 (F7): an HTTP 200 page with no access token was taken for a
        # refusal, so the connection expired and its creator was emailed.
        self.st.fail = "page"
        c = self._oauth(app=self.app, expires_in=10)
        with mock.patch("core.services.notification_service.NotificationService.send_email") as send:
            with self.assertRaises(svc.ConnectionLookupError) as ctx:
                self._issue(c)
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("CONNECTION_REFRESH_FAILED", 503))
        self.assertIn("without an access token", ctx.exception.message)
        c.refresh_from_db()
        self.assertEqual((c.status, c.last_error), ("ok", ""))
        send.assert_not_called()
        self.st.fail = ""
        token = self._issue(c)["token"]  # the next try refreshes
        self.assertEqual(token, Connection.objects.get(pk=c.pk).get_credentials()["access_token"])
        self.assertNotEqual(token, c.get_credentials()["access_token"])

    def test_a_revoke_during_the_refresh_is_reported_as_revoked(self):
        c = self._oauth(app=self.app, expires_in=10)
        Connection.objects.filter(pk=c.pk).update(status=Connection.Status.REVOKED, credentials_encrypted="")
        with self.assertRaises(svc.ConnectionLookupError) as ctx:
            oauth.ensure_fresh(c)  # the caller's copy still says ok
        self.assertEqual(ctx.exception.code, "CONNECTION_REVOKED")
        self.assertEqual(self.st.refresh_calls, 0)

    def test_no_refresh_token_or_a_deleted_app_expires_it(self):
        lone = self._oauth(name="lone", refresh_token="", expires_in=5)
        orphan = self._oauth(name="orphan", app=None, expires_in=5)
        with mock.patch("core.services.notification_service.NotificationService.send_email"):
            for c, words in ((lone, "no refresh token"), (orphan, "OAuth app was deleted")):
                with self.assertRaises(svc.ConnectionLookupError):
                    self._issue(c)
                c.refresh_from_db()
                self.assertEqual(c.status, "expired")
                self.assertIn(words, c.last_error)

    def test_a_token_without_an_expiry_is_never_refreshed(self):
        c = self._oauth(app=self.app, expires_in=None)
        self.assertIsNone(self._issue(c)["expires_in"])
        self.assertEqual(self.st.refresh_calls, 0)

    def test_sqlite_waits_for_the_process_that_claimed_the_refresh(self):
        """The conditional-UPDATE path (what SQLite uses), driven directly on either engine."""
        c = self._oauth(app=self.app, expires_in=10)
        Connection.objects.filter(pk=c.pk).update(refreshing_until=timezone.now() + timedelta(seconds=20))
        new_expiry = timezone.now() + timedelta(hours=1)

        def the_other_process_finishes(_seconds):
            row = Connection.objects.get(pk=c.pk)
            row.set_credentials({"access_token": "at-from-the-other-0009", "refresh_token": REFRESH})
            row.token_expires_at, row.refreshing_until = new_expiry, None
            row.save()

        with mock.patch("core.services.oauth._sleep", side_effect=the_other_process_finishes) as slept:
            outcome, _message, row = oauth._refresh_claimed(c)
        self.assertEqual((outcome, self.st.refresh_calls), ("ok", 0))
        self.assertEqual(slept.call_count, 1)
        self.assertEqual(Connection.objects.get(pk=row.pk).get_credentials()["access_token"], "at-from-the-other-0009")

    def test_sqlite_takes_over_a_stale_claim_and_releases_it(self):
        c = self._oauth(app=self.app, expires_in=10)
        Connection.objects.filter(pk=c.pk).update(refreshing_until=timezone.now() - timedelta(seconds=1))
        outcome, _message, _row = oauth._refresh_claimed(c)
        self.assertEqual((outcome, self.st.refresh_calls), ("ok", 1))
        self.assertIsNone(Connection.objects.get(pk=c.pk).refreshing_until)

    def test_the_test_button_refreshes_then_calls_whoami(self):
        c = self._oauth(app=self.app, expires_in=10)
        ok, message = svc.check_connection(Connection.objects.get(pk=c.pk))
        self.assertTrue(ok, message)
        self.assertEqual(message, "Connected as robot@example.com.")
        self.assertEqual(self.st.refresh_calls, 1)

    def test_the_test_button_refreshes_once_on_a_401(self):
        # 2.5 T52 (test pass 6): a token the service no longer takes (not yet
        # expired by its clock) is refreshed and tried again once, like a script's call.
        from django.core.cache import cache

        c = self._oauth(app=self.app, expires_in=3600)
        self.st.access.discard("at-t24-old-0000")
        self.addCleanup(cache.delete, f"pyrunner:oauth:refused:{c.pk}")
        ok, message = svc.check_connection(Connection.objects.get(pk=c.pk))
        self.assertTrue(ok, message)
        self.assertEqual(message, "Connected as robot@example.com.")
        self.assertEqual(self.st.refresh_calls, 1)
        self.assertEqual(Connection.objects.get(pk=c.pk).status, "ok")

        # Refused again right after: no second refresh within the minute, and the test fails.
        self.st.access.clear()
        ok, message = svc.check_connection(Connection.objects.get(pk=c.pk))
        self.assertFalse(ok)
        self.assertIn("HTTP 401", message)
        self.assertEqual(self.st.refresh_calls, 1)


@skipUnless(db_connection.vendor == "postgresql", "the row-lock path is Postgres's")
class PostgresSingleFlightTests(_ProviderMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        _setup_wizard_off(self)
        ws = Workspace.get_default() or Workspace.objects.create(name="T24", is_default=True)
        self.ws = ws
        app = self._app()
        self.connection = Connection(workspace=ws, name="g", provider="google", auth_kind=OAUTH2,
                                     oauth_client=app, token_expires_at=timezone.now() + timedelta(seconds=5))
        self.connection.set_credentials({"access_token": "at-old", "refresh_token": REFRESH})
        self.connection.save()

    def test_two_refreshers_at_once_make_one_refresh(self):
        self.st.delay = 0.5  # the first holds the row lock while the provider answers
        got, errors = [], []

        def refresher():
            from django.db import connection as thread_db

            try:
                row = oauth.ensure_fresh(Connection.objects.get(pk=self.connection.pk))
                got.append(row.get_credentials()["access_token"])
            except Exception as e:  # pragma: no cover - reported below
                errors.append(e)
            finally:
                thread_db.close()

        threads = [threading.Thread(target=refresher) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(self.st.refresh_calls, 1)
        self.assertEqual(len(set(got)), 1)


# --------------------------------------------------------------------------- #
# What scripts get
# --------------------------------------------------------------------------- #


class LoopbackTests(_ProviderMixin, _Fixture, TestCase):
    def test_the_token_reply_has_the_access_token_and_never_the_refresh_token_or_secret(self):
        app = self._app()
        script = self._script()
        run = self._run(script)
        c = self._oauth(app=app, expires_in=30)  # refreshed on this call
        ConnectionGrant.objects.create(script=script, connection=c)
        resp = self.client.post(
            reverse("internal:connections_token"), data=json.dumps({"connection": "google"}),
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {mint_datastore_token(run.id)}",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()
        self.assertIn(data["token"], self.st.access)
        self.assertEqual(data["headers"], {"Authorization": f"Bearer {data['token']}"})
        self.assertEqual((data["auth_kind"], data["base_url"]), (OAUTH2, self.base))
        self.assertGreater(data["expires_in"], 3000)
        for secret in (REFRESH, CLIENT_SECRET):
            self.assertNotIn(secret, resp.content.decode())
        self.assertIn(data["token"], svc.issued_masks(run.pk).values())


class EndToEndTests(_ProviderMixin, _Fixture, LiveServerTestCase):
    """A real script uses an OAuth connection whose token expires during the run."""

    host = "127.0.0.1"

    CODE = (
        "import os, pyrunner_connections as pc\n"
        "first = pc.token('google')\n"
        "again = pc.token('google')\n"
        "print('token=' + first)\n"
        "print('same=' + str(first == again))\n"
        "print('whoami=' + str(pc.client('google').get('/whoami').json()))\n"
        "env = ' '.join(os.environ.values())\n"
        f"print('refresh_in_env=' + str({REFRESH!r} in env))\n"
        f"print('secret_in_env=' + str({CLIENT_SECRET!r} in env))\n"
    )

    def test_only_a_masked_access_token_reaches_the_script_and_its_output(self):
        script = self._script(code=self.CODE)
        c = self._oauth(app=self._app(), expires_in=20)  # inside the refresh window
        ConnectionGrant.objects.create(script=script, connection=c)
        run = self._run(script, status=Run.Status.PENDING)
        with override_settings(PYRUNNER_INTERNAL_BASE_URL=self.live_server_url), _ai_off(), \
                mock.patch("core.executor.resolve_secrets_for_run", return_value={}), \
                mock.patch("core.executor._validate_environment", return_value=sys.executable):
            execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.SUCCESS, run.stderr)
        self.assertEqual(self.st.refresh_calls, 1)  # refreshed once; the second token() was cached
        self.assertIn("token=[connection:google.", run.stdout)
        self.assertIn("same=True", run.stdout)
        self.assertIn("robot@example.com", run.stdout)
        self.assertIn("refresh_in_env=False", run.stdout)
        self.assertIn("secret_in_env=False", run.stdout)
        for value in [*self.st.access, REFRESH, CLIENT_SECRET]:
            self.assertNotIn(value, run.stdout)
        self.assertEqual(RunMask.objects.filter(run=run).count(), 0)


# --------------------------------------------------------------------------- #
# Console: OAuth apps
# --------------------------------------------------------------------------- #


class OAuthAppTests(_ProviderMixin, _Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def _add(self, **data):
        return self.client.post(reverse("cpanel:oauth_app_create"), {
            "provider": "google", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, **data,
        })

    def test_a_member_adds_the_workspace_app_and_its_secret_stays_hidden(self):
        resp = self._add(instance_default="on")  # not a superuser: ignored
        self.assertRedirects(resp, reverse("cpanel:oauth_app_list"))
        app = OAuthClient.objects.get()
        self.assertEqual((app.workspace, app.provider, app.get_secret()), (self.ws, "google", CLIENT_SECRET))
        self.assertNotIn(CLIENT_SECRET, app.client_secret_encrypted)
        page = self.client.get(reverse("cpanel:oauth_app_edit", kwargs={"pk": app.pk})).content.decode()
        self.assertNotIn(CLIENT_SECRET, page)
        self.assertIn("http://testserver/oauth/callback/", page)
        self.client.post(reverse("cpanel:oauth_app_edit", kwargs={"pk": app.pk}),
                         {"client_id": "renamed-t24.apps.googleusercontent.com", "client_secret": ""})
        app.refresh_from_db()
        self.assertEqual((app.client_id, app.get_secret()), ("renamed-t24.apps.googleusercontent.com", CLIENT_SECRET))  # blank keeps
        self.assertContains(self._add(), "already has a Google app")

    def test_the_instance_default_is_a_superusers_and_the_workspace_app_wins(self):
        admin = User.objects.create_superuser(username="t24admin", email="a@t24.test", password="pw")
        self.client.force_login(admin)
        self._add(instance_default="on", client_id="default-t24.apps.googleusercontent.com")
        default = OAuthClient.objects.get(workspace__isnull=True)
        self.assertEqual(oauth.client_for(self.ws, "google"), default)
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.get(reverse("cpanel:oauth_app_edit", kwargs={"pk": default.pk})).status_code, 404
        )
        self.assertEqual(
            self.client.post(reverse("cpanel:oauth_app_delete", kwargs={"pk": default.pk})).status_code, 404
        )
        self._add()
        self.assertEqual(oauth.client_for(self.ws, "google").workspace, self.ws)
        self.assertEqual(oauth.client_for(self.other_ws, "google"), default)
        self.assertContains(self.client.get(reverse("cpanel:oauth_app_list")), "not used here")

    def test_a_client_secret_pasted_as_the_client_id_is_refused(self):
        # Run 7 B2: Hasan pasted Google's GOCSPX- secret and a Microsoft "~" secret value into
        # Client ID; Connect failed oddly and the OAuth apps page showed the secret.
        guid = "1ef363fc-2b6a-4c55-9e1d-0a6b7c8d9e0f"
        accepted = {
            "google": ("guard-t24.apps.googleusercontent.com",),
            "microsoft": (guid, guid.upper()),
            "github": ("Ov23liABCdef1234567890", "Iv1.0123456789abcdef", "0123456789abcdef0123"),
            "slack": ("1234567890123.9876543210987",),
            "notion": (guid, "1ef363fc2b6a4c559e1d0a6b7c8d9e0f"),
        }
        refused = {
            "google": (("GOCSPX-t24-not-a-real-secret", "This looks like the client secret. "
                        "The Client ID ends in .apps.googleusercontent.com."),
                       ("123456789012-abc123def456", "This doesn't look like a client ID. "
                        "The Client ID ends in .apps.googleusercontent.com.")),
            "microsoft": (("Abc8Q~dEfGhIjKlMnOpQrStUvWxYz0123456789a", "This doesn't look like a client ID; "
                           "check it isn't the secret."),
                          ("not-a-guid-at-all", "Use the Application (client) ID from the app's Overview page")),
        }
        generic = "This doesn't look like a client ID; check it isn't the secret."
        for key in OAUTH_KEYS:
            for value in accepted[key]:
                with self.subTest(provider=key, value=value):
                    self.assertEqual(get_preset(key).oauth.client_id_problem(value), "")
            for value, message in refused.get(key, ()):
                with self.subTest(provider=key, value=value):
                    self.assertIn(message, get_preset(key).oauth.client_id_problem(value))
            for value in ("Abc8Q~dEf123", "two words"):
                with self.subTest(provider=key, value=value):
                    self.assertEqual(get_preset(key).oauth.client_id_problem(value), generic)

        # Through the form: refused, never echoed, the Client ID box comes back empty, nothing saved.
        for provider, value in (("google", "GOCSPX-t24-not-a-real-secret"),
                                ("microsoft", "Abc8Q~dEfGhIjKlMnOpQrStUvWxYz0123456789a"),
                                ("github", "gh secret value")):
            with self.subTest(form=provider):
                resp = self._add(provider=provider, client_id=value)
                self.assertEqual(resp.status_code, 200)
                self.assertNotContains(resp, value)
                self.assertNotContains(resp, CLIENT_SECRET)
                self.assertContains(resp, 'name="client_id"')
                self.assertNotContains(resp, 'name="client_id" value=')
                self.assertTrue(resp.context["form"].errors["client_id"])
        self.assertFalse(OAuthClient.objects.exists())

    def test_the_same_value_in_both_boxes_is_refused(self):
        # GitHub has no format rule: the same-value check still catches the swap.
        value = "0123456789abcdef0123456789abcdef01234567"
        resp = self._add(provider="github", client_id=value, client_secret=value)
        self.assertContains(resp, "The Client ID and the client secret are the same")
        self.assertNotContains(resp, value)
        self.assertFalse(OAuthClient.objects.exists())

    def test_an_edit_with_the_secret_left_blank_still_checks_the_id(self):
        self._add()
        app = OAuthClient.objects.get()
        edit = reverse("cpanel:oauth_app_edit", kwargs={"pk": app.pk})
        for value in ("GOCSPX-t24-not-a-real-secret", CLIENT_SECRET):
            with self.subTest(value=value):
                resp = self.client.post(edit, {"client_id": value, "client_secret": ""})
                self.assertEqual(resp.status_code, 200)
                self.assertNotContains(resp, value)
                self.assertTrue(resp.context["form"].errors["client_id"])
        self.assertContains(self.client.post(edit, {"client_id": CLIENT_SECRET, "client_secret": ""}),
                            "The Client ID and the client secret are the same")
        app.refresh_from_db()
        self.assertEqual((app.client_id, app.get_secret()), (CLIENT_ID, CLIENT_SECRET))
        good = "edited-t24.apps.googleusercontent.com"
        self.assertRedirects(self.client.post(edit, {"client_id": good, "client_secret": ""}),
                             reverse("cpanel:oauth_app_list"))
        app.refresh_from_db()
        self.assertEqual(app.client_id, good)

    def test_a_tenant_is_a_plain_identifier(self):
        resp = self.client.post(reverse("cpanel:oauth_app_create"), {
            "provider": "microsoft", "client_id": CLIENT_ID, "client_secret": "s", "tenant": "evil.test/x?",
        })
        self.assertContains(resp, "Letters, digits")
        self.assertFalse(OAuthClient.objects.exists())

    def test_deleting_an_app_unlinks_its_connections(self):
        app = self._app()
        c = self._oauth(app=app)
        self.client.post(reverse("cpanel:oauth_app_delete", kwargs={"pk": app.pk}))
        c.refresh_from_db()
        self.assertIsNone(c.oauth_client)

    def test_the_google_page_offers_connect_and_paste(self):
        url = reverse("cpanel:connection_create") + "?provider=google"
        html = self.client.get(url).content.decode()
        self.assertIn("No Google OAuth app yet", html)
        self.assertIn("Or paste a token", html)
        self._app()
        html = self.client.get(url).content.decode()
        self.assertIn('value="connect"', html)
        self.assertIn("http://testserver/oauth/callback/", html)
        self.assertIn("Sheets (spreadsheets)", html)
        picker = self.client.get(reverse("cpanel:connection_create")).content.decode()
        for label in ("Google", "Microsoft 365", "GitHub", "Slack", "Notion"):
            self.assertIn(label, picker)


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #


class OAuthBackupTests(_ProviderMixin, _Fixture, TestCase):
    def test_apps_and_oauth_connections_round_trip(self):
        app = self._app()
        default = self._app(workspace=None, provider="github")
        c = self._oauth(app=app, scopes=["openid", SHEETS])
        data = BackupService.create_backup(created_by_user=self.user)
        self.assertNotIn(CLIENT_SECRET, json.dumps(data))
        self.assertNotIn(REFRESH, json.dumps(data))
        self.assertEqual(BackupService.get_backup_preview(data)["counts"]["oauth_clients"], 2)
        result = BackupService.restore_backup(data, current_user=self.user)
        self.assertTrue(result["success"], result["errors"])
        self.assertEqual(result["counts"]["oauth_clients"], 2)
        back = Connection.objects.get(pk=c.pk)
        self.assertEqual((back.oauth_client_id, back.scopes), (app.pk, ["openid", SHEETS]))
        self.assertAlmostEqual((back.token_expires_at - c.token_expires_at).total_seconds(), 0, delta=1)
        self.assertEqual(back.get_credentials()["refresh_token"], REFRESH)
        self.assertEqual(OAuthClient.objects.get(pk=app.pk).get_secret(), CLIENT_SECRET)
        self.assertIsNone(OAuthClient.objects.get(pk=default.pk).workspace)

    def test_the_restore_preview_lists_every_kind_it_counts(self):
        # Test pass 5 (F8): the preview counted oauth_clients but its "This backup contains" list didn't show
        # them, though a restore replaces the OAuth apps. Every counted kind needs its row in the modal.
        from django.template.loader import get_template

        source = get_template("cpanel/settings.html").template.source
        self.assertIn("['OAuth apps', 'oauth_clients']", source)
        counts = BackupService.get_backup_preview(BackupService.create_backup(created_by_user=self.user))["counts"]
        for key in counts:
            if key != "package_operations":  # internal bookkeeping, never listed
                self.assertIn(f"'{key}']", source, key)

    def test_an_older_backup_leaves_the_apps_alone(self):
        app = self._app()
        data = BackupService.create_backup(created_by_user=self.user)
        del data["oauth_clients"]
        del data["connections"]
        self.assertTrue(BackupService.restore_backup(data, current_user=self.user)["success"])
        self.assertTrue(OAuthClient.objects.filter(pk=app.pk).exists())
