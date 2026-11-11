"""Queue, validator and human-gated revision transactions across Brain doors."""

import copy
import hashlib
import io
import json
import urllib.parse
import uuid
from datetime import date, timedelta
from decimal import Decimal
from email.message import Message
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import TestCase, SimpleTestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from core.api.exceptions import ApiError
from core.api.registry import registry
from core.brain import approval, feeder, intake, runtime, validator
from core.brain.access import principal_context
from core.brain.backup import export_brains, import_brains, validate_brain_backup
from core.brain.service import BrainService
from core.brain.tools import build_tools
from core.models import (
    AIProvider,
    BrainFeed,
    BrainGrant,
    BrainRevision,
    ClaudeUsage,
    GlobalSettings,
    User,
    Workspace,
    WorkspaceMembership,
)
from core.services.encryption_service import EncryptionService
from core.test_brain_store import contract_files
from core.test_brain_validator import note_file
from core.test_plugin_api import _mock_setup


class FeedTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="Feed workspace")
        self.other = Workspace.objects.create(name="Other feed workspace")
        files = contract_files()
        files["CLAUDE.md"] += "\n## Topic taxonomy\n`ai`\n"
        self.brain = BrainService.create(self.ws, "mind", files=files)
        self.user = User.objects.create_user(username="reviewer", email="reviewer@brain.test")

    def feed(self, files=None):
        return BrainFeed.objects.create(
            brain=self.brain,
            base_revision=self.brain.head_revision,
            source_id="thought-example",
            channel="console",
            principal=f"user:{self.user.pk}",
            raw_payload={"source_kind": "thought", "content": "Source words"},
            proposal={"files": files or [note_file()]},
            status="proposed",
        )

    def approve(self, feed):
        return approval.approve(
            self.ws, self.brain.pk, feed.pk, expected_digest=approval.digest(feed), user=self.user
        )

    def test_capture_queues_after_commit_without_changing_revision(self):
        with (
            patch("core.brain.intake.enqueue") as enqueue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            feed = intake.capture(
                self.ws,
                self.brain.pk,
                {"content": "New source", "source_kind": "thought"},
                channel="console",
                principal="user:1",
            )
            enqueue.assert_not_called()
        enqueue.assert_called_once_with(self.ws.pk, feed.pk)
        self.assertEqual(feed.status, "pending")
        self.assertEqual(self.brain.revisions.count(), 1)

    def test_invalid_input_and_utf8_cap_leave_no_feed(self):
        for params in (
            {},
            {"content": " "},
            {"content": "é" * (intake.MAX_PAYLOAD // 2 + 1)},
            {"content": "ok", "source_kind": "unknown"},
        ):
            with self.assertRaises(ApiError):
                intake.capture(
                    self.ws, self.brain.pk, params, channel="console", principal="user:1"
                )
        self.assertFalse(BrainFeed.objects.exists())

    def test_url_failure_keeps_source_retryable_but_unsafe_never_captures(self):
        with (
            patch("core.brain.intake.safe_url"),
            patch(
                "core.brain.intake.fetch_text",
                side_effect=ApiError("fetch_failed", "Paste content"),
            ),
        ):
            feed = intake.capture(
                self.ws,
                self.brain.pk,
                {"url": "https://example.com"},
                channel="console",
                principal="user:1",
            )
        self.assertEqual(feed.error, "Paste content")
        self.assertIsNone(feed.queued_at)
        with (
            patch("core.brain.intake.is_safe_endpoint_url", return_value=(False, "private")),
            self.assertRaises(ApiError),
        ):
            intake.capture(
                self.ws,
                self.brain.pk,
                {"url": "http://127.0.0.1/"},
                channel="console",
                principal="user:1",
            )
        self.assertEqual(BrainFeed.objects.count(), 1)

    def test_no_propose_grant_and_foreign_workspace_return_unknown(self):
        ctx = principal_context(self.ws, "agent", "test")
        params = {"brain": "mind", "content": "Remember this"}
        for tier, enabled in (("public", True), ("agents-only", False)):
            BrainGrant.objects.update_or_create(
                brain=self.brain,
                subject_kind="agent",
                subject_id="test",
                defaults={"max_tier": tier, "can_propose": enabled},
            )
            with self.assertRaises(ApiError) as caught:
                registry.call("brain.propose-feed", ctx, params)
            self.assertEqual(caught.exception.code, "not_found")
        with self.assertRaises(ApiError):
            approval.get_feed(self.other, self.brain.pk, uuid.uuid4())

    def test_apply_is_atomic_and_idempotent_with_unique_feed_revision(self):
        feed = self.feed()
        first = self.approve(feed)
        second = self.approve(feed)
        self.assertEqual(first.revision_id, second.revision_id)
        self.assertEqual(self.brain.revisions.count(), 2)
        self.assertEqual(BrainRevision.objects.get(feed=feed).author, self.user)
        self.assertEqual(
            self.brain.entities.get(entity_id="take-example").revision_id, first.revision_id
        )

    def test_invalid_proposal_is_stable_and_writes_nothing(self):
        feed = self.feed([note_file(source="")])
        before = (self.brain.blobs.count(), self.brain.revisions.count(), feed.updated_at)
        for _ in range(2):
            verdict = approval.review(self.ws, self.brain.pk, feed.pk).validation
            self.assertEqual([item.rule for item in verdict.violations], [2])
            with self.assertRaises(ApiError):
                self.approve(feed)
        feed.refresh_from_db()
        self.assertEqual(
            before, (self.brain.blobs.count(), self.brain.revisions.count(), feed.updated_at)
        )
        self.assertEqual(feed.retries, 0)

    def test_revision_failure_rolls_back_feed_blobs_and_head(self):
        feed = self.feed()
        with (
            patch("core.brain.service.rebuild", side_effect=ValueError("index failed")),
            self.assertRaises(ValueError),
        ):
            self.approve(feed)
        feed.refresh_from_db()
        self.brain.refresh_from_db()
        self.assertEqual(feed.status, "proposed")
        self.assertEqual(self.brain.head_revision.number, 1)
        self.assertFalse(self.brain.blobs.filter(content=note_file()["content"]).exists())

    def test_disjoint_change_rebases_and_overlapping_change_requires_review(self):
        feed = self.feed()
        BrainService.edit(
            self.ws,
            self.brain.pk,
            "raw/unrelated.md",
            "Other",
            expected_head=self.brain.head_revision_id,
            message="Other",
        )
        self.assertEqual(self.approve(feed).status, "applied")
        self.assertIn("raw/unrelated.md", BrainService.files(self.ws, self.brain.pk))
        self.brain.refresh_from_db()
        update = note_file()
        update["action"] = "update"
        update["content"] += "\nProposed update"
        feed = self.feed([update])
        BrainService.edit(
            self.ws,
            self.brain.pk,
            update["path"],
            note_file()["content"] + "\nConcurrent",
            expected_head=self.brain.head_revision_id,
            message="Concurrent",
        )
        result = self.approve(feed)
        self.assertEqual(result.status, "proposed")
        self.assertTrue(result.needs_review)
        with self.assertRaises(ApiError):
            self.approve(feed)
        self.assertEqual(self.approve(result).status, "applied")

    def test_empty_proposal_and_stale_edit_are_not_approved(self):
        feed = self.feed()
        feed.proposal = {"files": []}
        feed.save()
        with self.assertRaises(ApiError) as caught:
            self.approve(feed)
        self.assertEqual(caught.exception.code, "empty_proposal")
        approval.edit_proposal(
            self.ws,
            self.brain.pk,
            feed.pk,
            {"files": [note_file()]},
            expected_digest=approval.digest(feed),
        )
        with self.assertRaises(ApiError):
            self.approve(feed)

    def test_generated_changes_are_in_diff_and_same_revision(self):
        old = note_file("take-old")
        BrainService.edit(
            self.ws,
            self.brain.pk,
            old["path"],
            old["content"],
            expected_head=self.brain.head_revision_id,
            message="Old",
        )
        self.brain.refresh_from_db()
        new = note_file(description="", topics="[new-tag]")
        new["content"] += "\nA paragraph that can describe this note."
        feed = self.feed([new])
        feed.proposal.update(
            supersedes=[{"old_entity_id": "take-old", "new_entity_id": "take-example"}],
            taxonomy_additions=["new-tag"],
        )
        feed.save()
        review = approval.review(self.ws, self.brain.pk, feed.pk)
        self.assertEqual(
            {c["filename"] for c in review.changes}, {old["path"], new["path"], "CLAUDE.md"}
        )
        result = self.approve(feed)
        self.assertEqual(result.status, "applied")
        self.assertEqual(self.brain.entities.get(entity_id="take-old").status, "superseded")
        self.assertEqual(
            self.brain.entities.get(entity_id="take-example").description,
            "A paragraph that can describe this note.",
        )

    def test_worker_proposes_only_and_claim_prevents_duplicate_runs(self):
        feed = self.feed()
        feed.status = "pending"
        feed.save()
        result = runtime.AgentResult(output={"files": [note_file()]}, cost_usd=0.03)
        with patch("core.brain.feeder.run_agent", return_value=result) as run:
            feeder.extract(self.ws.pk, feed.pk)
            feeder.extract(self.ws.pk, feed.pk)
        run.assert_called_once()
        feed.refresh_from_db()
        self.assertEqual(
            (feed.status, feed.retries, feed.cost_usd), ("proposed", 1, Decimal("0.03"))
        )
        self.assertEqual(self.brain.revisions.count(), 1)

    def test_three_transient_attempts_but_config_failure_does_not_autoretry(self):
        from django_q.models import Schedule

        feed = self.feed()
        feed.status = "pending"
        feed.save()
        with patch(
            "core.brain.feeder.run_agent", side_effect=ApiError("provider_error", "Try again", 502)
        ):
            for _ in range(4):
                feeder.extract(self.ws.pk, feed.pk)
        feed.refresh_from_db()
        self.assertEqual((feed.status, feed.retries), ("failed", 3))
        self.assertEqual(Schedule.objects.filter(func="core.brain.feeder.extract").count(), 2)
        feed.status, feed.retries = "pending", 0
        feed.save()
        with patch("core.brain.feeder.run_agent", side_effect=ApiError("daily_cap", "Cap")):
            feeder.extract(self.ws.pk, feed.pk)
        feed.refresh_from_db()
        self.assertEqual((feed.status, feed.retries), ("failed", 1))

    def test_reject_during_extraction_does_not_resurrect_proposal(self):
        feed = self.feed()
        feed.status = "pending"
        feed.save()

        def reject(*args, **kwargs):
            current = BrainFeed.objects.get(pk=feed.pk)
            approval.reject(
                self.ws,
                self.brain.pk,
                feed.pk,
                expected_digest=approval.digest(current),
                user=self.user,
            )
            return runtime.AgentResult(output={"files": [note_file()]})

        with patch("core.brain.feeder.run_agent", side_effect=reject):
            feeder.extract(self.ws.pk, feed.pk)
        feed.refresh_from_db()
        self.assertEqual(feed.status, "rejected")

    def test_feeder_cannot_read_private_or_unlinked_raw(self):
        files = BrainService.files(self.ws, self.brain.pk)
        files.update(
            {
                "knowledge/takes/private.md": note_file("private", visibility="private")["content"],
                "raw/private.md": "PRIVATE-SENTINEL",
            }
        )
        view = feeder._view(self.brain, files)
        tool = feeder._tools(view)[0]
        for path in ("knowledge/takes/private.md", "raw/private.md", "../outside"):
            self.assertTrue(async_to_sync(tool.handler)({"path": path})["isError"])

    def test_reconcile_interrupted_approval_and_extraction(self):
        feed = self.feed()
        feed.status = "approving"
        feed.save()
        BrainFeed.objects.filter(pk=feed.pk).update(updated_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(feeder.reconcile(), 1)
        feed.refresh_from_db()
        self.assertEqual(feed.status, "proposed")
        self.assertTrue(feed.needs_review)
        feed.status, feed.retries, feed.queued_at = "extracting", 3, timezone.now()
        feed.save()
        BrainFeed.objects.filter(pk=feed.pk).update(updated_at=timezone.now() - timedelta(hours=1))
        feeder.reconcile()
        feed.refresh_from_db()
        self.assertEqual(feed.status, "failed")

    def test_backup_roundtrips_feeds_unique_links_and_never_autoqueues(self):
        applied = self.approve(self.feed())
        pending = self.feed()
        pending.status = "extracting"
        pending.save()
        rows = export_brains()
        validate_brain_backup(rows)
        self.brain.delete()
        with patch("core.brain.intake.enqueue") as enqueue:
            import_brains(
                rows,
                workspace_for=lambda _: self.ws,
                user_map={str(self.user.pk): self.user},
                script_map={},
                current_user=self.user,
            )
        enqueue.assert_not_called()
        restored = BrainFeed.objects.get(pk=applied.pk)
        self.assertEqual(BrainRevision.objects.get(feed=restored).pk, restored.revision_id)
        self.assertEqual(BrainFeed.objects.get(pk=pending.pk).status, "proposed")
        bad = copy.deepcopy(rows)
        bad[0]["feeds"][0]["base_revision_id"] = str(uuid.uuid4())
        with self.assertRaises(ApiError):
            validate_brain_backup(bad)

    def test_inbox_status_tabs_filter_and_count(self):
        """U108: the inbox splits feeds by status with a count per tab."""
        _mock_setup(self)
        WorkspaceMembership.objects.create(workspace=self.ws, user=self.user, role="member")
        self.client.force_login(self.user)
        for status, title in (("proposed", "Review me"), ("failed", "Broke once"), ("failed", "Broke twice"),
                              ("applied", "Done"), ("pending", "Queued one")):
            feed = self.feed()
            BrainFeed.objects.filter(pk=feed.pk).update(status=status, raw_payload={"title": title})
        url = reverse("cpanel_ws:brain_inbox", args=[self.ws.pk, self.brain.pk])
        html = self.client.get(url).content.decode()
        for title in ("Review me", "Broke once", "Done", "Queued one"):
            self.assertIn(title, html)
        self.assertRegex(html, r'aria-current="page">All<span[^>]*>5</span>')
        self.assertRegex(html, r'>Failed<span[^>]*>2</span>')
        self.assertRegex(html, r'>In progress<span[^>]*>1</span>')
        html = self.client.get(url + "?status=failed").content.decode()
        self.assertIn("Broke once", html)
        self.assertIn("Broke twice", html)
        self.assertNotIn("Review me", html)
        self.assertNotIn("Queued one", html)
        self.assertRegex(html, r'aria-current="page">Failed<span')
        html = self.client.get(url + "?status=review").content.decode()
        self.assertIn("Review me", html)
        self.assertNotIn("Broke once", html)
        html = self.client.get(url + "?status=rejected").content.decode()
        self.assertIn("No sources with this status.", html)
        # an unknown tab falls back to every feed
        html = self.client.get(url + "?status=bogus").content.decode()
        self.assertIn("Broke once", html)
        self.assertIn("Review me", html)

    def test_console_members_propose_edit_but_only_owner_decides(self):
        _mock_setup(self)
        WorkspaceMembership.objects.create(workspace=self.ws, user=self.user, role="member")
        self.client.force_login(self.user)
        feed = self.feed()
        url = reverse("cpanel_ws:brain_feed", args=[self.ws.pk, self.brain.pk, feed.pk])
        for route in (
            url,
            reverse("cpanel_ws:brain_inbox", args=[self.ws.pk, self.brain.pk]),
            reverse("cpanel_ws:brain_feed_create", args=[self.ws.pk, self.brain.pk]),
        ):
            self.assertEqual(self.client.get(route).status_code, 200)
        self.assertEqual(
            self.client.post(
                url, {"action": "approve", "expected_digest": approval.digest(feed)}
            ).status_code,
            403,
        )
        response = self.client.post(
            url,
            {
                "action": "edit",
                "expected_digest": approval.digest(feed),
                "proposal": json.dumps(feed.proposal),
            },
        )
        self.assertEqual(response.status_code, 302)
        feed.refresh_from_db()
        self.assertTrue(feed.proposal_edited)
        foreign = reverse("cpanel_ws:brain_feed", args=[self.other.pk, self.brain.pk, feed.pk])
        self.assertEqual(self.client.get(foreign).status_code, 404)

    def test_remember_tool_requires_current_propose_grant(self):
        grant = BrainGrant.objects.create(
            brain=self.brain, subject_kind="agent", subject_id="pyai", max_tier="agents-only"
        )
        self.assertNotIn("remember", [t.name for t in build_tools(self.ws)])
        grant.can_propose = True
        grant.save()
        tool = next(t for t in build_tools(self.ws) if t.name == "remember")
        result = async_to_sync(tool.handler)(
            {"brain": "mind", "content": "Remember this", "source_kind": "thought"}
        )
        self.assertNotIn("isError", result)
        self.assertEqual(BrainFeed.objects.get().channel, "agent")
        grant.delete()
        self.assertTrue(
            async_to_sync(tool.handler)({"brain": "mind", "content": "Denied"})["isError"]
        )

    def test_grant_revoked_during_fetch_does_not_capture(self):
        grant = BrainGrant.objects.create(
            brain=self.brain,
            subject_kind="agent",
            subject_id="test",
            max_tier="agents-only",
            can_propose=True,
        )

        def fetch(*args, **kwargs):
            grant.delete()
            return "Captured after revocation"

        with (
            patch("core.brain.intake.safe_url"),
            patch("core.brain.intake.fetch_text", side_effect=fetch),
            self.assertRaises(ApiError),
        ):
            registry.call(
                "brain.propose-feed",
                principal_context(self.ws, "agent", "test"),
                {"brain": "mind", "url": "https://example.com"},
            )
        self.assertFalse(BrainFeed.objects.exists())

    def test_call_expiring_during_fetch_does_not_capture(self):
        import time

        with (
            patch("core.brain.intake.safe_url"),
            patch("core.brain.intake.fetch_text", return_value="Text"),
            self.assertRaises(ApiError),
        ):
            intake.capture(
                self.ws,
                self.brain.pk,
                {"url": "https://example.com"},
                channel="console",
                principal="user:1",
                deadline=time.monotonic() - 1,
            )
        self.assertFalse(BrainFeed.objects.exists())

    def test_telegram_remember_uses_channel_specific_grant(self):
        from core.models import Channel
        from core.services.channels.base import InboundMessage
        from core.services.channels.handlers import _pyai_handler

        channel = Channel.objects.create(
            workspace=self.ws, name="Bot", provider="telegram", inbound_handler="pyai"
        )
        message = InboundMessage(
            str(channel.pk), "telegram", "/remember mind A thought", {}, {"chat_id": "test"}, {}
        )
        BrainGrant.objects.create(
            brain=self.brain,
            subject_kind="agent",
            subject_id="pyai",
            max_tier="agents-only",
            can_propose=True,
        )
        self.assertIn("Unknown", _pyai_handler(channel, message).text)
        BrainGrant.objects.create(
            brain=self.brain,
            subject_kind="agent",
            subject_id=f"telegram:{channel.pk}",
            max_tier="agents-only",
            can_propose=True,
        )
        self.assertIn("Captured", _pyai_handler(channel, message).text)
        self.assertEqual(BrainFeed.objects.get().channel, "telegram")


class SourceIdTests(TestCase):
    """A source id is the provenance notes cite, so no two feeds in a brain share one (F2.5b-03)."""

    UID = uuid.UUID("3f9a3f9a-3f9a-4f9a-8f9a-3f9a3f9a3f9a")

    def setUp(self):
        self.ws = Workspace.objects.create(name="Source id workspace")
        self.brain = BrainService.create(self.ws, "mind", files=contract_files())

    def capture(self, brain=None, **params):
        with (
            patch("core.brain.intake.safe_url"),
            patch.object(intake.timezone, "localdate", return_value=date(2026, 11, 9)),
        ):
            return intake.capture(
                self.ws, (brain or self.brain).pk, params, channel="api", principal="token:1"
            )

    def existing(self, source_id, brain=None):
        brain = brain or self.brain
        return BrainFeed.objects.create(
            brain=brain, base_revision=brain.head_revision, source_id=source_id,
            channel="console", principal="user:1", raw_payload={"content": "Earlier"},
        )

    def test_untitled_thoughts_get_distinct_ids(self):
        BrainGrant.objects.create(
            brain=self.brain, subject_kind="agent", subject_id="test",
            max_tier="agents-only", can_propose=True,
        )
        ctx = principal_context(self.ws, "agent", "test")
        params = {"brain": "mind", "content": "Remember this", "source_kind": "thought"}
        with patch.object(intake.timezone, "localdate", return_value=date(2026, 11, 9)):
            first = registry.call("brain.propose-feed", ctx, params)
            second = registry.call("brain.propose-feed", ctx, params)
        digest = hashlib.sha256(b"Remember this").hexdigest()[:6]
        self.assertEqual(first["source_id"], f"thought-2026-11-{digest}")
        self.assertEqual(
            second["source_id"], f"thought-2026-11-{digest}-{second['feed_id'][:4]}"
        )
        # Content with no ASCII words is hashed the same way.
        ids = [
            self.capture(content=text, source_kind="thought").source_id
            for text in ("تذكر هذا", "!!!")
        ]
        self.assertEqual(
            ids[0], f"thought-2026-11-{hashlib.sha256('تذكر هذا'.encode()).hexdigest()[:6]}"
        )
        self.assertNotEqual(ids[0], ids[1])

    def test_untitled_sources_are_named_by_a_content_hash_never_their_words(self):
        content = "  Why small teams ship faster than big ones.\nMore"
        thought = self.capture(content=content, source_kind="thought")
        self.assertRegex(thought.source_id, r"^thought-2026-11-[0-9a-f]{6}$")
        self.assertEqual(
            thought.source_id, f"thought-2026-11-{hashlib.sha256(content.encode()).hexdigest()[:6]}"
        )
        self.assertNotIn("why", thought.source_id)
        # A URL whose path is "/" names nothing, so the hash decides.
        page = self.capture(
            url="https://example.com/", content="Example Domain page text", source_kind="article"
        )
        self.assertRegex(page.source_id, r"^article-2026-11-[0-9a-f]{6}$")
        self.assertNotIn("example", page.source_id)
        # A title that slugifies to nothing names nothing either.
        untitled = self.capture(title="؟؟", content="Plain words here")
        self.assertRegex(untitled.source_id, r"^doc-2026-11-[0-9a-f]{6}$")
        # An id stored before (words-based or "source") is never renamed.
        self.existing("thought-2026-11-source")
        kept = BrainFeed.objects.get(brain=self.brain, source_id="thought-2026-11-source")
        self.capture(content="Something new", source_kind="thought")
        kept.refresh_from_db()
        self.assertEqual(kept.source_id, "thought-2026-11-source")

    def test_titled_and_url_sources_keep_the_previous_format(self):
        for title, url, kind in (
            ("Hello, World!", "", "doc"),
            ("A very long title that keeps going " * 4, "https://example.com/x", "article"),
            ("", "https://example.com/blog/My Post?x=1", "article"),
            ("Ünïcode Títle", "", "video"),
            ("a" * 59 + " b", "", "thread"),
        ):
            legacy = (
                f"{kind}-2026-11-"
                f"{slugify(title or urllib.parse.urlsplit(url).path)[:60] or 'source'}"
            )
            feed = self.capture(title=title, url=url, content="Body", source_kind=kind)
            self.assertEqual(feed.source_id, legacy)

    def test_a_taken_id_gets_a_suffix_that_grows_on_a_clash(self):
        first = self.capture(title="Weekly notes", content="One")
        self.assertEqual(first.source_id, "doc-2026-11-weekly-notes")
        # Another brain's ids don't count.
        other = BrainService.create(self.ws, "other", files=contract_files())
        self.assertEqual(
            self.capture(brain=other, title="Weekly notes", content="Two").source_id,
            "doc-2026-11-weekly-notes",
        )
        self.existing("doc-2026-11-weekly-notes-3f9a")
        with patch("core.brain.intake.uuid.uuid4", return_value=self.UID):
            second = self.capture(title="Weekly notes", content="Three")
        self.assertEqual(second.pk, self.UID)
        self.assertEqual(second.source_id, "doc-2026-11-weekly-notes-3f9a3f9a")

    def test_ids_fit_the_column_and_never_lose_the_suffix(self):
        title = "Long title " + "x" * 400
        first = self.capture(title=title, content="Body", source_kind="conversation")
        second = self.capture(title=title, content="Body", source_kind="conversation")
        self.assertEqual(len(first.source_id), len("conversation-2026-11-") + 60)
        self.assertEqual(second.source_id, f"{first.source_id}-{second.pk.hex[:4]}")
        limit = BrainFeed._meta.get_field("source_id").max_length
        self.existing("a" * limit)
        self.assertEqual(
            intake.unique_source_id(self.brain, "a" * limit, self.UID), "a" * (limit - 5) + "-3f9a"
        )
        # A slug cut at 60 can end in "-"; the suffix doesn't double it.
        dashed = self.capture(title="a" * 59 + " b", content="Body")
        self.assertTrue(dashed.source_id.endswith("a-"))
        again = self.capture(title="a" * 59 + " b", content="Body")
        self.assertEqual(again.source_id, f"{dashed.source_id}{again.pk.hex[:4]}")


class RuntimeTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="AI brain")
        self.brain = BrainService.create(self.ws, "ai", files=contract_files())
        self.provider = AIProvider.objects.create(
            name="Test",
            provider_type="anthropic",
            credential_encrypted=EncryptionService.encrypt("e2e-only-dummy"),
            default_model="provider-model",
        )
        settings = GlobalSettings.get_settings()
        settings.claude_enabled, settings.active_ai_provider = True, self.provider
        settings.save()

    def test_profile_override_model_and_fail_closed_daily_cap(self):
        self.brain.daily_cost_cap_usd = Decimal("0.10")
        self.brain.feeder_model = "custom-model"
        self.brain.save()
        usage, env, model, budget = runtime.prepare(self.ws, self.brain.pk, "feeder")
        self.assertEqual((model, budget), ("custom-model", 0.1))
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "")
        with self.assertRaises(ApiError) as caught:
            runtime.prepare(self.ws, self.brain.pk, "feeder")
        self.assertEqual(caught.exception.code, "daily_cap")
        usage.cost_usd = 0.1
        usage.save()
        with self.assertRaises(ApiError):
            runtime.prepare(self.ws, self.brain.pk, "feeder")
        self.assertEqual(ClaudeUsage.objects.count(), 1)

    def test_budget_exhausted_mid_call_reports_the_daily_cap(self):
        import claude_agent_sdk as sdk

        self.brain.daily_cost_cap_usd = Decimal("0.01")
        self.brain.save()

        async def over_budget(*, prompt, options):
            self.assertEqual(options.max_budget_usd, 0.01)
            yield sdk.ResultMessage(
                subtype="error_max_budget_usd",
                duration_ms=900,
                duration_api_ms=800,
                is_error=True,
                num_turns=1,
                session_id="s",
                total_cost_usd=0.012,
                usage={"input_tokens": 0, "output_tokens": 0},
                errors=["Reached maximum budget ($0.01)"],
            )

        with (
            patch("claude_agent_sdk.query", new=over_budget),
            self.assertRaises(ApiError) as caught,
        ):
            runtime.run_agent(
                self.ws,
                self.brain.pk,
                kind="reader",
                prompt="test",
                system="test",
                schema={"type": "object"},
            )
        self.assertEqual(caught.exception.code, "daily_cap")
        self.assertIn("daily AI cap", caught.exception.message)
        self.assertEqual(ClaudeUsage.objects.get(brain_id=self.brain.pk).cost_usd, 0.012)

    def test_timeout_records_partial_tokens_with_unknown_cost(self):
        async def timeout(prompt, options, result, partial, seconds):
            partial.observe({"type": "message_start", "message": {"usage": {"input_tokens": 42}}})
            partial.observe({"type": "message_delta", "usage": {"output_tokens": 6}})
            partial.observe({"type": "message_delta", "usage": {"output_tokens": 9}})
            raise TimeoutError

        with patch("core.brain.runtime._drive", side_effect=timeout), self.assertRaises(ApiError):
            runtime.run_agent(
                self.ws,
                self.brain.pk,
                kind="feeder",
                prompt="test",
                system="test",
                schema={"type": "object"},
            )
        usage = ClaudeUsage.objects.get(brain_id=self.brain.pk)
        self.assertEqual((usage.input_tokens, usage.output_tokens), (42, 9))
        self.assertIsNone(usage.cost_usd)


class FetchTests(SimpleTestCase):
    def response(self, body=b"Hello", content_type="text/plain"):
        result = io.BytesIO(body)
        result.headers = Message()
        result.headers["Content-Type"] = content_type
        return result

    def test_html_strips_scripts_and_head_preserves_inline_text(self):
        response = self.response(
            b"<head>hidden</head><p>Hello <b>world</b>.</p><script>secret</script><p>Next</p>",
            "text/html",
        )
        with (
            patch("core.brain.intake.safe_url"),
            patch("urllib.request.OpenerDirector.open", return_value=response),
        ):
            self.assertEqual(intake.fetch_text("https://example.com"), "Hello world.\nNext")

    def test_cap_nontext_and_redirect_are_bounded(self):
        import urllib.error

        for response in (
            self.response(b"x" * (intake.MAX_PAYLOAD + 1)),
            self.response(b"image", "image/png"),
        ):
            with (
                patch("core.brain.intake.safe_url"),
                patch("urllib.request.OpenerDirector.open", return_value=response),
                self.assertRaises(ApiError),
            ):
                intake.fetch_text("https://example.com")
        headers = Message()
        headers["Location"] = "http://127.0.0.1/private"
        redirect = urllib.error.HTTPError("https://example.com", 302, "Redirect", headers, None)
        with (
            patch(
                "core.brain.intake.safe_url", side_effect=[None, ApiError("unsafe_url", "Blocked")]
            ) as safe,
            patch("urllib.request.OpenerDirector.open", side_effect=redirect),
            self.assertRaises(ApiError),
        ):
            intake.fetch_text("https://example.com")
        self.assertEqual(safe.call_count, 2)


class AdditionalValidatorTests(SimpleTestCase):
    def test_non_utf8_proposal_is_invalid_instead_of_crashing(self):
        result = validator.validate(
            {"files": [{"path": "raw/source.md", "action": "create", "content": "\ud800"}]},
            validator.ValidationContext(),
        )
        self.assertFalse(result.valid)

    def test_url_budget_reduces_each_socket_read_timeout(self):
        from unittest.mock import Mock

        response = io.BytesIO(b"Hello")
        response.headers = Message()
        response.headers["Content-Type"] = "text/plain"
        response.fp = Mock()
        with (
            patch("core.brain.intake.safe_url"),
            patch("urllib.request.OpenerDirector.open", return_value=response),
            patch("core.brain.intake.time.monotonic", side_effect=[0, 0, 14, 14, 14, 14]),
        ):
            intake.fetch_text("https://example.com")
        response.fp.raw._sock.settimeout.assert_called_with(1)

    def test_description_visibility_and_raw_update_rules(self):
        proposal = {"files": [note_file(description="", visibility="public")]}
        result = validator.validate(proposal, validator.ValidationContext(taxonomy={"ai"}))
        self.assertEqual({v.rule for v in result.violations}, {9, 11})
        proposal["files"][0]["content"] += "\nA useful paragraph."
        proposal["issues"] = ["Source is a published article; public is intentional."]
        self.assertTrue(
            validator.validate(proposal, validator.ValidationContext(taxonomy={"ai"})).valid
        )
        proposal = {"files": [{"path": "raw/missing.md", "action": "update", "content": "text"}]}
        self.assertFalse(validator.validate(proposal, validator.ValidationContext()).valid)

    def test_all_source_blockable_scripts_and_template_prose(self):
        samples = dict(
            arabic="ع",
            hebrew="ש",
            cyrillic="Ж",
            greek="Ω",
            devanagari="अ",
            thai="ก",
            han="字",
            hiragana="あ",
            katakana="ア",
            hangul="한",
        )
        for script, text in samples.items():
            proposal = {"files": [{"path": "raw/source.md", "action": "create", "content": text}]}
            self.assertTrue(validator.validate(proposal, validator.ValidationContext()).valid)
            self.assertFalse(
                validator.validate(
                    proposal, validator.ValidationContext(blocked_scripts=(script,))
                ).valid
            )
        from pathlib import Path

        template = Path("core/brain/template/CLAUDE.md").read_text(encoding="utf-8")
        self.assertEqual(validator.blocked_scripts_from_claude_md(template), ((), ()))
