"""
Connections in the plugin SDK (2.5 Stage 5, ticket T25).

What this pins, roughly in order of how much it would hurt to get wrong:
- A plugin's web code gets fresh auth material through ``ConnectionAPI.credentials``
  (an expiring OAuth token is refreshed first) and never a refresh token or a
  client secret; a revoked or expired connection raises ConnectionUnavailable.
- The plugin page's Connect makes a connection the plugin holds for that
  requirement (owner_plugin + owner_key), asks for the manifest's scopes and
  comes back to the page; picking an existing connection replaces the plugin's
  previous one (released, with its plugin grants dropped), a connection another
  plugin uses can't be taken, and only manifest-declared keys can be acted on.
- Status: missing, ok, missing_scopes (the account didn't grant a scope asked
  for), and the connection's own expired/revoked/error.
- ``require``/``get``/``list``/``grant``: validation as the doctor's, idempotent
  grants, other workspaces refused.
- The doctor validates ``requires_connections``; deleting a plugin with its
  data releases its connections (never deletes them); backups keep which plugin
  holds a connection, and a second holder of one requirement in a hand-edited
  backup is restored without it.
No test reaches a real provider: OAuth goes to T24's in-process provider.
"""

import re
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from core.integrations.presets import OAUTH2
from core.models import Connection, ConnectionGrant, Plugin
from core.plugins.api import ConnectionAPI, ConnectionUnavailable, ConnectionView, ScriptAPI
from core.services import plugin_connections as pc
from core.services.backup_service import BackupService
from core.services.plugin_doctor import FAIL, PASS, WARN, run_doctor
from core.services.plugin_service import PluginService
from core.test_connections import AIRTABLE_KEY, _Fixture
from core.test_connections_oauth import CLIENT_SECRET, REFRESH, SHEETS, _ProviderMixin
from core.test_plugin_doctor import _Builder

User = get_user_model()

SLUG = "t25plug"
GMAIL_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
YOUTUBE = "https://www.googleapis.com/auth/youtube.force-ssl"
MANIFEST = {
    "slug": SLUG,
    "name": "T25 Plug",
    "version": "1.0.0",
    "requires_connections": [
        {"key": "sheet", "provider": "google", "scopes": ["sheets"], "label": "The sheet to fill"},
        {"key": "mail", "provider": "google", "scopes": ["gmail_modify", YOUTUBE]},
        {"provider": "airtable", "label": "Your base"},
    ],
}


def _findings(report, rule):
    return [(f.severity, f.message) for f in report.findings if f.rule == rule]


# --------------------------------------------------------------------------- #
# The manifest entries
# --------------------------------------------------------------------------- #


class RequirementTests(SimpleTestCase):
    def test_an_entry_and_its_scopes(self):
        r = pc.parse_requirement({"provider": "google", "scopes": ["gmail_modify", YOUTUBE, "gmail_modify"]})
        self.assertEqual((r.key, r.provider, r.scopes, r.label), ("google", "google", ("gmail_modify", YOUTUBE), ""))
        self.assertEqual(r.split_scopes(), (["gmail_modify"], [YOUTUBE]))
        self.assertEqual(r.wanted_scopes(), [GMAIL_MODIFY, YOUTUBE])
        self.assertEqual(r.connect_config(), {"scope_sets": ["gmail_modify"], "extra_scopes": YOUTUBE})
        # Connect also asks for what whoami needs.
        self.assertEqual(r.connect_scopes(), ["openid", "email", GMAIL_MODIFY, YOUTUBE])
        airtable = pc.parse_requirement({"provider": "airtable"})
        self.assertFalse(airtable.is_oauth)
        self.assertEqual(airtable.wanted_scopes(), [])

    def test_bad_entries_are_refused(self):
        for entry, phrase in (
            ("google", "must be an object"),
            ({}, "needs a 'provider'"),
            ({"provider": "myspace"}, "unknown provider"),
            ({"provider": "google", "key": "Bad Key"}, "lowercase letters"),
            ({"provider": "google", "key": ""}, "lowercase letters"),
            ({"provider": "google", "scopes": "sheets"}, "must be a list"),
            ({"provider": "google", "scopes": ["a b"]}, "no spaces"),
            ({"provider": "google", "scopes": ["a,b"]}, "no spaces"),
            ({"provider": "airtable", "scopes": ["x"]}, "have no scopes"),
            ({"provider": "google", "label": "x" * 121}, "120 characters"),
        ):
            with self.subTest(entry=entry), self.assertRaisesMessage(ValueError, phrase):
                pc.parse_requirement(entry)

    def test_manifest_problems(self):
        self.assertEqual(pc.manifest_problems(None), ([], []))
        self.assertEqual(pc.manifest_problems({"provider": "google"})[0],
                         ["'requires_connections' must be a list of objects."])
        failures, warnings = pc.manifest_problems([
            {"provider": "google"}, {"provider": "google", "colour": "red"}, {"provider": "nope"},
        ])
        self.assertEqual(len(failures), 2)
        self.assertIn("'google' is used twice", failures[0])
        self.assertIn("unknown provider", failures[1])
        self.assertEqual(warnings, ["requires_connections[google]: unknown field(s) colour."])
        self.assertIn("21 entries", pc.manifest_problems([{"provider": "github", "key": f"k{i}"} for i in range(21)])[0][0])

    def test_the_console_skips_what_the_doctor_refuses(self):
        manifest = {"requires_connections": [{"provider": "nope"}, {"provider": "github"}, {"provider": "github"}]}
        self.assertEqual([r.key for r in pc.requirements_from_manifest(manifest)], ["github"])
        self.assertEqual(pc.requirements_from_manifest({"requires_connections": "x"}), [])
        self.assertEqual(pc.requirements_from_manifest(None), [])


