"""
WhatsApp media download and rich outbound (2.5 Stage 8, ticket T37).

What this pins, roughly in order of how much it would hurt to get wrong:

- "Download media" is off by default: an inbound attachment then stays
  metadata only, with no call for the file and nothing written to disk.
- On, the worker fetches the file while it dispatches the message (the link
  from ``GET /{media-id}``, then the file with the same token, no redirects,
  through the SSRF guard), within the channel's size cap, and the run's event
  lists it with kind, mime, caption and a ``path`` (a run-scoped file the
  script reads, removed when the run ends) or a ``url`` (the storage seam,
  when a private assets connection is set). A failure is the item's
  ``error``; the run still starts.
- Leftovers (a skipped run, a crash) are removed: right away for a skipped
  run, else by the daily housekeeping; stored files after 30 days.
- Py AI never gets a download, only a line naming the attachment.
- Outbound: a file by URL and reply buttons / lists go through the ledger
  (kept on the row, so a retry sends the same thing), inside the 24-hour
  window only; scripts send them with ``pyrunner_notify.send/reply``.

Every Graph API call and download goes to ``FakeWhatsApp``
(core/test_whatsapp_channel.py); nothing reaches a Meta host.
"""

import json
import os
import shutil
import sys
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Channel, ChannelMessage, Run, User
from core.services.channels import OutboundMessage, ledger
from core.services.channels import media as channel_media
from core.services.channels.inbound import dispatch_inbound
from core.services.channels.whatsapp import (
    BAD_TOKEN_MESSAGE,
    OUT_OF_WINDOW_RICH_MESSAGE,
    clean_interactive,
    clean_media,
)
from core.services.datastore_token import mint_datastore_token
from core.test_channel_ledger import _Resp
from core.test_whatsapp_channel import ANN, MEDIA_HOST, TOKEN, _Base, _File, _message, _payload

# The real lookup, kept before any test patches it.
_real_storage = channel_media._storage
JPEG = b"\xff\xd8\xff\xe0" + b"pixels" * 10
PDF = b"%PDF-1.7 invoice"


class _MediaBase(_Base):
    def setUp(self):
        super().setUp()
        self.workdir = tempfile.mkdtemp(prefix="t37-")
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)
        override = override_settings(SCRIPTS_WORKDIR=self.workdir)
        override.enable()
        self.addCleanup(override.disable)
        # No assets storage connection unless a test sets one.
        storage = mock.patch("core.services.channels.media._storage", return_value=None)
        self.storage = storage.start()
        self.addCleanup(storage.stop)
        self.ch.inbound_enabled = True
        self.ch.inbound_handler = "script"
        self.ch.inbound_target_id = self.script.id
        self.ch.save()

    def _media_on(self, **fields):
        self.ch.download_media = True
        for name, value in fields.items():
            setattr(self.ch, name, value)
        self.ch.save()

    def _dispatch(self, message):
        with mock.patch("core.tasks.queue_script_run") as queue:
            dispatch_inbound(str(self.ch.id), _payload(message))
        self.assertEqual(queue.call_count, 1)
        return Run.objects.get(script=self.script)

    def _media_dirs(self):
        return list(Path(self.workdir).glob("*/.pyrun-media-*"))

    def _downloads(self):
        return [c for c in self.wa.calls if c[0] == "DOWNLOAD"]


