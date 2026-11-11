"""The gmail integration: search, read, send and reply to Gmail, labels and attachments (see core/integrations/framework.py)."""

from core.integrations.framework import Integration, Smoke


def _smoke_read(module, connection, config):
    """List the mailbox's labels, then look for one message (read-only)."""
    labels = module.list_labels(connection=connection)
    found = list(module.search(limit=1, connection=connection))
    return f"list_labels answered with {len(labels)} label(s); search answered with {len(found)} message(s)"


EXAMPLE = '''\
from pyrunner_integrations import gmail

# Answer new quote requests once, then file them.
for found in gmail.search("is:unread subject:quote", limit=10):
    msg = gmail.get_message(found["id"])
    print(msg["from"], msg["subject"])
    gmail.reply(found["id"], "Thanks! We'll send the quote today.")
    gmail.modify_labels(found["id"], add="Quotes", remove="UNREAD")

# Save the PDFs of last week's invoices.
for found in gmail.search("label:Invoices has:attachment newer_than:7d"):
    for attachment in gmail.get_message(found["id"])["attachments"]:
        if attachment["filename"].lower().endswith(".pdf"):
            gmail.download_attachment(found["id"], attachment, f"/tmp/{attachment['filename']}")

gmail.send("team@example.com", "Invoices saved", "Last week's invoices are in /tmp.")
'''

INTEGRATION = Integration(
    key="gmail",
    provider="google",
    title="Gmail",
    functions=("search", "get_message", "send", "reply", "list_labels", "create_label", "modify_labels",
               "download_attachment", "new_messages", "client"),
    example=EXAMPLE,
    smoke=(Smoke("read", _smoke_read),),
)