class DoctorTests(SimpleTestCase):
    def setUp(self):
        self.b = _Builder()
        self.addCleanup(self.b.cleanup)

    def _with(self, value):
        import json

        folder = self.b.make()
        manifest = {"slug": "goodplug", "name": "X", "version": "1.0.0", "api": "2.8", "requires_connections": value}
        (folder / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        return run_doctor(folder)

    def test_valid_entries_pass(self):
        report = self._with(MANIFEST["requires_connections"])
        self.assertEqual(_findings(report, "connections"), [(PASS, "requires_connections entries are valid.")])
        self.assertTrue(report.ok, report.format())

    def test_bad_entries_fail_activation(self):
        report = self._with([{"provider": "google", "scopes": ["has space"]}, {"key": "x"}])
        found = _findings(report, "connections")
        self.assertEqual([s for s, _m in found], [FAIL])
        self.assertIn("no spaces", found[0][1])
        self.assertIn("needs a 'provider'", found[0][1])
        self.assertFalse(report.ok)

    def test_an_unknown_field_only_warns(self):
        report = self._with([{"provider": "github", "why": "issues"}])
        self.assertEqual([s for s, _m in _findings(report, "connections")], [WARN])
        self.assertTrue(report.ok)

    def test_no_entry_no_finding(self):
        self.assertEqual(_findings(run_doctor(self.b.make()), "connections"), [])


# --------------------------------------------------------------------------- #
# The SDK
# --------------------------------------------------------------------------- #


class _PluginFixture(_Fixture):
    def setUp(self):
        super().setUp()
        self.plugin = Plugin.objects.create(slug=SLUG, name="T25 Plug", version="1.0.0", manifest=MANIFEST)
        self.api = ConnectionAPI(SLUG, workspace=self.ws)

    def _held(self, key, **kwargs):
        kwargs.setdefault("name", f"{SLUG}-{key}")
        return self._connection(owner_plugin=SLUG, owner_key=key, **kwargs)

    def _plugin_script(self, key="worker", workspace=None):
        return ScriptAPI(SLUG, workspace=workspace or self.ws).upsert(key=key, name=f"t25 {key}", code="pass", environment=self.env)


class SdkTests(_PluginFixture, TestCase):
    def test_needs_an_owner(self):
        with self.assertRaises(ValueError):
            ConnectionAPI(None)

    def test_require_reports_the_status(self):
        self.assertEqual(self.api.require("airtable").status, "missing")
        self.assertIsNone(self.api.require("airtable").connection)
        self._held("airtable")
        req = self.api.require("airtable", label="Your base")
        self.assertTrue(req.ok)
        self.assertEqual((req.key, req.label, req.connection.name), ("airtable", "Your base", f"{SLUG}-airtable"))
        with self.assertRaisesMessage(ValueError, "unknown provider"):
            self.api.require("nope")
        # Another plugin's, or another workspace's, connection doesn't count.
        self.assertEqual(ConnectionAPI("other", workspace=self.ws).require("airtable").status, "missing")
        self.assertEqual(ConnectionAPI(SLUG, workspace=self.other_ws).require("airtable").status, "missing")

    def test_missing_scopes_and_the_connections_own_status(self):
        c = self._held("mail", provider="google", auth_kind=OAUTH2,
                       credentials={"access_token": "at-x"}, scopes=["openid", "email", GMAIL_MODIFY])
        req = self.api.require("google", ["gmail_modify", YOUTUBE], key="mail")
        self.assertEqual((req.status, req.missing_scopes), ("missing_scopes", (YOUTUBE,)))
        self.assertTrue(self.api.require("google", ["gmail_modify"], key="mail").ok)
        # A pasted token that says nothing of its scopes isn't flagged.
        Connection.objects.filter(pk=c.pk).update(scopes=[])
        self.assertTrue(self.api.require("google", ["gmail_modify", YOUTUBE], key="mail").ok)
        for status in ("expired", "revoked", "error"):
            Connection.objects.filter(pk=c.pk).update(status=status)
            self.assertEqual(self.api.require("google", key="mail").status, status)

    def test_get_and_list_never_carry_a_credential(self):
        self.assertIsNone(self.api.get("airtable"))
        self._held("airtable", account_label="me@base")
        self._connection(name="mine")  # a user connection: not the plugin's
        view = self.api.get("airtable")
        self.assertIsInstance(view, ConnectionView)
        self.assertEqual((view.name, view.key, view.status, view.account_label), (f"{SLUG}-airtable", "airtable", "ok", "me@base"))
        self.assertNotIn(AIRTABLE_KEY, repr(view))
        self.assertEqual([v.name for v in self.api.list()], [f"{SLUG}-airtable"])

    def test_grant_is_idempotent_and_stays_in_the_workspace(self):
        script = self._plugin_script()
        with self.assertRaises(ConnectionUnavailable) as cm:
            self.api.grant(script, "airtable")
        self.assertEqual(cm.exception.code, "CONNECTION_NOT_FOUND")
        c = self._held("airtable")
        g1 = self.api.grant(script, "airtable")
        g2 = self.api.grant(script, self.api.get("airtable"))
        self.assertEqual((g1.pk, ConnectionGrant.objects.count()), (g2.pk, 1))
        self.api.grant(script, c, active=False)
        self.assertFalse(ConnectionGrant.objects.get().active)
        self.api.grant(script, c)
        self.assertTrue(ConnectionGrant.objects.get().active)
        elsewhere = self._plugin_script(workspace=self.other_ws)
        with self.assertRaisesMessage(ValueError, "its own workspace"):
            self.api.grant(elsewhere, c)

    def test_credentials_outside_a_run(self):
        with self.assertRaises(ConnectionUnavailable) as cm:
            self.api.credentials("airtable")
        self.assertEqual(cm.exception.code, "CONNECTION_NOT_FOUND")
        c = self._held("airtable")
        material = self.api.credentials("airtable")
        self.assertEqual(material["headers"]["Authorization"], f"Bearer {AIRTABLE_KEY}")
        self.assertEqual((material["name"], material["provider"]), (c.name, "airtable"))
        Connection.objects.filter(pk=c.pk).update(status="revoked", credentials_encrypted="")
        with self.assertRaises(ConnectionUnavailable) as cm:
            self.api.credentials("airtable")
        self.assertEqual(cm.exception.code, "CONNECTION_REVOKED")

    def test_requirements_read_the_installed_manifest(self):
        states = self.api.requirements()
        self.assertEqual([(s.key, s.status, s.label) for s in states], [
            ("sheet", "missing", "The sheet to fill"),
            ("mail", "missing", "A Google account"),
            ("airtable", "missing", "Your base"),
        ])
        self.assertEqual(states[1].scope_labels, ["Gmail: read, label and send", YOUTUBE])
        self.assertEqual(ConnectionAPI("not_installed").requirements(), [])

    def test_one_connection_per_requirement(self):
        self._held("airtable")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._held("airtable", name="second")
        # User connections (no plugin) are unaffected.
        self._connection(name="u1")
        self._connection(name="u2")


class SdkOAuthTests(_ProviderMixin, _PluginFixture, TestCase):
    def test_credentials_refresh_an_expiring_token_and_never_carry_the_refresh_token(self):
        app = self._app()
        self._oauth(name=f"{SLUG}-sheet", expires_in=30, app=app, owner_plugin=SLUG, owner_key="sheet")
        material = self.api.credentials("sheet")
        self.assertEqual(self.st.refresh_calls, 1)
        self.assertIn(material["token"], self.st.access)
        self.assertNotEqual(material["token"], "at-t24-old-0000")
        self.assertGreater(material["expires_in"], 3000)
        flat = repr(material)
        for value in (REFRESH, CLIENT_SECRET):
            self.assertNotIn(value, flat)
        # Still fresh: no second refresh.
        self.api.credentials("sheet")
        self.assertEqual(self.st.refresh_calls, 1)

    def test_a_refused_refresh_is_connection_expired(self):
        app = self._app()
        self._oauth(name=f"{SLUG}-sheet", expires_in=30, app=app, owner_plugin=SLUG, owner_key="sheet")
        self.st.fail = "refuse"
        with mock.patch("core.services.oauth.notify_expired"), self.assertRaises(ConnectionUnavailable) as cm:
            self.api.credentials("sheet")
        self.assertEqual(cm.exception.code, "CONNECTION_EXPIRED")
        self.assertEqual(self.api.require("google", key="sheet").status, "expired")


# --------------------------------------------------------------------------- #
# The plugin page
# --------------------------------------------------------------------------- #


class PluginPageTests(_ProviderMixin, _PluginFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser(
            username=f"a{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@t25.test", password="pw"
        )
        from core.models import WorkspaceMembership

        WorkspaceMembership.objects.update_or_create(
            user=self.admin, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_OWNER}
        )
        self.client.force_login(self.admin)
        self.page = reverse("cpanel:plugin_detail", kwargs={"slug": SLUG})

    def _post(self, action, key, **data):
        data.setdefault("next", self.page)
        return self.client.post(
            reverse(f"cpanel:plugin_connection_{action}", kwargs={"slug": SLUG, "key": key}), data
        )

    def test_the_plugin_page_shows_each_requirement(self):
        self._held("airtable", account_label="me@base")
        mine = self._connection(name="my-google", provider="google", auth_kind=OAUTH2,
                                credentials={"access_token": "at-y"})
        resp = self.client.get(self.page)
        self.assertContains(resp, "Connections this plugin needs")
        self.assertContains(resp, 'data-requirement="sheet"')
        self.assertContains(resp, "The sheet to fill")
        self.assertContains(resp, 'data-status="missing"', count=2)
        self.assertContains(resp, 'data-status="ok"')
        self.assertContains(resp, "me@base")
        # No Google OAuth app yet: no Connect button, a pointer to the apps page instead.
        self.assertNotContains(resp, "Connect Google")
        self.assertContains(resp, reverse("cpanel:oauth_app_list"))
        # The picker offers the workspace's own Google connection.
        self.assertContains(resp, f'<option value="{mine.pk}">my-google</option>', html=True)
        self._app()
        self.assertContains(self.client.get(self.page), "Connect Google", count=2)

    def test_the_picker_shrinks_to_the_card_at_phone_width(self):
        # Test pass 5 (F9): a long "name (account)" option made the picker's form wider than a 390 px screen
        # (the page scrolled sideways); the form and its select may now shrink and wrap.
        self._connection(name="a-long-google-connection-name", provider="google", auth_kind=OAUTH2,
                         account_label="someone.with.a.long.address@example.test", credentials={"access_token": "at"})
        html = self.client.get(self.page).content.decode()
        use = reverse("cpanel:plugin_connection_use", kwargs={"slug": SLUG, "key": "sheet"})
        form = re.search(r'<form method="post" action="%s" class="([^"]+)"' % re.escape(use), html).group(1)
        self.assertIn("min-w-0 max-w-full", form)
        self.assertIn("flex-wrap", form)
        select = re.search(r'<select id="pc-[^"]+" name="connection" class="([^"]+)"', html).group(1)
        self.assertIn("min-w-0 max-w-full", select)

    def test_connect_makes_the_plugins_connection_and_comes_back(self):
        self._app()
        resp = self._post("connect", "sheet")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith(self.base + "/authorize?"))
        callback, query = self.st.consent(resp["Location"])
        self.assertEqual(query["scope"].split(" "), ["openid", "email", SHEETS])
        resp = self.client.get(reverse("oauth_callback"), callback)
        self.assertRedirects(resp, self.page, fetch_redirect_response=False)
        c = Connection.objects.get()
        self.assertEqual((c.name, c.owner_plugin, c.owner_key, c.status), (f"{SLUG}-sheet", SLUG, "sheet", "ok"))
        self.assertEqual(c.config, {"scope_sets": ["sheets"], "extra_scopes": ""})
        self.assertTrue(self.api.require("google", ["sheets"], key="sheet").ok)
        # Reconnect updates the same row; a requirement asking for more widens its config.
        resp = self._post("connect", "sheet")
        self.client.get(reverse("oauth_callback"), self.st.consent(resp["Location"])[0])
        self.assertEqual(Connection.objects.get().pk, c.pk)

    def test_connect_for_a_held_connection_asks_for_the_requirements_scopes_too(self):
        self._app()
        c = self._oauth(name="picked", owner_plugin=SLUG, owner_key="mail")
        resp = self._post("connect", "mail")
        _callback, query = self.st.consent(resp["Location"])
        self.assertEqual(query["scope"].split(" "), ["openid", "email", SHEETS, GMAIL_MODIFY, YOUTUBE])
        c.refresh_from_db()
        self.assertEqual(c.config, {"scope_sets": ["sheets", "gmail_modify"], "extra_scopes": YOUTUBE})

    def test_a_name_taken_by_the_user_gets_a_suffix(self):
        self._app()
        self._connection(name=f"{SLUG}-sheet")
        resp = self._post("connect", "sheet")
        self.client.get(reverse("oauth_callback"), self.st.consent(resp["Location"])[0])
        self.assertEqual(Connection.objects.get(owner_plugin=SLUG).name, f"{SLUG}-sheet-2")

    def test_connect_refusals(self):
        resp = self._post("connect", "sheet")
        self.assertRedirects(resp, self.page, fetch_redirect_response=False)
        self.assertIn("Add a Google OAuth app first", [str(m) for m in resp.wsgi_request._messages][-1])
        resp = self._post("connect", "airtable")
        self.assertIn("added on the Connections page", [str(m) for m in resp.wsgi_request._messages][-1])
        self.assertEqual(self._post("connect", "undeclared").status_code, 404)
        self.assertEqual(self.client.post(reverse(
            "cpanel:plugin_connection_connect", kwargs={"slug": "nope", "key": "sheet"})).status_code, 404)
        self.assertFalse(Connection.objects.exists())

    def test_a_foreign_next_is_ignored(self):
        resp = self._post("release", "sheet", next="https://evil.example/")
        self.assertRedirects(resp, reverse("cpanel:connection_list"), fetch_redirect_response=False)
        self._app()
        resp = self._post("connect", "sheet", next="//evil.example/x")
        resp = self.client.get(reverse("oauth_callback"), self.st.consent(resp["Location"])[0])
        c = Connection.objects.get()
        self.assertRedirects(resp, reverse("cpanel:connection_edit", kwargs={"pk": c.pk}),
                             fetch_redirect_response=False)

    def test_use_replaces_the_previous_connection_and_its_plugin_grants(self):
        script = self._plugin_script()
        user_script = self._script()
        old = self._held("airtable")
        self.api.grant(script, "airtable")
        ConnectionGrant.objects.create(script=user_script, connection=old)
        new = self._connection(name="base-two")
        resp = self._post("use", "airtable", connection=str(new.pk))
        self.assertRedirects(resp, self.page, fetch_redirect_response=False)
        old.refresh_from_db()
        new.refresh_from_db()
        self.assertEqual((new.owner_plugin, new.owner_key), (SLUG, "airtable"))
        self.assertEqual((old.owner_plugin, old.owner_key), (None, None))
        # The plugin's script lost its grant to the old one; the user's kept theirs.
        self.assertEqual(list(ConnectionGrant.objects.values_list("script_id", flat=True)), [user_script.pk])

    def test_use_refusals(self):
        theirs = self._connection(name="theirs", owner_plugin="other", owner_key="airtable")
        resp = self._post("use", "airtable", connection=str(theirs.pk))
        self.assertIn("already used by the plugin other", [str(m) for m in resp.wsgi_request._messages][-1])
        github = self._connection(name="gh", provider="github", auth_kind="bearer", credentials={"token": "x"})
        elsewhere = self._connection(name="far", workspace=self.other_ws)
        revoked = self._connection(name="rev", status="revoked")
        for pk in (github.pk, elsewhere.pk, revoked.pk, "not-a-uuid"):
            self._post("use", "airtable", connection=str(pk))
        self.assertFalse(Connection.objects.filter(owner_plugin=SLUG).exists())

    def test_a_pick_racing_another_one_is_refused_not_a_500(self):
        # Read while unowned; another plugin takes it before the update.
        stale = self._connection(name="raced")
        Connection.objects.filter(pk=stale.pk).update(owner_plugin="other", owner_key="airtable")
        with self.assertRaisesMessage(ValueError, "just taken by another plugin"):
            pc.adopt(stale, SLUG, "airtable")
        # Two picks for one key: the unique constraint keeps the first.
        first, second = self._connection(name="first"), self._connection(name="second")
        pc.adopt(first, SLUG, "airtable")
        with mock.patch.object(pc, "connection_for", return_value=None):
            with self.assertRaisesMessage(ValueError, "Another connection was just picked"):
                pc.adopt(second, SLUG, "airtable")
        self.assertEqual(pc.connection_for(self.ws, SLUG, "airtable"), first)

    def test_stop_using(self):
        script = self._plugin_script()
        c = self._held("airtable")
        self.api.grant(script, c)
        self._post("release", "airtable")
        c.refresh_from_db()
        self.assertIsNone(c.owner_plugin)
        self.assertFalse(ConnectionGrant.objects.exists())
        self.assertTrue(Connection.objects.filter(pk=c.pk).exists())

    def test_any_member_may_connect_in_their_workspace(self):
        self.client.force_login(self.user)
        c = self._connection(name="mine")
        self._post("use", "airtable", connection=str(c.pk))
        self.assertEqual(Connection.objects.get(pk=c.pk).owner_plugin, SLUG)

    def test_the_connections_list_names_the_plugin(self):
        self._held("airtable")
        self.assertContains(self.client.get(reverse("cpanel:connection_list")), f"Used by the plugin {SLUG} (airtable)")