class SettingTests(_MediaBase):
    def test_off_by_default_with_a_16_mb_cap(self):
        ch = Channel.objects.get(pk=self.ch.pk)
        self.assertEqual((ch.download_media, ch.media_max_mb), (False, 16))

    def test_the_inbound_form_saves_the_setting_for_whatsapp_only(self):
        admin = User.objects.create_superuser(username="a", email="a@x.io", password="pw")
        self.client.force_login(admin)
        html = self.client.get(reverse("cpanel:channel_edit", args=[self.ch.pk])).content.decode()
        self.assertIn('name="download_media"', html)
        self.assertIn("Largest attachment (MB)", html)
        data = {"inbound_enabled": "on", "inbound_handler": "script", "inbound_target_id": str(self.script.id),
                "inbound_access": "approval", "daily_reply_cap": "0", "download_media": "on", "media_max_mb": "5"}
        self.client.post(reverse("cpanel:channel_inbound", args=[self.ch.pk]), data)
        self.ch.refresh_from_db()
        self.assertEqual((self.ch.download_media, self.ch.media_max_mb), (True, 5))
        # Out of range: refused, nothing changes.
        resp = self.client.post(
            reverse("cpanel:channel_inbound", args=[self.ch.pk]), {**data, "media_max_mb": "101"}, follow=True
        )
        self.ch.refresh_from_db()
        self.assertEqual(self.ch.media_max_mb, 5)
        # The message names the field by its label, not its form key (2.5 test pass 8).
        shown = [str(m) for m in resp.context["messages"]]
        self.assertIn("Largest attachment (MB): Ensure this value is less than or equal to 100.", shown)
        self.assertFalse([m for m in shown if "media_max_mb" in m])
        # Unticked: off again, the cap kept.
        self.client.post(reverse("cpanel:channel_inbound", args=[self.ch.pk]),
                         {k: v for k, v in data.items() if k not in ("download_media", "media_max_mb")})
        self.ch.refresh_from_db()
        self.assertEqual((self.ch.download_media, self.ch.media_max_mb), (False, 5))

        tg = Channel(workspace=self.ws, provider="telegram", name="TG")
        tg.set_credentials({"bot_token": "1:2"}, identity="1:2")
        tg.save()
        html = self.client.get(reverse("cpanel:channel_edit", args=[tg.pk])).content.decode()
        self.assertNotIn('name="download_media"', html)


class ToggleOffTests(_MediaBase):
    def test_metadata_only_no_download_no_disk(self):
        self.wa.add_media("555", JPEG)
        run = self._dispatch(_message("image", id="555", mime_type="image/jpeg", caption="receipt"))
        self.assertEqual(run.event["media"], [{"kind": "image", "id": "555", "mime": "image/jpeg", "caption": "receipt"}])
        self.assertEqual([c for c in self.wa.calls if c[0] != "POST"], [])  # no media lookup, no download
        self.assertEqual(self._media_dirs(), [])
        self.assertEqual(list(Path(self.workdir).iterdir()), [])

    def test_a_py_ai_channel_never_downloads(self):
        from core.services.pyai import PyAIService
        from core.services.pyai.runtime import PyAIResult

        self._media_on(inbound_handler="pyai")
        self.wa.add_media("555", PDF, "application/pdf")
        with mock.patch.object(PyAIService, "is_available", return_value=True), \
                mock.patch.object(PyAIService, "respond", return_value=PyAIResult(text="ok")) as respond:
            dispatch_inbound(str(self.ch.id), _payload(_message(
                "document", id="555", mime_type="application/pdf", filename="inv.pdf", caption="Is this paid?")))
        self.assertEqual(respond.call_args.args[0], 'Is this paid?\n\n[The sender attached: document "inv.pdf" '
                                                    "(application/pdf). You can't open attachments.]")
        self.assertEqual(self._downloads(), [])
        self.assertEqual(self._media_dirs(), [])


