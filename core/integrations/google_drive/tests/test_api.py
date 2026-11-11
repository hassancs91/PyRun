"""The google_drive integration's tests: the conformance suite and exchanges replayed offline (core/integrations/testing.py).

The cassettes are hand-built from Google's public Drive API v3 reference
(developers.google.com/drive/api/reference/rest/v3): no live account was
used. The fake-service tests pin what the cassettes can't show in one place
(resumable uploads, query quoting, argument checks, retries).
"""

import os
import tempfile
from unittest import mock

from core.integrations import testing

FOLDER_ID = "1FolderReports0123456789abcdefghi"
REPORT = "1FileReport0123456789abcdefghij"
TOTALS = "1FileTotals0123456789abcdefghij"
DOC = "1DocNotes01234567890abcdefghijk"
CSV = b"id,total\n1,30\n2,12\n"


class GoogleDriveConformance(testing.ConformanceSuite):
    integration = "google_drive"
    pages = [
        {"json": {"files": [{"id": "f1"}, {"id": "f2"}], "nextPageToken": "t2"}},
        {"json": {"files": [{"id": "f3"}]}},
    ]
    page_items = [{"id": "f1"}, {"id": "f2"}, {"id": "f3"}]

    def call(self, module):
        return module.get_file("f1")

    def paginate_call(self, module, limit=None):
        return module.list_files(limit=limit)


