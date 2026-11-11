"""
Google Drive for scripts: list, upload and download files, make folders and share links.

Files come back in Drive's own shape, with these fields unless you ask for
others: ``id``, ``name``, ``mimeType``, ``parents``, ``size`` (bytes, as text;
none for Google Docs, Sheets and Slides), ``modifiedTime`` and
``webViewLink``. A file's or folder's id is the part of its URL after
``/d/`` or ``/folders/``. Files in shared drives work too.

The connection is a Google connection with a Drive scope set: **Drive: files
it made or opened** (``drive.file``) sees only the files this connection
uploaded or created; **Drive: all files** (``drive``) sees everything the
account can. Its token goes only to ``https://www.googleapis.com``.
"""

import hashlib
import json
import mimetypes
import os

from . import _client

PROVIDER = "google"
FIELDS = "id, name, mimeType, parents, size, modifiedTime, webViewLink"
FOLDER = "application/vnd.google-apps.folder"
PAGE_SIZE = 100  # files per page (Drive allows up to 1000)
MULTIPART_MAX = 5 * 1024 * 1024  # larger uploads go as a resumable upload (Drive's advice)
# Shortcuts for download(export_as=...): Google Docs, Sheets and Slides export to these.
EXPORT_TYPES = {
    "pdf": "application/pdf",
    "csv": "text/csv",
    "txt": "text/plain",
    "html": "text/html",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
_ROLES = ("reader", "commenter", "writer")
# Python's own extension table, not the system's (Windows' registry calls .csv application/vnd.ms-excel).
_TYPES = mimetypes.MimeTypes()
_ALL_DRIVES = {"supportsAllDrives": "true"}


def _api(connection):
    return _client.shared(connection, PROVIDER)


def _file_path(file_id, suffix="") -> str:
    return f"/drive/v3/files/{_client.quote(file_id)}{suffix}"


def _literal(value) -> str:
    """``value`` as a quoted string in a Drive search query."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _multipart(metadata, data, mime_type):
    """``(body, content type)`` of a multipart/related upload: the metadata as JSON, then the file."""
    boundary = "pyrunner-" + hashlib.sha256(data).hexdigest()[:32]
    while boundary.encode("ascii") in data:
        boundary += "x"
    head = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata)}\r\n--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n")
    body = head.encode("utf-8") + data + f"\r\n--{boundary}--".encode("ascii")
    return body, f"multipart/related; boundary={boundary}"


def client(connection=None, *, timeout=30):
    """The shared client of ``connection``, for calls this module has no function for.

    Paths are relative to ``https://www.googleapis.com``. For example:

        about = google_drive.client().get("/drive/v3/about", params={"fields": "storageQuota"})
    """
    return _client.Client(connection, provider=PROVIDER, timeout=timeout)


def list_files(*, query=None, folder=None, fields=None, order_by=None, include_trashed=False, limit=None,
               connection=None):
    """Yield the files the connection can see, fetching pages as it goes.

    ``folder`` limits it to one folder's direct children (the folder's id).
    ``query`` is a Drive search query of your own
    (``"name contains 'invoice' and mimeType = 'application/pdf'"``). Trashed
    files are left out unless ``include_trashed``. ``fields`` replaces the
    default file fields, ``order_by`` sorts (``"modifiedTime desc"``),
    ``limit`` stops after that many. For example:

        for f in google_drive.list_files(folder=folder_id, order_by="modifiedTime desc", limit=20):
            print(f["name"], f["modifiedTime"])
    """
    terms = []
    if folder:
        terms.append(f"{_literal(folder)} in parents")
    if not include_trashed:
        terms.append("trashed = false")
    if query:
        terms.append(f"({query})")
    params = {
        "pageSize": min(PAGE_SIZE, limit) if limit and limit > 0 else PAGE_SIZE,
        "fields": f"nextPageToken, files({fields or FIELDS})",
        "includeItemsFromAllDrives": "true",
        **_ALL_DRIVES,
    }
    if terms:
        params["q"] = " and ".join(terms)
    if order_by:
        params["orderBy"] = order_by
    return _api(connection).paginate("/drive/v3/files", params=params, items="files", cursor="nextPageToken",
                                     cursor_param="pageToken", limit=limit)


def get_file(file_id, *, fields=None, connection=None):
    """One file's (or folder's) details; one that doesn't exist (or isn't visible) raises NOT_FOUND.

    For example:

        info = google_drive.get_file(file_id)
        print(info["name"], info["mimeType"])
    """
    return _api(connection).get(_file_path(file_id), params={"fields": fields or FIELDS, **_ALL_DRIVES})


def create_folder(name, *, parent=None, connection=None):
    """Create a folder (in ``parent``, a folder's id, or at the top of My Drive); returns it.

    For example:

        folder = google_drive.create_folder("Reports 2026")
        google_drive.upload("report.pdf", folder=folder["id"])
    """
    body = {"name": str(name), "mimeType": FOLDER}
    if parent:
        body["parents"] = [parent]
    return _api(connection).post("/drive/v3/files", params={"fields": FIELDS, **_ALL_DRIVES}, json=body)


def upload(source, *, name=None, folder=None, mime_type=None, file_id=None, connection=None):
    """Upload a file; returns the new file.

    ``source`` is a file's path, or the content as bytes (then ``name`` is
    needed; for text, ``text.encode()``). ``name`` defaults to the path's file
    name, ``mime_type`` to a guess from the name. ``folder`` puts it in that
    folder (its id). ``file_id`` replaces that file's content instead (and its
    name, when ``name`` is given; pass ``mime_type`` when replacing with bytes
    and no name). The content is read into memory; files over 5 MB go as a
    resumable upload, in one request. For example:

        uploaded = google_drive.upload("/tmp/report.pdf", folder=folder_id)
        google_drive.upload(b"id,total\\n1,30\\n", name="totals.csv", folder=folder_id)
    """
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
        if not name and not file_id:
            raise ValueError("upload() of bytes needs name=")
    elif isinstance(source, (str, os.PathLike)):
        path = os.fspath(source)
        with open(path, "rb") as f:
            data = f.read()
        name = name or os.path.basename(path)
    else:
        raise TypeError(f"upload() takes a file path or bytes, not {type(source).__name__}")
    if file_id and folder:
        raise ValueError("upload() replaces a file's content with file_id=, or makes a new one in folder=: not both")
    mime_type = mime_type or _TYPES.guess_type(name or "")[0] or "application/octet-stream"
    metadata = {}
    if name:
        metadata["name"] = str(name)
    if folder:
        metadata["parents"] = [folder]
    api = _api(connection)
    method, path = ("PATCH", f"/upload{_file_path(file_id)}") if file_id else ("POST", "/upload/drive/v3/files")
    if len(data) <= MULTIPART_MAX:
        body, content_type = _multipart(metadata, data, mime_type)
        return api.call(method, path, params={"uploadType": "multipart", "fields": FIELDS, **_ALL_DRIVES},
                        data=body, headers={"Content-Type": content_type})
    # Resumable: the metadata opens an upload session, then the content goes to it in one request.
    started = api.request(method, path, params={"uploadType": "resumable", "fields": FIELDS, **_ALL_DRIVES},
                          json=metadata, headers={"X-Upload-Content-Type": mime_type,
                                                  "X-Upload-Content-Length": str(len(data))})
    session = started.headers.get("location")
    if not session:
        raise _client.IntegrationError(f"{method} {path}: Drive opened no upload session (no Location)",
                                       "BAD_RESPONSE", started.status_code, response=started)
    # Not repeated after a 5xx: a broken session is started again by calling upload() again.
    response = api.request("PUT", session, data=data, headers={"Content-Type": mime_type}, idempotent=False)
    if response.status_code not in (200, 201):
        raise _client.IntegrationError(f"PUT {_client.safe_url(session)}: the upload didn't finish "
                                       f"(HTTP {response.status_code})", "BAD_RESPONSE", response.status_code,
                                       response=response)
    return _client.decode(response, "PUT")


def download(file_id, path=None, *, export_as=None, connection=None):
    """A file's content: bytes, or written to ``path`` (then the path is returned).

    Google Docs, Sheets and Slides have no content of their own: pass
    ``export_as`` (``"pdf"``, ``"csv"``, ``"xlsx"``, ``"docx"``, ``"pptx"``,
    ``"txt"``, ``"html"`` or a MIME type) to download them converted (Drive
    exports up to 10 MB). The content is held in memory. For example:

        data = google_drive.download(file_id)
        google_drive.download(sheet_file_id, "/tmp/sheet.csv", export_as="csv")
    """
    if export_as:
        mime_type = EXPORT_TYPES.get(str(export_as).lower(), str(export_as))
        response = _api(connection).request("GET", _file_path(file_id, "/export"), params={"mimeType": mime_type})
    else:
        response = _api(connection).request("GET", _file_path(file_id), params={"alt": "media", **_ALL_DRIVES})
    if path is None:
        return response.content
    with open(path, "wb") as f:
        f.write(response.content)
    return os.fspath(path)


def share_link(file_id, *, role="reader", connection=None):
    """Let anyone with the link open the file (as ``role``) and return the link.

    ``role`` is ``"reader"``, ``"commenter"`` or ``"writer"``. The file isn't
    listed publicly: only people with the link find it. A shared drive or an
    organisation may forbid link sharing (FORBIDDEN). For example:

        link = google_drive.share_link(uploaded["id"])
    """
    if role not in _ROLES:
        raise ValueError(f"role is one of {', '.join(_ROLES)}, not {role!r}")
    api = _api(connection)
    api.post(_file_path(file_id, "/permissions"), params=_ALL_DRIVES,
             json={"type": "anyone", "role": role, "allowFileDiscovery": False})
    return api.get(_file_path(file_id), params={"fields": "webViewLink", **_ALL_DRIVES}).get("webViewLink")