class DownloadTests(_MediaBase):
    def test_the_file_reaches_the_run_as_a_path(self):
        self._media_on()
        self.wa.add_media("555", JPEG)
        run = self._dispatch(_message("image", id="555", mime_type="image/jpeg", caption="receipt"))
        item = run.event["media"][0]
        folder = Path(self.workdir) / str(self.script.id) / f".pyrun-media-{run.id.hex}"
        self.assertEqual(item, {"kind": "image", "id": "555", "mime": "image/jpeg", "caption": "receipt",
                                "size": len(JPEG), "path": str(folder / "555.jpg")})
        self.assertEqual(Path(item["path"]).read_bytes(), JPEG)
        # The link comes from GET /{media-id}; the file is fetched with the same token, unredirected.
        lookup = [c for c in self.wa.calls if c[0] == "GET"]
        self.assertEqual((lookup[0][1], lookup[0][3]), ("555", {"Authorization": f"Bearer {TOKEN}"}))
        (_, url, kwargs, headers), = self._downloads()
        self.assertTrue(url.startswith(MEDIA_HOST + "555"))
        self.assertEqual(headers, {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual((kwargs["stream"], kwargs["allow_redirects"]), (True, False))

    def test_a_document_keeps_a_safe_version_of_its_name(self):
        self._media_on()
        self.wa.add_media("777", PDF, "application/pdf")
        run = self._dispatch(_message("document", id="777", mime_type="application/pdf", filename="../../my inv?.pdf"))
        self.assertEqual(Path(run.event["media"][0]["path"]).name, "my_inv_.pdf")
        self.assertEqual(Path(run.event["media"][0]["path"]).parent.parent, Path(self.workdir) / str(self.script.id))

    def test_files_over_the_cap_are_not_downloaded(self):
        self._media_on(media_max_mb=1)
        self.wa.add_media("1", b"x", file_size=2 * 1024 * 1024)
        run = self._dispatch(_message("video", id="1", mime_type="video/mp4"))
        item = run.event["media"][0]
        self.assertEqual(item["error"], "Not downloaded: the file is 2.0 MB, over the 1 MB cap.")
        self.assertNotIn("path", item)
        self.assertEqual(self._downloads(), [])
        # WhatsApp gave a size under the cap but sends more: stopped mid-way, nothing written.
        Run.objects.all().delete()
        self.wa.add_media("2", b"y" * (1024 * 1024 + 1), file_size=10)
        item = self._dispatch(_message("audio", id="2", mime_type="audio/ogg", wamid="wamid.IN2")).event["media"][0]
        self.assertEqual(item["error"], "Not downloaded: the file is over the 1 MB cap.")
        self.assertEqual(self._media_dirs(), [])

    def test_failures_keep_the_metadata_and_the_run(self):
        self._media_on()
        self.wa.add_media("1", JPEG)
        self.wa.reply(None).reply(None).reply(_File(404))  # read receipt, link lookup, then the expired link
        run = self._dispatch(_message("image", id="1", mime_type="image/jpeg", caption="hi"))
        item = run.event["media"][0]
        self.assertIn("answered HTTP 404", item["error"])
        self.assertEqual((item["caption"], run.event["text"]), ("hi", "hi"))
        Run.objects.all().delete()
        self.wa.reply(None).error(401, 190, f"Error validating access token {TOKEN}")
        item = self._dispatch(_message("image", id="1", mime_type="image/jpeg", wamid="wamid.IN2")).event["media"][0]
        self.assertEqual(item["error"], BAD_TOKEN_MESSAGE)
        self.assertNotIn(TOKEN, json.dumps(item))

    @override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=False)
    def test_a_link_the_ssrf_guard_refuses_is_never_fetched(self):
        self._media_on()
        self.wa.reply(None).reply(_Resp(200, {"url": "https://169.254.169.254/latest/meta-data", "mime_type": "image/png"}))
        item = self._dispatch(_message("image", id="1", mime_type="image/png")).event["media"][0]
        self.assertTrue(item["error"].startswith("Not downloaded: the download link was refused"), item)
        self.assertEqual(self._downloads(), [])

    def test_a_skipped_run_leaves_no_files(self):
        self._media_on()
        self.wa.add_media("555", JPEG)

        def skip(run, webhook_data=None):
            run.status = Run.Status.CANCELLED
            run.cancel_reason = Run.CancelReason.CONCURRENCY_SKIP
            run.save()

        with mock.patch("core.tasks.queue_script_run", side_effect=skip):
            dispatch_inbound(str(self.ch.id), _payload(_message("image", id="555", mime_type="image/jpeg")))
        run = Run.objects.get(script=self.script)
        self.assertTrue(run.is_skipped)
        self.assertIn("path", run.event["media"][0])
        self.assertEqual(self._media_dirs(), [])


class StorageTests(_MediaBase):
    class _Storage:
        def __init__(self, fail=False):
            self.objects = {}
            self.fail = fail

        def put(self, key, data, content_type=""):
            from core.plugins.api import StorageError

            if self.fail:
                raise StorageError("bucket says no")
            self.objects[key] = (data, content_type)
            return key

        def url(self, key, expires_in=3600):
            return f"https://bucket.example/{key}?expires={expires_in}"

        def list(self, prefix=""):
            return [{"key": k} for k in self.objects if k.startswith(prefix)]

        def delete(self, key):
            return self.objects.pop(key, None) is not None

    def test_a_private_assets_connection_stores_it_and_the_run_gets_a_link(self):
        self._media_on()
        store = self._Storage()
        self.storage.return_value = store
        self.wa.add_media("555", JPEG)
        run = self._dispatch(_message("image", id="555", mime_type="image/jpeg"))
        key = f"whatsapp/{self.ch.id.hex}/{run.id.hex}/555.jpg"
        self.assertEqual(store.objects, {key: (JPEG, "image/jpeg")})
        item = run.event["media"][0]
        self.assertEqual(item["url"], f"https://bucket.example/{key}?expires={24 * 3600}")
        self.assertNotIn("path", item)
        self.assertEqual(self._media_dirs(), [])
        # A skipped run's stored file goes too.
        channel_media.discard(self.ch, self.script.id, run.id, run.event["media"])
        self.assertEqual(store.objects, {})

    def test_a_failing_bucket_falls_back_to_a_file(self):
        self._media_on()
        self.storage.return_value = self._Storage(fail=True)
        self.wa.add_media("555", JPEG)
        with self.assertLogs("core.services.channels.media", "WARNING"):
            run = self._dispatch(_message("image", id="555", mime_type="image/jpeg"))
        self.assertEqual(Path(run.event["media"][0]["path"]).read_bytes(), JPEG)

    def test_which_connection_is_used(self):
        with mock.patch("core.services.s3_service.S3Service.for_assets", return_value=None):
            self.assertIsNone(_real_storage(self.ch))
        public = mock.Mock(is_public=True)
        with mock.patch("core.services.s3_service.S3Service.for_assets", return_value=public):
            self.assertIsNone(_real_storage(self.ch))  # never a permanent public link
        private = mock.Mock(is_public=False, enabled=True, is_configured=True)
        with mock.patch("core.services.s3_service.S3Service.for_assets", return_value=private):
            storage = _real_storage(self.ch)
        self.assertEqual(storage.prefix, f"apps/{channel_media.OWNER}/{self.ws.pk}/")


class CleanupTests(_MediaBase):
    def _folder(self, run_id, hours_old=2):
        folder = channel_media.run_dir(self.script.id, run_id)
        folder.mkdir(parents=True)
        (folder / "a.jpg").write_bytes(b"x")
        old = (timezone.now() - timedelta(hours=hours_old)).timestamp()
        os.utime(folder, (old, old))
        return folder

    def test_the_daily_purge_removes_leftovers_only(self):
        ended = Run.objects.create(script=self.script, workspace=self.ws, status=Run.Status.SUCCESS)
        pending = Run.objects.create(script=self.script, workspace=self.ws, status=Run.Status.PENDING)
        gone, fresh = uuid.uuid4(), uuid.uuid4()
        folders = {name: self._folder(run_id) for name, run_id in (("ended", ended.id), ("pending", pending.id), ("gone", gone))}
        folders["fresh"] = self._folder(fresh, hours_old=0)
        with mock.patch("core.services.s3_service.S3Service.for_assets", return_value=None):
            from core.tasks import daily_housekeeping_task

            result = daily_housekeeping_task()
        self.assertEqual(result["channel_media_deleted"], 2)
        self.assertEqual({n for n, f in folders.items() if f.exists()}, {"pending", "fresh"})

    def test_stored_files_go_after_the_ledger_retention(self):
        now = timezone.now()
        storage = mock.Mock()
        storage.is_available.return_value = True
        storage.list.return_value = [
            {"key": "whatsapp/a/old.jpg", "last_modified": now - timedelta(days=31)},
            {"key": "whatsapp/a/new.jpg", "last_modified": now - timedelta(days=29)},
        ]
        storage.delete.return_value = True
        connection = mock.Mock(enabled=True, is_configured=True)
        Channel.objects.all().delete()  # a deleted channel's files go too
        with mock.patch("core.services.s3_service.S3Service.for_assets", return_value=connection), \
                mock.patch("core.plugins.api.StorageAPI", return_value=storage) as api:
            self.assertEqual(channel_media.purge(now), 1)
        storage.delete.assert_called_once_with("whatsapp/a/old.jpg")
        self.assertEqual(api.call_args.args, (channel_media.OWNER,))

    def test_remove_run_files(self):
        run_id = uuid.uuid4()
        folder = self._folder(run_id)
        channel_media.remove_run_files(self.script.id, run_id)
        self.assertFalse(folder.exists())
        channel_media.remove_run_files(self.script.id, run_id)  # nothing left: no error


@mock.patch("core.executor._validate_environment", return_value=sys.executable)
class RunReadsTheFileTests(_MediaBase):
    """A real subprocess opens the downloaded file from its event; the folder goes when the run ends."""

    def test_the_script_reads_the_path_and_the_folder_is_removed(self, _env):
        self._media_on()
        self.script.code = (
            "import json, os\n"
            "with open(os.environ['PYRUNNER_RUN_FILE'], encoding='utf-8') as fh:\n"
            "    item = json.load(fh)['event']['media'][0]\n"
            "with open(item['path'], 'rb') as fh:\n"
            "    print(len(fh.read()), item['mime'])\n"
        )
        self.script.save()
        self.wa.add_media("555", JPEG)
        run = self._dispatch(_message("image", id="555", mime_type="image/jpeg"))
        run.code_snapshot = self.script.code
        run.save()
        from core.executor import execute_run

        with mock.patch("core.executor.ClaudeService.get_script_env", return_value={}):
            execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.SUCCESS, run.stderr)
        self.assertEqual(run.stdout.strip(), f"{len(JPEG)} image/jpeg")
        self.assertEqual(self._media_dirs(), [])