# --------------------------------------------------------------------------- #
# Plugin delete, backups
# --------------------------------------------------------------------------- #


class LifecycleTests(_PluginFixture, TestCase):
    def test_deleting_the_plugin_with_its_data_releases_its_connections(self):
        script = self._plugin_script()
        c = self._held("airtable")
        self.api.grant(script, c)
        self.assertNotIn("connection", PluginService.owned_resource_summary(SLUG))
        # The legacy "drop the plugin's own tables" subprocess has nothing to do here.
        with mock.patch.object(PluginService, "_run_uninstall_data", return_value=(True, "")):
            PluginService.delete(self.plugin, remove_data=True)
        c.refresh_from_db()
        self.assertEqual((c.owner_plugin, c.owner_key, c.status), (None, None, "ok"))
        self.assertFalse(ConnectionGrant.objects.exists())

    def test_backup_round_trip_keeps_the_holder(self):
        self._held("airtable")
        data = BackupService.create_backup()
        Connection.objects.all().delete()
        BackupService.restore_backup(data, self.user)
        c = Connection.objects.get()
        self.assertEqual((c.owner_plugin, c.owner_key), (SLUG, "airtable"))

    def test_a_second_holder_in_a_hand_edited_backup_is_restored_without_it(self):
        self._held("airtable")
        data = BackupService.create_backup()
        twin = dict(data["connections"][0], id=str(uuid.uuid4()), name="twin")
        data["connections"].append(twin)
        result = BackupService.restore_backup(data, self.user)
        self.assertEqual(Connection.objects.count(), 2)
        self.assertEqual(Connection.objects.get(name="twin").owner_plugin, None)
        self.assertTrue(any("restored without its plugin" in w for w in result.get("warnings", [])), result)


class ProviderMismatchTests(_PluginFixture, TestCase):
    """A held connection of another provider (a hand-edited backup) is an error, not ok."""

    def test_a_held_connection_of_another_provider_is_an_error(self):
        self._held("sheet", provider="airtable")
        self.assertEqual(ConnectionAPI(SLUG, workspace=self.ws).require("google", key="sheet").status, "error")
