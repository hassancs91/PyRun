"""GitHub mirroring keeps canonical writes behind review and workspace gates."""

import copy
from datetime import timedelta
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.api.exceptions import ApiError
from core.brain import approval, feeder, intake, sync, sync_proposal
from core.brain.backup import export_brains, import_brains, validate_brain_backup
from core.brain.github import GitHub, validate_repository
from core.brain.service import BrainService
from core.models import BrainFeed, BrainSync, Secret, User, Workspace, WorkspaceMembership
from core.test_brain_store import archive, contract_files
from core.test_plugin_api import _mock_setup

A, B, C = "a" * 40, "b" * 40, "c" * 40


class SyncTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="Sync workspace")
        self.other = Workspace.objects.create(name="Other sync workspace")
        self.user = User.objects.create_user(username="sync-owner", email="sync@brain.test")
        WorkspaceMembership.objects.create(user=self.user, workspace=self.ws, role="owner")
        self.brain = BrainService.create(self.ws, "mind", files=contract_files())
        self.secret = Secret(workspace=self.ws, key="GITHUB_TEST_TOKEN")
        self.secret.set_value("test-only-sync-secret")
        self.secret.save()
        self.config = sync.configure(self.ws, self.brain.pk, repository="test/brain", branch="main", secret=self.secret)

    def remote(self, files, commit=A):
        client = Mock()
        client.head.return_value = commit
        client.files.return_value = files
        sync.submit(self.ws, self.brain.pk, "pull", user=self.user)
        with patch("core.brain.sync.GitHub", return_value=client):
            sync.run(self.ws.pk, self.brain.pk)
            sync.run(self.ws.pk, self.brain.pk)
        self.assertEqual(client.head.call_count, 1)
        self.config.refresh_from_db()
        self.assertEqual(self.config.status, "idle", self.config.error)
        return self.config.result_feed

    def approve(self, feed):
        return approval.approve(self.ws, self.brain.pk, feed.pk, expected_digest=approval.digest(feed), user=self.user)

    def test_pull_is_proposal_only_and_repeat_deduplicates(self):
        files = contract_files() | {"raw/incoming.md": "Incoming"}
        feed = self.remote(files)
        self.assertEqual((feed.channel, feed.status), ("sync", "proposed"))
        self.assertNotIn("raw/incoming.md", BrainService.files(self.ws, self.brain.pk))
        self.assertEqual(self.remote(files).pk, feed.pk)
        self.assertEqual(BrainFeed.objects.count(), 1)
        result = self.approve(feed)
        self.assertEqual(result.revision.source, "sync")
        self.assertEqual(self.approve(result).revision_id, result.revision_id)
        self.config.refresh_from_db()
        self.assertEqual(self.config.remote_sha, A)

    def test_pull_keeps_its_own_source_id_beside_intake_feeds(self):
        # Intake ids get a suffix when taken (F2.5b-03); sync ids are separate and dedupe on themselves.
        intake.capture(self.ws, self.brain.pk, {"content": "A thought", "source_kind": "thought"},
                       channel="console", principal=f"user:{self.user.pk}")
        files = contract_files() | {"raw/incoming.md": "Incoming"}
        feed = self.remote(files)
        self.assertEqual(feed.source_id, f"sync:{self.config.generation}:{A}")
        self.assertEqual(self.remote(files).pk, feed.pk)

    def test_matching_pull_updates_only_baseline_without_revision(self):
        self.assertIsNone(self.remote(contract_files()))
        self.assertEqual(self.config.remote_sha, A)
        self.assertEqual(self.brain.revisions.count(), 1)
        self.assertIsNone(self.remote({}, A))
        self.assertEqual(self.config.remote_manifest, sync.manifest(contract_files()))

    def test_two_way_changes_delete_and_preserve_local_only_files(self):
        files = contract_files() | {"raw/delete.md": "Delete me"}
        self.approve(self.remote(files))
        self.brain.refresh_from_db()
        BrainService.edit(self.ws, self.brain.pk, "raw/local.md", "Local only", expected_head=self.brain.head_revision_id, message="Local")
        feed = self.remote(contract_files() | {"identity/core.md": "# New identity"}, B)
        review = approval.review(self.ws, self.brain.pk, feed.pk)
        self.assertTrue(review.validation.valid, review.validation.violations)
        self.assertEqual({c["filename"] for c in review.changes}, {"identity/core.md", "raw/delete.md"})
        self.approve(feed)
        result = BrainService.files(self.ws, self.brain.pk)
        self.assertEqual(result["raw/local.md"], "Local only")
        self.assertEqual(result["identity/core.md"], "# New identity")
        self.assertNotIn("raw/delete.md", result)

    def test_concurrent_local_change_requires_new_review(self):
        feed = self.remote(contract_files() | {"raw/shared.md": "Remote"})
        BrainService.edit(self.ws, self.brain.pk, "raw/shared.md", "Local", expected_head=self.brain.head_revision_id, message="Local")
        reviewed = self.approve(feed)
        self.assertTrue(reviewed.needs_review)
        self.assertEqual(reviewed.status, "proposed")
        self.assertEqual(self.approve(reviewed).status, "applied")

    def test_reconfiguration_and_changed_baseline_refuse_stale_approval(self):
        feed = self.remote(contract_files() | {"raw/incoming.md": "Incoming"})
        sync.configure(self.ws, self.brain.pk, repository="test/different", branch="main", secret=self.secret)
        with self.assertRaises(ApiError):
            self.approve(feed)
        self.assertEqual(self.brain.revisions.count(), 1)

    def test_incoming_newer_commit_can_be_repolled_after_older_approval(self):
        first = self.remote(contract_files() | {"raw/a.md": "a"})
        second_files = contract_files() | {"raw/a.md": "a", "raw/b.md": "b"}
        second = self.remote(second_files, B)
        self.approve(first)
        second = self.approve(second)
        self.assertTrue(second.needs_review)
        with self.assertRaises(ApiError):
            self.approve(second)
        newer = self.remote(second_files, B)
        self.assertNotEqual(newer.pk, second.pk)
        self.approve(newer)

    def test_reject_does_not_advance_baseline(self):
        feed = self.remote(contract_files() | {"raw/reject.md": "No"})
        approval.reject(self.ws, self.brain.pk, feed.pk, expected_digest=approval.digest(feed), user=self.user)
        self.assertEqual(self.remote(contract_files() | {"raw/reject.md": "No"}).status, "rejected")
        self.assertEqual(self.config.remote_sha, "")

    def test_sync_uses_exact_files_and_cannot_erase_contract_or_escape_paths(self):
        for rows in (
            [{"path": "CLAUDE.md", "action": "delete", "content": ""}],
            [{"path": "../outside", "action": "create", "content": "x"}],
            [{"path": "raw/bad.md", "action": "create", "content": "(agents-only: hidden"}],
            [{"path": "raw/bad.md", "action": "create", "content": "\ud800"}],
            [{"path": "identity/core.md", "action": "update", "content": "---\ninvalid: [\n---\n"}],
        ):
            self.assertFalse(sync_proposal.validate(contract_files(), {"files": rows}).valid)
        good = {"files": [{"path": "CLAUDE.md", "action": "update", "content": contract_files()["CLAUDE.md"] + "\nOwner contract"}]}
        self.assertTrue(sync_proposal.validate(contract_files(), good).valid)

    def test_review_rejects_duplicate_entity_ids_before_approval(self):
        proposal = {"files": [{"path": f"knowledge/takes/{name}.md", "action": "create",
                              "content": "---\nid: duplicated\n---\nText"} for name in ("one", "two")]}
        self.assertFalse(sync_proposal.validate(contract_files(), proposal).valid)

    def test_sync_shape_rejects_invalid_utf8_in_metadata(self):
        with self.assertRaises(ApiError):
            sync_proposal.shape({"files": [], "summary": "\ud800"})

    def test_export_needs_owner_acknowledgement_and_initial_sha(self):
        for kwargs in ({}, {"user": self.user}, {"user": self.user, "acknowledge": True}):
            with self.assertRaises(ApiError):
                sync.submit(self.ws, self.brain.pk, "export", **kwargs)
        sync.submit(self.ws, self.brain.pk, "export", user=self.user, acknowledge=True, initial_sha=A)
        client = Mock()
        client.export.return_value = B
        with patch("core.brain.sync.GitHub", return_value=client):
            sync.run(self.ws.pk, self.brain.pk)
        self.config.refresh_from_db()
        self.assertEqual((self.config.status, self.config.remote_sha), ("idle", B))
        self.assertEqual(client.export.call_args.args[:2], (contract_files(), A))
        self.assertEqual(self.brain.revisions.count(), 1)

    def test_revoked_owner_cannot_run_queued_export(self):
        sync.submit(self.ws, self.brain.pk, "export", user=self.user, acknowledge=True, initial_sha=A)
        WorkspaceMembership.objects.filter(user=self.user, workspace=self.ws).update(role="member")
        with patch("core.brain.sync.GitHub") as client:
            sync.run(self.ws.pk, self.brain.pk)
        client.return_value.export.assert_not_called()
        self.config.refresh_from_db()
        self.assertEqual(self.config.status, "failed")

    def test_busy_settings_export_pending_and_secret_workspace_guards(self):
        sync.submit(self.ws, self.brain.pk, "pull")
        with self.assertRaises(ApiError):
            sync.configure(self.ws, self.brain.pk, repository="test/brain", branch="main", secret=self.secret)
        BrainSync.objects.filter(pk=self.brain.pk).update(status="idle")
        foreign = Secret.objects.create(workspace=self.other, key="FOREIGN")
        with self.assertRaises(ValidationError):
            sync.configure(self.ws, self.brain.pk, repository="test/brain", branch="main", secret=foreign)
        with self.assertRaises(ApiError):
            sync.submit(self.other, self.brain.pk, "pull")
        self.remote(contract_files() | {"raw/new.md": "Pending"})
        with self.assertRaises(ApiError):
            sync.submit(self.ws, self.brain.pk, "export", user=self.user, acknowledge=True, initial_sha=A)

    def test_sync_cannot_enter_ai_intake_or_retry(self):
        with self.assertRaises(ApiError):
            intake.capture(self.ws, self.brain.pk, {"content": "bad"}, channel="sync", principal="fake")
        feed = self.remote(contract_files() | {"raw/new.md": "Pending"})
        feed.status = "failed"
        feed.save()
        with self.assertRaises(ApiError):
            feeder.retry(self.ws, self.brain.pk, feed.pk)

    def test_failure_sanitizes_unexpected_remote_errors_and_stale_jobs_do_not_retry_export(self):
        sync.submit(self.ws, self.brain.pk, "pull")
        with patch("core.brain.sync.GitHub", side_effect=RuntimeError("SECRET AND URL")):
            sync.run(self.ws.pk, self.brain.pk)
        self.config.refresh_from_db()
        self.assertNotIn("SECRET", self.config.error)
        BrainSync.objects.filter(pk=self.brain.pk).update(status="running", operation="export", updated_at=timezone.now() - timedelta(hours=1))
        with patch("core.brain.sync.enqueue") as enqueue, self.captureOnCommitCallbacks(execute=True):
            sync.reconcile()
        enqueue.assert_not_called()
        self.config.refresh_from_db()
        self.assertEqual(self.config.status, "failed")

    def test_poll_queues_one_job_on_existing_worker(self):
        BrainSync.objects.filter(pk=self.brain.pk).update(poll_enabled=True)
        with patch("core.brain.sync.enqueue") as enqueue, self.captureOnCommitCallbacks(execute=True):
            sync.reconcile()
            sync.reconcile()
        enqueue.assert_called_once()
        self.config.refresh_from_db()
        self.assertEqual((self.config.status, self.config.operation), ("queued", "pull"))

    def test_backup_preserves_baseline_but_never_restarts_jobs_or_polling(self):
        self.remote(contract_files())
        BrainSync.objects.filter(pk=self.brain.pk).update(poll_enabled=True, status="running")
        rows = export_brains()
        validate_brain_backup(rows)
        self.assertNotIn("test-only-sync-secret", str(rows))
        self.brain.delete()
        _, warnings = import_brains(rows, workspace_for=lambda _: self.ws, user_map={}, script_map={}, current_user=self.user)
        restored = BrainSync.objects.get()
        self.assertEqual((restored.remote_sha, restored.secret_id, restored.poll_enabled, restored.status, restored.job), (A, self.secret.pk, False, "idle", {}))
        self.assertTrue(any("polling disabled" in w for w in warnings))
        bad = copy.deepcopy(rows)
        bad[0]["github_sync"]["remote_manifest"] = {"../bad": "a" * 64}
        with self.assertRaises(ApiError):
            validate_brain_backup(bad)

    def test_backup_preserves_authorship_with_the_actual_email_user_map(self):
        applied = self.approve(self.remote(contract_files() | {"raw/incoming.md": "Incoming"}))
        rows = export_brains()
        self.brain.delete()
        import_brains(rows, workspace_for=lambda _: self.ws,
                      user_map={self.user.email: self.user, None: self.user}, script_map={}, current_user=self.user)
        restored = BrainFeed.objects.get(pk=applied.pk)
        self.assertEqual(restored.decided_by_id, self.user.pk)
        self.assertEqual(restored.revision.author_id, self.user.pk)
        self.assertIsNone(restored.brain.revisions.get(number=1).author_id)

    def test_console_scope_permissions_and_no_credentials(self):
        _mock_setup(self)
        self.client.force_login(self.user)
        url = reverse("cpanel_ws:brain_sync", args=[self.ws.pk, self.brain.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "test-only-sync-secret")
        response = self.client.post(url, {"action": "pull"})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        foreign = reverse("cpanel_ws:brain_sync", args=[self.other.pk, self.brain.pk])
        self.assertEqual(self.client.get(foreign).status_code, 404)
        WorkspaceMembership.objects.filter(user=self.user, workspace=self.ws).update(role="member")
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_console_refused_pull_and_export_show_a_message_not_a_500(self):
        """F124: a refused export/pull rendered the unbound settings form's error -> HTTP 500."""
        _mock_setup(self)
        self.client.force_login(self.user)
        url = reverse("cpanel_ws:brain_sync", args=[self.ws.pk, self.brain.pk])
        cases = [
            ({"action": "export", "initial_sha": A}, "An owner/admin must acknowledge"),
            ({"action": "export", "acknowledge": "on"}, "Enter the full commit SHA"),
            ({"action": "export", "acknowledge": "on", "initial_sha": "abc123"}, "Enter the full commit SHA"),
        ]
        for data, text in cases:
            response = self.client.post(url, data)
            self.assertEqual(response.status_code, 200, data)
            self.assertContains(response, text)
        self.config.refresh_from_db()
        self.assertEqual((self.config.status, self.config.operation), ("idle", ""))
        # A pasted upper-case SHA is accepted (GitHub shows lower case, users may not).
        response = self.client.post(url, {"action": "export", "acknowledge": "on", "initial_sha": A.upper()})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.config.refresh_from_db()
        self.assertEqual((self.config.status, self.config.job["expected"]), ("queued", A))
        # Busy and pending-proposal refusals are messages too.
        response = self.client.post(url, {"action": "pull"})
        self.assertContains(response, "A sync job is already queued or running.")
        BrainSync.objects.filter(pk=self.config.pk).update(status="idle")
        self.remote(contract_files() | {"raw/new.md": "Pending"})
        response = self.client.post(url, {"action": "export", "acknowledge": "on", "initial_sha": A})
        self.assertContains(response, "Review or reject incoming sync proposals before exporting.")

    @override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=True)
    def test_http_client_conflict_force_false_and_full_tree(self):
        client = GitHub(self.config)
        with patch.object(client, "json", side_effect=[{"object": {"sha": A}}, {"sha": B}, {"sha": C}, {}]) as api:
            self.assertEqual(client.export(contract_files(), A, "Export"), C)
        self.assertEqual(api.call_args_list[-1].args, ("PATCH", "/git/refs/heads/main", {"sha": C, "force": False}))
        self.assertNotIn("base_tree", api.call_args_list[1].args[2])
        with patch.object(client, "head", return_value=B), self.assertRaises(ApiError):
            client.export(contract_files(), A, "Conflicting")
        client.close()

    def test_http_headers_redirect_policy_and_archive_validation(self):
        client = GitHub(self.config)
        redirect = Mock(status_code=302, headers={"Location": "https://codeload.github.com/test/brain/zip/" + A})
        downloaded = Mock(status_code=200, content=archive({"wrapper/" + p: v for p, v in contract_files().items()}))
        with patch("core.brain.github.is_safe_endpoint_url", return_value=(True, "")), patch.object(client.session, "request", side_effect=[redirect, downloaded]) as request:
            self.assertEqual(client.files(A), contract_files())
        self.assertIn("Authorization", request.call_args_list[0].kwargs["headers"])
        self.assertNotIn("Authorization", request.call_args_list[1].kwargs["headers"])
        for url in ("https://evil.test/archive", "http://codeload.github.com/archive", "https://user@api.github.com/private"):
            with patch("core.brain.github.is_safe_endpoint_url", return_value=(True, "")), self.assertRaises(ApiError):
                client._request("GET", url)
        client.close()

    def test_http_status_body_and_url_are_not_leaked(self):
        client = GitHub(self.config)
        with patch("core.brain.github.is_safe_endpoint_url", return_value=(True, "")), patch.object(client.session, "request", return_value=Mock(status_code=403, text="SECRET")), self.assertRaises(ApiError) as error:
            client.head()
        self.assertNotIn("SECRET", error.exception.message)
        self.assertNotIn("api.github.com", error.exception.message)
        client.close()

    def test_repository_and_branch_validation(self):
        for repository, branch in (("../repo", "main"), ("test/name/path", "main"), ("test/repo", "../main"), ("test/repo", "main.lock"), ("test/repo", "main?token=x")):
            with self.assertRaises(ApiError):
                validate_repository(repository, branch)
        validate_repository("test/repo", "brain/main")