class OutboundTests(_MediaBase):
    def test_a_file_by_url_inside_the_window(self):
        self._inbound()
        content = {"media": clean_media({"kind": "document", "url": "https://files.example/inv.pdf",
                                         "caption": "Your invoice", "filename": "inv.pdf"})}
        row = ledger.deliver(self.ch, "Your invoice", kind="reply", reply_ref={"chat_id": ANN}, content=content)
        self.assertEqual(row.status, "sent")
        self.assertEqual(self.wa.sends[-1], {
            "messaging_product": "whatsapp", "recipient_type": "individual", "to": ANN, "type": "document",
            "document": {"link": "https://files.example/inv.pdf", "caption": "Your invoice", "filename": "inv.pdf"},
        })

    def test_buttons_and_a_list(self):
        self._inbound()
        buttons = {"type": "buttons", "body": "How can we help?", "footer": "Support",
                   "buttons": [{"id": "track", "title": "Track order"}, {"id": "human", "title": "Talk to us"}]}
        ledger.deliver(self.ch, "How can we help?", kind="reply", reply_ref={"chat_id": ANN},
                       content={"interactive": clean_interactive(buttons)})
        self.assertEqual(self.wa.sends[-1]["interactive"], {
            "type": "button", "body": {"text": "How can we help?"}, "footer": {"text": "Support"},
            "action": {"buttons": [{"type": "reply", "reply": {"id": "track", "title": "Track order"}},
                                   {"type": "reply", "reply": {"id": "human", "title": "Talk to us"}}]},
        })
        lst = {"type": "list", "body": "Pick an order", "header": "Orders", "button": "Choose",
               "sections": [{"title": "Recent", "rows": [{"id": "o-1042", "title": "#1042", "description": "2 items"}]}]}
        ledger.deliver(self.ch, "Pick an order", kind="reply", reply_ref={"chat_id": ANN},
                       content={"interactive": clean_interactive(lst)})
        self.assertEqual(self.wa.sends[-1]["interactive"], {
            "type": "list", "body": {"text": "Pick an order"}, "header": {"type": "text", "text": "Orders"},
            "action": {"button": "Choose", "sections": [
                {"title": "Recent", "rows": [{"id": "o-1042", "title": "#1042", "description": "2 items"}]}]},
        })

    def test_outside_the_window_files_and_buttons_fail_without_a_template(self):
        self.ch.set_credentials({**self.ch.get_credentials(), "default_template": "pyrun_update"}, identity="x")
        self.ch.save()
        content = {"media": clean_media({"kind": "image", "url": "https://x.example/a.png"})}
        row = ledger.deliver(self.ch, "pic", kind="script", content=content)
        self.assertEqual((row.status, row.error), ("failed", OUT_OF_WINDOW_RICH_MESSAGE))
        self.assertEqual(self.wa.sends, [])

    def test_a_retry_sends_the_same_buttons(self):
        self._inbound()
        spec = clean_interactive({"type": "buttons", "body": "Ok?", "buttons": [{"id": "y", "title": "Yes"}]})
        self.wa.error(500, 131000, "Something went wrong")
        row = ledger.deliver(self.ch, "Ok?", kind="reply", reply_ref={"chat_id": ANN}, content={"interactive": spec})
        self.assertEqual(row.status, "retrying")
        self.assertEqual(row.content, {"interactive": spec})
        ChannelMessage.objects.filter(pk=row.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        ledger.drain_due()
        row.refresh_from_db()
        self.assertEqual(row.status, "sent")
        self.assertEqual(self.wa.sends[-1]["interactive"]["action"]["buttons"][0]["reply"]["id"], "y")

    def test_the_provider_rechecks_stored_content(self):
        self._inbound()
        msg = OutboundMessage("x", reply_ref={"chat_id": ANN}, content={"media": {"kind": "gif", "url": "https://a/b"}})
        with self.assertRaises(Exception) as ctx:
            self.provider.send(self.ch, msg)
        self.assertIn("media kind must be one of", str(ctx.exception))

    def test_validation(self):
        bad_media = [
            ("x", "must be an object"), ({"kind": "gif", "url": "https://a/b"}, "kind must be one of"),
            ({"kind": "image"}, "media url is required"), ({"kind": "image", "url": "ftp://a/b"}, "http or https"),
            ({"kind": "image", "url": "https://"}, "http or https"), ({"kind": "audio", "url": "https://a/b", "caption": "c"}, "Audio"),
            ({"kind": "image", "url": "https://a/b", "filename": "f"}, "Only a document"),
            ({"kind": "image", "url": "https://a/b", "size": 1}, "Unknown media keys: size"),
            ({"kind": "image", "url": "https://a/" + "b" * 2048}, "longer than 2048"),
        ]
        for value, error in bad_media:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, error):
                clean_media(value)
        three = [{"id": str(i), "title": f"B{i}"} for i in range(3)]
        bad_interactive = [
            ({"type": "carousel", "body": "b"}, "'buttons' or 'list'"),
            ({"type": "buttons", "buttons": three}, "body is required"),
            ({"type": "buttons", "body": "b", "buttons": three + [{"id": "9", "title": "x"}]}, "1 to 3 buttons"),
            ({"type": "buttons", "body": "b", "buttons": []}, "1 to 3 buttons"),
            ({"type": "buttons", "body": "b", "buttons": [{"id": "a", "title": "x" * 21}]}, "button title is longer than 20"),
            ({"type": "buttons", "body": "b", "buttons": [{"id": "a", "title": "x"}, {"id": "a", "title": "y"}]}, "own id"),
            ({"type": "list", "body": "b", "sections": [{"rows": three}]}, "list button is required"),
            ({"type": "list", "body": "b", "button": "Go", "sections": [{"rows": [{"id": str(i), "title": "t"} for i in range(11)]}]},
             "at most 10 rows"),
            ({"type": "list", "body": "b", "button": "Go", "sections": [{"rows": three}, {"rows": three}]}, "section title is required"),
            ({"type": "list", "body": "b", "button": "Go", "sections": [{"title": "s", "rows": []}]}, "at least one row"),
            ({"type": "list", "body": "b", "button": "Go", "buttons": three}, "Unknown interactive keys: buttons"),
        ]
        for value, error in bad_interactive:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, error):
                clean_interactive(value)