class _Drive(testing.IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.drive = self.integration("google_drive")
        self.conn = self.use_connection("google", provider="google")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _error(self, fn, *args, **kwargs):
        with self.assertRaises(self.integration_client.IntegrationError) as ctx:
            fn(*args, **kwargs)
        self.assertNoSecrets(str(ctx.exception))
        return ctx.exception


class GoogleDriveCassetteTests(_Drive):
    def test_list_files_in_a_folder_page_by_page_and_with_a_query(self):
        with self.cassette("list_files"):
            files = list(self.drive.list_files(folder=FOLDER_ID, order_by="modifiedTime desc"))
            found = list(self.drive.list_files(query="name contains 'report'", fields="id, name", limit=2))
        self.assertEqual([f["name"] for f in files], ["report.pdf", "totals.csv", "Notes"])
        self.assertEqual(files[0]["size"], "48213")
        self.assertEqual(found, [{"id": REPORT, "name": "report.pdf"}])

    def test_get_file(self):
        with self.cassette("get_file"):
            info = self.drive.get_file(REPORT)
        self.assertEqual((info["name"], info["mimeType"], info["parents"]), ("report.pdf", "application/pdf",
                                                                             [FOLDER_ID]))

    def test_create_folder(self):
        with self.cassette("create_folder"):
            folder = self.drive.create_folder("Reports 2026", parent="0AParentRootFolder0")
        self.assertEqual((folder["id"], folder["mimeType"]), (FOLDER_ID, "application/vnd.google-apps.folder"))

    def test_upload_a_path_into_a_folder_then_replace_its_content(self):
        path = os.path.join(self.tmp.name, "totals.csv")
        with open(path, "wb") as f:
            f.write(CSV)
        with self.cassette("upload"):
            uploaded = self.drive.upload(path, folder=FOLDER_ID)
            replaced = self.drive.upload(CSV + b"3,9\n", name="totals.csv", file_id=uploaded["id"])
        self.assertEqual((uploaded["id"], uploaded["size"]), (TOTALS, "14"))
        self.assertEqual((replaced["id"], replaced["size"]), (TOTALS, "18"))

    def test_download_bytes_and_an_export_to_a_path(self):
        target = os.path.join(self.tmp.name, "notes.pdf")
        with self.cassette("download"):
            data = self.drive.download(TOTALS)
            written = self.drive.download(DOC, target, export_as="pdf")
        self.assertEqual(data, CSV)
        self.assertEqual(written, target)
        with open(target, "rb") as f:
            content = f.read()
        self.assertTrue(content.startswith(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3"), "binary content kept byte for byte")

    def test_share_link(self):
        with self.cassette("share_link"):
            link = self.drive.share_link(REPORT)
        self.assertEqual(link, f"https://drive.google.com/file/d/{REPORT}/view?usp=drivesdk")

    def test_errors_keep_googles_message(self):
        with self.cassette("errors"):
            doc = self._error(self.drive.download, DOC)
            missing = self._error(self.drive.get_file, "nope")
            scope = self._error(self.drive.share_link, REPORT, role="writer")
        self.assertEqual((doc.code, doc.status), ("FORBIDDEN", 403))
        self.assertIn("Use Export with Docs Editors files", str(doc))
        self.assertEqual((missing.code, missing.status), ("NOT_FOUND", 404))
        self.assertIn("File not found: nope.", str(missing))
        self.assertEqual(scope.code, "FORBIDDEN")
        self.assertIn("insufficient authentication scopes", str(scope))


class GoogleDriveRequestTests(_Drive):
    def setUp(self):
        super().setUp()
        self.fake = self.fake_http()

    def test_the_token_goes_to_www_googleapis_as_a_bearer_header(self):
        self.fake.add("GET", f"/drive/v3/files/{REPORT}", json={"id": REPORT})
        self.drive.get_file(REPORT)
        sent = self.fake.requests[0]
        self.assertTrue(sent.url.startswith("https://www.googleapis.com/drive/v3/files/"), sent.url)
        self.assertEqual(sent.headers["Authorization"], f"Bearer {self.conn.credentials['access_token']}")

    def test_query_values_are_quoted_and_trashed_files_optional(self):
        self.fake.default(json={"files": []})
        list(self.drive.list_files(folder="it's\\here"))
        list(self.drive.list_files(include_trashed=True, limit=5000))
        self.assertEqual(self.fake.requests[0].params["q"], "'it\\'s\\\\here' in parents and trashed = false")
        self.assertNotIn("q", self.fake.requests[1].params)
        self.assertEqual(self.fake.requests[1].params["pageSize"], "100")

    def test_a_multipart_upload_carries_the_metadata_and_the_bytes(self):
        self.fake.add("POST", "/upload/drive/v3/files", json={"id": "f1"})
        self.drive.upload(b"\x00\x01binary", name="blob.bin", mime_type="application/x-thing")
        sent = self.fake.requests[0]
        boundary = sent.headers["Content-Type"].split("boundary=", 1)[1]
        self.assertTrue(sent.headers["Content-Type"].startswith("multipart/related; "))
        self.assertEqual(sent.params["uploadType"], "multipart")
        parts = sent.body.split(f"--{boundary}".encode())
        self.assertEqual(len(parts), 4)  # before, metadata, file, "--" after the closing boundary
        self.assertIn(b'{"name": "blob.bin"}', parts[1])
        self.assertTrue(parts[2].endswith(b"Content-Type: application/x-thing\r\n\r\n\x00\x01binary\r\n"))

    def test_a_large_file_goes_as_a_resumable_upload(self):
        session = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=xyz"
        self.fake.add("POST", "/upload/drive/v3/files", headers={"Location": session})
        self.fake.add("PUT", session, status=200, json={"id": "big1", "name": "big.bin"})
        with mock.patch.object(self.drive, "MULTIPART_MAX", 4):
            made = self.drive.upload(b"0123456789", name="big.bin", folder=FOLDER_ID)
        self.assertEqual(made["id"], "big1")
        start, put = self.fake.requests
        self.assertEqual((start.params["uploadType"], start.json()), ("resumable", {"name": "big.bin",
                                                                                     "parents": [FOLDER_ID]}))
        self.assertEqual((start.headers["X-Upload-Content-Length"], start.headers["X-Upload-Content-Type"]),
                         ("10", "application/octet-stream"))
        self.assertEqual((put.method, put.body, put.headers["Content-Type"]),
                         ("PUT", b"0123456789", "application/octet-stream"))

    def test_a_resumable_upload_that_fails_or_stops_raises(self):
        with mock.patch.object(self.drive, "MULTIPART_MAX", 4):
            self.fake.add("POST", "/upload/drive/v3/files", json={})
            error = self._error(self.drive.upload, b"0123456789", name="big.bin")
            self.assertEqual(error.code, "BAD_RESPONSE")
            session = "/upload/drive/v3/files?uploadType=resumable&upload_id=s2"
            self.fake.add("POST", "/upload/drive/v3/files", headers={"Location": session})
            self.fake.add("PUT", None, status=503)
            error = self._error(self.drive.upload, b"0123456789", name="big.bin")
            self.assertEqual((error.code, self.sleeps), ("SERVER_ERROR", []), "the PUT isn't repeated")
            self.fake.add("POST", "/upload/drive/v3/files", headers={"Location": session})
            self.fake.add("PUT", None, status=308, headers={"Range": "bytes=0-4"})
            error = self._error(self.drive.upload, b"0123456789", name="big.bin")
            self.assertEqual((error.code, error.status), ("BAD_RESPONSE", 308))

    def test_upload_arguments_are_checked_before_any_request(self):
        with self.assertRaisesRegex(ValueError, "needs name="):
            self.drive.upload(b"data")
        with self.assertRaisesRegex(ValueError, "not both"):
            self.drive.upload(b"data", name="x", folder="f", file_id="g")
        with self.assertRaisesRegex(TypeError, "file path or bytes"):
            self.drive.upload(123)
        with self.assertRaises(FileNotFoundError):
            self.drive.upload(os.path.join(self.tmp.name, "missing.txt"))
        with self.assertRaisesRegex(ValueError, "role is one of reader, commenter, writer"):
            self.drive.share_link(REPORT, role="owner")
        self.assertEqual(self.fake.requests, [])

    def test_a_path_upload_guesses_the_type_and_a_new_file_is_not_repeated_after_a_5xx(self):
        path = os.path.join(self.tmp.name, "notes.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("hello")
        self.fake.add("POST", "/upload/drive/v3/files", status=500)
        error = self._error(self.drive.upload, path)
        self.assertEqual((error.code, len(self.fake.requests)), ("SERVER_ERROR", 1))
        self.assertIn(b"Content-Type: text/plain\r\n\r\nhello", self.fake.requests[0].body)
        self.assertIn(b'"name": "notes.txt"', self.fake.requests[0].body)

    def test_export_takes_a_shortcut_or_a_mime_type_and_a_download_is_retried(self):
        self.fake.add("GET", None, body=b"a,b\n")
        self.fake.add("GET", None, status=502).add("GET", None, body=b"x")
        self.assertEqual(self.drive.download(DOC, export_as="CSV"), b"a,b\n")
        self.assertEqual(self.drive.download(DOC, export_as="application/rtf"), b"x")
        self.assertEqual(self.fake.requests[0].params["mimeType"], "text/csv")
        self.assertEqual(self.fake.requests[2].params["mimeType"], "application/rtf")
        self.assertEqual(self.sleeps, [1.0])
