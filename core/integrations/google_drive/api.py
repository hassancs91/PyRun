"""The google_drive integration: files and folders in Google Drive (see core/integrations/framework.py)."""

from core.integrations.framework import Integration, Smoke


def _smoke_list(module, connection, config):
    """List a few files (read-only): with the drive.file scope, maybe none."""
    files = list(module.list_files(limit=5, connection=connection))
    return f"list_files answered with {len(files)} file(s) (first 5)"


EXAMPLE = '''\
from pyrunner_integrations import google_drive

# Upload today's report into a folder and get a link anyone can open.
folder = google_drive.create_folder("Reports")
uploaded = google_drive.upload("/tmp/report.pdf", folder=folder["id"])
print(google_drive.share_link(uploaded["id"]))

# The ten most recent PDFs, then download the first one.
pdfs = list(google_drive.list_files(query="mimeType = 'application/pdf'", order_by="modifiedTime desc", limit=10))
if pdfs:
    google_drive.download(pdfs[0]["id"], "/tmp/latest.pdf")
'''

INTEGRATION = Integration(
    key="google_drive",
    provider="google",
    title="Google Drive",
    functions=("list_files", "get_file", "create_folder", "upload", "download", "share_link", "client"),
    example=EXAMPLE,
    smoke=(Smoke("list_files", _smoke_list),),
)