class InternalRichSendTests(_MediaBase):
    def setUp(self):
        super().setUp()
        self.run = Run.objects.create(script=self.script, workspace=self.ws, status=Run.Status.RUNNING)
        self.token = mint_datastore_token(self.run.id)
        self._inbound()

    def _post(self, body):
        return self.client.post(
            reverse("internal:channels_send"), data=json.dumps(body), content_type="application/json",
            REMOTE_ADDR="127.0.0.1", HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )

    def test_a_script_sends_a_file_and_buttons(self):
        resp = self._post({"target": "Support", "reply_ref": {"chat_id": ANN},
                           "media": {"kind": "image", "url": "https://x.example/map.png", "caption": "Here"}})
        self.assertEqual(resp.status_code, 200, resp.content)
        resp = self._post({"target": "Support", "reply_ref": {"chat_id": ANN},
                           "interactive": {"type": "buttons", "body": "Solved?", "buttons": [{"id": "y", "title": "Yes"}]}})
        self.assertEqual(resp.status_code, 200, resp.content)
        resp = self._post({"target": "Support", "reply_ref": {"chat_id": ANN},
                           "media": {"kind": "document", "url": "https://x.example/inv.pdf"}})
        rows = list(self._out().order_by("created_at", "id"))
        self.assertEqual([r.text for r in rows], ["Here", "Solved?", "Document: https://x.example/inv.pdf"])
        self.assertEqual(rows[0].content, {"media": {"kind": "image", "url": "https://x.example/map.png", "caption": "Here"}})
        self.assertEqual(rows[1].run_id, self.run.id)
        self.assertEqual([s["type"] for s in self.wa.sends], ["image", "interactive", "document"])

    def test_bad_requests_are_refused(self):
        both = {"target": "Support", "media": {"kind": "image", "url": "https://a/b"},
                "interactive": {"type": "buttons", "body": "b", "buttons": [{"id": "y", "title": "Y"}]}}
        resp = self._post(both)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("not several", resp.json()["error"]["message"])
        self.assertEqual(self._post({"target": "Support", "media": {"kind": "gif"}}).status_code, 400)
        self.assertEqual(self._post({"target": "email", "media": {"kind": "image", "url": "https://a/b"}}).status_code, 400)
        tg = Channel(workspace=self.ws, provider="telegram", name="TG")
        tg.set_credentials({"bot_token": "1:2"}, identity="1:2")
        tg.save()
        resp = self._post({"target": "TG", "interactive": {"type": "buttons", "body": "b", "buttons": [{"id": "y", "title": "Y"}]}})
        self.assertIn("doesn't send interactive messages", resp.json()["error"]["message"])
        resp = self._post({"target": "TG", "media": {"kind": "image", "url": "https://a/b"}})
        self.assertIn("doesn't send files", resp.json()["error"]["message"])
        self.assertEqual(self._out().count(), 0)

    def test_pyrunner_notify_passes_media_and_buttons(self):
        from core.script_helpers import pyrunner_notify

        buttons = {"type": "buttons", "body": "b", "buttons": [{"id": "y", "title": "Y"}]}
        env = {"INBOUND_CHANNEL": "Support", "INBOUND_REPLY_REF": json.dumps({"chat_id": ANN})}
        with mock.patch.object(pyrunner_notify, "_post", return_value={"ok": True}) as post, \
                mock.patch.dict(os.environ, env):
            pyrunner_notify.send("Support", media={"kind": "image", "url": "https://a/b"})
            pyrunner_notify.reply(interactive=buttons)
            pyrunner_notify.reply("plain")
        self.assertEqual(post.call_args_list[0].args[0],
                         {"target": "Support", "text": "", "media": {"kind": "image", "url": "https://a/b"}})
        self.assertEqual(post.call_args_list[1].args[0],
                         {"target": "Support", "text": "", "reply_ref": {"chat_id": ANN}, "interactive": buttons})
        self.assertEqual(post.call_args_list[2].args[0], {"target": "Support", "text": "plain", "reply_ref": {"chat_id": ANN}})
