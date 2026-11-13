"""Console permissions, import/edit flows and portable history restoration."""

import copy
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from core.brain.backup import export_brains
from core.brain.imports import read_zip
from core.brain.service import BrainService
from core.models import APIToken, Brain, BrainGrant, Environment, Script, User, Workspace, WorkspaceMembership
from core.services.backup_service import BackupService
from core.test_brain_store import archive, contract_files, note
from core.test_plugin_api import _mock_setup


class BrainConsoleTests(TestCase):
    def setUp(self):
        _mock_setup(self)
        self.ws = Workspace.objects.create(name="Brain console")
        self.other = Workspace.objects.create(name="Other")
        self.owner = User.objects.create_user(username="brain-owner", email="owner@brain.test")
        self.member = User.objects.create_user(username="brain-member", email="member@brain.test")
        WorkspaceMembership.objects.create(workspace=self.ws, user=self.owner, role="owner")
        WorkspaceMembership.objects.create(workspace=self.ws, user=self.member, role="member")
        self.brain = BrainService.create(self.ws, "console", files=contract_files())
        self.client.force_login(self.owner)

    def url(self, view, *args):
        return reverse("cpanel_ws:brain_" + view, args=[self.ws.pk, *args])

    def test_pages_render_and_other_workspace_ids_are_unknown(self):
        foreign = BrainService.create(self.other, "foreign", files=contract_files())
        for view in ("detail", "edit", "settings", "grants", "revisions", "export"):
            with self.subTest(view=view):
                self.assertEqual(self.client.get(self.url(view, self.brain.pk)).status_code, 200)
                self.assertEqual(self.client.get(self.url(view, foreign.pk)).status_code, 404)
        self.assertEqual(
            self.client.get(self.url("diff", self.brain.pk, foreign.head_revision_id)).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                self.url("diff", self.brain.pk, self.brain.head_revision_id)
            ).status_code,
            200,
        )

    def test_member_reads_private_but_cannot_mutate(self):
        self.client.force_login(self.member)
        for view in ("list", "detail", "revisions", "export"):
            args = [] if view == "list" else [self.brain.pk]
            self.assertEqual(self.client.get(self.url(view, *args)).status_code, 200)
        for view in ("create", "edit", "settings", "grants"):
            args = [] if view == "create" else [self.brain.pk]
            self.assertEqual(self.client.post(self.url(view, *args), {}).status_code, 403)

    def test_nonmember_and_anonymous(self):
        stranger = User.objects.create_user(username="stranger", email="stranger@brain.test")
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(self.url("detail", self.brain.pk)).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(self.url("list")).status_code, 302)

    def test_create_and_zip_import_preserve_workspace(self):
        response = self.client.post(
            self.url("create"), {"name": "Seed", "slug": "seed", "source": "template"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Brain.objects.get(slug="seed").workspace, self.ws)
        upload = SimpleUploadedFile(
            "brain.zip", archive(contract_files()), content_type="application/zip"
        )
        response = self.client.post(
            self.url("create"),
            {"name": "Imported", "slug": "imported", "source": "zip", "archive": upload},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Brain.objects.get(slug="imported").contract_version, "1.0")
        response = self.client.post(
            self.url("create"), {"name": "Duplicate", "slug": "seed", "source": "template"}
        )
        self.assertContains(response, "already exists")

    def test_edit_message_conflict_diff_and_escaped_html(self):
        payload = {
            "path": "knowledge/takes/new.md",
            "content": note("new", "public") + "<script>alert('x')</script>",
            "message": "New note",
            "expected_head": str(self.brain.head_revision_id),
        }
        response = self.client.post(self.url("edit", self.brain.pk), payload)
        self.assertEqual(response.status_code, 302)
        self.brain.refresh_from_db()
        self.assertEqual(self.brain.head_revision.number, 2)
        page = self.client.get(self.url("detail", self.brain.pk), {"path": payload["path"]})
        self.assertContains(page, "&lt;script&gt;")
        self.assertNotContains(page, "<script>alert")
        response = self.client.post(
            self.url("edit", self.brain.pk), {**payload, "content": "stale"}
        )
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "Reload and review", status_code=409)

    def test_editor_crlf_is_stored_as_lf_unless_the_file_uses_crlf(self):
        def save(path, content):
            self.brain.refresh_from_db()
            response = self.client.post(
                self.url("edit", self.brain.pk),
                {
                    "path": path,
                    "content": content,
                    "message": "Browser edit",
                    "expected_head": str(self.brain.head_revision_id),
                },
            )
            self.assertEqual(response.status_code, 302)
            return BrainService.files(self.ws, self.brain.pk)[path]

        lf_note = note("lf", "public").replace("\r\n", "\n")
        self.assertEqual(save("knowledge/takes/lf.md", lf_note.replace("\n", "\r\n")), lf_note)
        crlf = note("crlf", "public").replace("\r\n", "\n").replace("\n", "\r\n")
        BrainService.edit(
            self.ws,
            self.brain.pk,
            path="knowledge/takes/crlf.md",
            content=crlf,
            message="Windows file",
            expected_head=BrainService.get(self.ws, self.brain.pk).head_revision_id,
        )
        self.assertEqual(save("knowledge/takes/crlf.md", crlf + "More\r\n"), crlf + "More\r\n")

    def test_warning_badges_and_superseded_link(self):
        files = {
            **contract_files(),
            "lenses/bad.md": '---\nvisibility: "broken\n---\n# Bad',
            "projects/stale.md": "# Stale",
            "knowledge/takes/old.md": note("old", extra="status: superseded\nsuperseded_by: new\n"),
            "knowledge/takes/new.md": note("new"),
        }
        BrainService.commit(
            self.ws,
            self.brain.pk,
            files,
            expected_head=self.brain.head_revision_id,
            message="Badges",
        )
        for path, label in (
            ("lenses/bad.md", "Malformed frontmatter"),
            ("projects/stale.md", "Stale project"),
            ("knowledge/takes/old.md", "Superseded"),
        ):
            self.assertContains(
                self.client.get(self.url("detail", self.brain.pk), {"path": path}), label
            )

    def test_settings_validate_cap_and_preserve_content(self):
        head = self.brain.head_revision_id
        response = self.client.post(
            self.url("settings", self.brain.pk), {"name": "Changed", "daily_cost_cap_usd": "-1"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "brain_nonnegative_cap")
        response = self.client.post(
            self.url("settings", self.brain.pk),
            {"name": "Changed", "daily_cost_cap_usd": "0", "reader_model": "provider-specific"},
        )
        self.assertEqual(response.status_code, 302)
        self.brain.refresh_from_db()
        self.assertEqual(self.brain.head_revision_id, head)
        self.assertEqual(self.brain.daily_cost_cap_usd, 0)
        self.assertFalse(self.brain.include_in_backup)

    def test_brain_settings_does_not_activate_global_settings_sidebar(self):
        # Only superusers see the global Settings link at all.
        self.owner.is_superuser = True
        self.owner.save(update_fields=["is_superuser"])
        response = self.client.get(self.url("settings", self.brain.pk))
        anchor = (
            response.content.decode()
            .split('href="' + reverse("cpanel_ws:settings", args=[self.ws.pk]) + '"', 1)[1]
            .split(">", 1)[0]
        )
        self.assertNotIn("font-medium text-ok", anchor)

    def test_grants_upsert_revoke_and_token_workspace_validation(self):
        token = APIToken.objects.create(
            name="Brain read",
            workspace=self.ws,
            scope="api",
            token=APIToken.generate_token(scope="api"),
            permissions=["brain:read"],
        )
        other = APIToken.objects.create(
            name="Foreign",
            workspace=self.other,
            scope="api",
            token=APIToken.generate_token(scope="api"),
            permissions=["brain:read"],
        )
        payload = {"subject_kind": "token", "subject_id": str(token.pk), "max_tier": "public"}
        response = self.client.post(self.url("grants", self.brain.pk), payload)
        self.assertEqual(response.status_code, 302)
        response = self.client.post(
            self.url("grants", self.brain.pk),
            {**payload, "max_tier": "private", "can_propose": "on"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.brain.grants.count(), 1)
        grant = self.brain.grants.get()
        self.assertTrue(grant.can_propose)
        response = self.client.post(
            self.url("grants", self.brain.pk), {**payload, "subject_id": str(other.pk)}
        )
        self.assertContains(response, "Unknown subject in this workspace")
        self.assertEqual(
            self.client.get(self.url("grant_delete", self.brain.pk, grant.pk)).status_code, 405
        )
        self.assertEqual(
            self.client.post(self.url("grant_delete", self.brain.pk, grant.pk)).status_code, 302
        )
        self.assertFalse(self.brain.grants.exists())

    def test_grant_picker_scopes_names_and_never_exposes_token_credentials(self):
        environment = Environment.objects.create(name="Grant picker")
        script = Script.objects.create(name="Cafe reader", workspace=self.ws, environment=environment)
        foreign = Script.objects.create(name="Foreign script", workspace=self.other, environment=environment)
        raw = APIToken.generate_token(scope="api")
        token = APIToken.objects.create(
            name="Cafe connector", workspace=self.ws, scope="api", token=raw,
            permissions=["brain:read"],
        )
        APIToken.objects.create(
            name="Foreign connector", workspace=self.other, scope="api",
            token=APIToken.generate_token(scope="api"), permissions=["brain:read"],
        )
        APIToken.objects.create(name="Legacy token", workspace=self.ws, token=APIToken.generate_token())
        BrainGrant.objects.create(brain=self.brain, subject_kind="script", subject_id=str(script.pk))
        BrainGrant.objects.create(brain=self.brain, subject_kind="token", subject_id=str(token.pk))
        response = self.client.get(self.url("grants", self.brain.pk))
        self.assertContains(response, "Cafe reader")
        self.assertContains(response, "Cafe connector")
        for hidden in (foreign.name, str(foreign.pk), "Foreign connector", "Legacy token", raw, token.token):
            self.assertNotContains(response, hidden)
        self.assertContains(response, '<p class="break-words">Cafe reader</p>', html=True)
        self.assertContains(response, '<p class="break-words">Cafe connector</p>', html=True)

    def test_grant_picker_preserves_selection_on_error_and_rejects_foreign_scripts(self):
        environment = Environment.objects.create(name="Grant validation")
        script = Script.objects.create(name="Reader", workspace=self.ws, environment=environment)
        foreign = Script.objects.create(name="Foreign reader", workspace=self.other, environment=environment)
        payload = {
            "subject_kind": "script", "subject_id": str(script.pk),
            "max_tier": "public", "can_propose": "on",
        }
        response = self.client.post(self.url("grants", self.brain.pk), payload)
        self.assertContains(response, "Proposals require agents-only or private tier")
        self.assertEqual(response.context["form"]["subject_id"].value(), str(script.pk))
        self.assertFalse(self.brain.grants.exists())
        response = self.client.post(
            self.url("grants", self.brain.pk),
            {**payload, "subject_id": str(foreign.pk), "max_tier": "agents-only"},
        )
        self.assertContains(response, "Unknown subject in this workspace")
        self.assertFalse(self.brain.grants.exists())
        response = self.client.post(
            self.url("grants", self.brain.pk), {**payload, "max_tier": "agents-only"},
        )
        self.assertEqual(response.status_code, 302)
        grant = self.brain.grants.get()
        self.assertEqual(grant.subject_id, str(script.pk))
        self.assertTrue(grant.can_propose)

    def test_export_content_and_headers(self):
        response = self.client.get(self.url("export", self.brain.pk))
        self.assertEqual(read_zip(response.content), contract_files())
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_large_raw_replacement_uses_bounded_file_upload(self):
        text = "x" * (3 * 1024 * 1024)
        response = self.client.post(
            self.url("edit", self.brain.pk),
            {
                "path": "raw/large.md",
                "content": "",
                "content_file": SimpleUploadedFile("large.md", text.encode()),
                "expected_head": str(self.brain.head_revision_id),
                "message": "Large raw source",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(BrainService.files(self.ws, self.brain.pk)["raw/large.md"], text)
        response = self.client.get(self.url("edit", self.brain.pk), {"path": "raw/large.md"})
        self.assertContains(response, "Upload a UTF-8 replacement")
        self.assertNotContains(response, "x" * 1000)
        self.brain.refresh_from_db()
        response = self.client.post(
            self.url("edit", self.brain.pk),
            {
                "path": "raw/large.md",
                "content": "",
                "expected_head": str(self.brain.head_revision_id),
                "message": "Accidentally blank editor",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(BrainService.files(self.ws, self.brain.pk)["raw/large.md"], text)
        for path, raw, status in (
            ("raw/binary.md", b"\xff", 400),
            ("identity/oversize.md", b"x" * (1024 * 1024 + 1), 413),
            ("raw/oversize.md", b"x" * (4 * 1024 * 1024 + 1), 413),
        ):
            with self.subTest(path=path):
                response = self.client.post(
                    self.url("edit", self.brain.pk),
                    {
                        "path": path,
                        "content_file": SimpleUploadedFile("input.md", raw),
                        "expected_head": str(self.brain.head_revision_id),
                        "message": "Reject invalid upload",
                    },
                )
                self.assertEqual(response.status_code, status)

    def test_settings_do_not_overwrite_a_concurrently_advanced_head(self):
        from core.views.brains import BrainSettingsForm

        is_valid = BrainSettingsForm.is_valid
        new_head = None

        def advance(form):
            nonlocal new_head
            valid = is_valid(form)
            new_head = BrainService.edit(
                self.ws,
                self.brain.pk,
                "identity/core.md",
                "# Concurrent",
                expected_head=self.brain.head_revision_id,
                message="Concurrent",
            ).pk
            return valid

        with patch.object(BrainSettingsForm, "is_valid", advance):
            response = self.client.post(self.url("settings", self.brain.pk), {"name": "Changed"})
        self.assertEqual(response.status_code, 302)
        self.brain.refresh_from_db()
        self.assertEqual(self.brain.head_revision_id, new_head)


class BrainBackupTests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()
        self.brain = BrainService.create(self.ws, "backup", files=contract_files())
        self.old_revision = self.brain.head_revision_id
        BrainService.edit(
            self.ws,
            self.brain.pk,
            "identity/core.md",
            "# Changed",
            expected_head=self.old_revision,
            message="Backup history",
        )
        BrainGrant.objects.create(
            brain=self.brain, subject_kind="plugin", subject_id="fixture", max_tier="private"
        )
        BrainGrant.objects.create(
            brain=self.brain, subject_kind="token", subject_id="not-portable", max_tier="public"
        )

    def backup(self):
        return BackupService.create_backup(include_runs=False)

    def test_roundtrip_all_revisions_dedup_grants_index_and_optout(self):
        excluded = BrainService.create(self.ws, "excluded", files=contract_files())
        excluded.include_in_backup = False
        excluded.save()
        data = self.backup()
        self.assertEqual(data["backup_metadata"]["version"], "1.14.0")
        self.assertEqual(len(data["brains"]), 1)
        result = BackupService.restore_backup(data)
        self.assertTrue(result["success"], result["errors"])
        self.assertEqual(result["counts"]["brains"], 1)
        brain = Brain.objects.get(pk=self.brain.pk)
        self.assertEqual(brain.created_at, self.brain.created_at)
        self.assertEqual(brain.revisions.count(), 2)
        self.assertEqual(BrainService.files(self.ws, brain.pk, self.old_revision), contract_files())
        self.assertEqual(BrainService.files(self.ws, brain.pk)["identity/core.md"], "# Changed")
        self.assertEqual(list(brain.grants.values_list("subject_kind", flat=True)), ["plugin"])
        self.assertEqual(brain.entities.count(), 2)
        self.assertIn("token grants", " ".join(result["warnings"]))

    def test_corrupt_blob_rejected_before_delete(self):
        data = self.backup()
        data["brains"][0]["blobs"][0]["content"] = "tampered"
        with patch.object(BackupService, "_cleanup_existing_databases") as cleanup:
            result = BackupService.restore_backup(data)
        self.assertFalse(result["success"])
        self.assertTrue(Brain.objects.filter(pk=self.brain.pk).exists())
        cleanup.assert_not_called()

    def test_invalid_manifest_or_ancestry_rejected(self):
        data = self.backup()
        for mutation in ("missing_blob", "traversal", "ancestry"):
            damaged = copy.deepcopy(data)
            row = damaged["brains"][0]
            if mutation == "missing_blob":
                row["revisions"][0]["manifest"]["missing.md"] = "0" * 64
            elif mutation == "traversal":
                row["revisions"][0]["manifest"]["../outside"] = row["blobs"][0]["sha256"]
            else:
                row["revisions"][0]["parent_id"] = row["revisions"][1]["id"]
            with self.subTest(mutation=mutation):
                self.assertFalse(BackupService.restore_backup(damaged)["success"])

    def test_old_backup_without_section_preserves_existing_brain(self):
        data = self.backup()
        del data["brains"]
        data["backup_metadata"]["version"] = "1.9.0"
        result = BackupService.restore_backup(data)
        self.assertTrue(result["success"], result["errors"])
        self.assertTrue(Brain.objects.filter(pk=self.brain.pk).exists())

    def test_export_contains_no_provider_credentials_or_tokens(self):
        rows = export_brains()
        self.assertNotIn("credential", str(rows))
        self.assertNotIn("api_tokens", self.backup())
