# Chat channels

A channel connects PyRun to a chat service. It sends run notifications and
a script's `pyrunner_notify.send()` messages, and, with **Inbound** on, lets
people message it to run a script or ask Py AI. Channels live under
**Channels** in the console; each one keeps its credentials encrypted, and
scripts send through it without ever seeing them.

Two providers are built in: **Telegram** (a bot) and **WhatsApp** (a
WhatsApp Business number on Meta's Cloud API, called directly: no Twilio, no
unofficial bridge). How sends are recorded, retried and capped is the same
for both: see [Chat messages and deliveries](run-context.md#chat-messages-and-deliveries).

Delivery is **at least once**. A message being sent is leased to one worker;
if that worker dies mid-send (after the chat service took the message but
before PyRun recorded it), the message is sent again once the lease runs out,
so a recipient can, rarely, get it twice.

Each channel's inbound callback URL takes at most **60 requests a minute from
one client address**; past that it answers `429` until the minute is over.
Telegram and Meta retry those calls later, so on a very busy channel
incoming messages and WhatsApp's status callbacks can arrive late.

---

## Telegram

Create a bot with `@BotFather`, add a channel with its token, message the bot,
then use **Find chat ID** on the channel's page to set the default chat. For
inbound, turn **Inbound** on: PyRun registers its webhook with Telegram
itself. Inbound needs a publicly reachable HTTPS instance.

---

## WhatsApp

A WhatsApp channel is one WhatsApp Business phone number. It sends text,
approved templates, files by URL, reply buttons and lists, receives messages
(text, button and list replies, locations, contacts, reactions; photos,
documents, voice notes and videos as metadata, or downloaded when you turn
that on), and shows each message's delivery: sent, delivered, read or
failed. One-to-one chats only: group messages are ignored.

### Set it up in Meta

1. **Create a Meta app.** On developers.facebook.com, create an app of type
   *Business* and add the **WhatsApp** product. Its **API Setup** page gives
   you a free **test number** and a temporary token. With the test number
   you can message up to five recipient numbers you add (and verify) on that
   page: enough to try everything below.
2. **Note the ids.** API Setup shows the **Phone number ID** (an id, not the
   phone number) and the **WhatsApp Business Account ID** (WABA).
3. **Make a permanent token.** The API Setup token expires after 24 hours. In
   **Meta Business settings → Users → System users**, add a system user
   (admin), assign it your app and the WhatsApp account, and **Generate
   token** with the `whatsapp_business_messaging` permission (add
   `whatsapp_business_management` to manage templates from the API). Choose
   "Never" as its expiry.
4. **Get the app secret.** **App settings → Basic → App secret.** PyRun uses
   it to check that each webhook call really comes from Meta.
5. **Verify your business** (Meta Business settings → Security Center) before
   you go live with your own number: an unverified business has a low
   messaging limit and can't get its display name approved. The test number
   doesn't need it.

### Add the channel in PyRun

**Channels → Add channel → WhatsApp**, then fill in:

| Field | What to enter |
|---|---|
| Phone number ID | from API Setup |
| WhatsApp Business Account ID | from API Setup |
| Access token | the System User token (stored encrypted, never shown again) |
| App secret | needed for inbound (encrypted) |
| Verify token | any string you choose; you enter the same one in Meta (encrypted) |
| Default template | an approved template for sends outside the 24-hour window (below); optional |
| Default template language | its language code, `en_US` when empty |
| Default phone number | where notifications go: country code and number, digits only, e.g. `15551234567` |

One number can be connected to one channel. **Test** on the channel's page
reads the number's display name from Meta and checks that the number belongs
to the WhatsApp Business Account (that check needs
`whatsapp_business_management`; without it Test still connects and says the
account couldn't be checked). If PyRun's `ENCRYPTION_KEY`
changes, the stored secrets can't be read: the channel shows as not
configured until you enter them again.

### Inbound: subscribe the webhook

1. On the channel's page, turn **Inbound** on, pick what a message does
   (**Run a script** or **Ask Py AI**) and save. The page then shows the
   **Callback URL**.
2. In your Meta app, open **WhatsApp → Configuration → Webhook → Edit**:
   paste the callback URL and your verify token, and **Verify and save**.
   Meta calls the URL with a challenge; PyRun answers it only when the verify
   token matches (otherwise 403).
3. Under **Webhook fields**, **subscribe** to `messages` (it carries both
   incoming messages and delivery statuses).

Every webhook call must carry a valid `X-Hub-Signature-256` signature (an
HMAC-SHA256 of the body with your app secret); one without it, or with a
body changed on the way, is refused with 401. Inbound needs a publicly
reachable HTTPS instance, and the channel must be enabled with Inbound on
before you verify (the URL answers 404 otherwise).

A Meta app has one webhook URL for all its numbers: if two PyRun channels use
numbers from the same app, give each number its own app (or its own webhook
override in Meta), since a channel ignores messages and statuses for any
other phone number id.

**Who can use it.** As with Telegram, new senders land in the channel's
**Approval inbox** (they get one "not approved yet" reply) until you approve
them; **Open** lets anyone who reaches the number through. PyRun marks each
message it handles as read (the blue ticks).

**What a script gets.** The script handler's run gets the message as
`INBOUND_*` variables and as its [run event](run-context.md#the-run-event)
(`kind: "channel"`), which for WhatsApp also has `message_type`,
`message_id` and, when the message has them, `details` and `media`:

| `message_type` | `text` | `details` |
|---|---|---|
| `text` | the message | none |
| `interactive` (a button or list reply) | the chosen title | `reply`: `{type: "button_reply" or "list_reply", id, title, description}` |
| `button` (a template's quick-reply button) | the button text | `reply`: `{type: "button", id (its payload), title}` |
| `location` | `Location: <name, address> (<lat>, <long>)` | `location`: `{latitude, longitude, name, address, url}` |
| `contacts` | `Contact: <name> <phones>` | `contacts`: `[{name, phones, emails}]` |
| `reaction` | the emoji | `reaction`: `{emoji, message_id}` (the message reacted to); a removed reaction is ignored |
| `image`, `document`, `audio`, `video`, `sticker` | the caption, if any | none; `media`: `[{kind, id, mime, caption, filename (documents), sha256}]` |

A reply to an earlier message adds `details.context`: `{message_id, from}`.
Other message types (system notices, orders, unsupported ones) are ignored.

### Inbound media: downloading attachments

By default an attachment arrives as metadata only: PyRun doesn't download the
file and writes nothing to disk. Turn on **Download media** in the channel's
**Inbound** settings to have PyRun fetch images, documents, audio, video and
stickers for the script handler. WhatsApp's download link expires about five
minutes after the message arrives, so PyRun downloads the file as soon as it
handles the message, before the run starts. Each `media` item then also has
`size` (bytes) and one of:

- `path`: the file in the run's own folder inside the script's working folder
  (`.pyrun-media-<run id>/`). The run can read it; the folder is deleted when
  the run ends, so copy the file elsewhere if you need to keep it.
- `url`: when an assets storage connection is set (Services → Object Storage)
  and it has no public base URL, the file is stored there instead (under
  `apps/pyrun-channels/`) and `url` is a link valid for 24 hours. Stored files
  are deleted after 30 days. A public connection isn't used for customers'
  files: they get a `path`.

**Largest attachment (MB)** (16 by default, up to 100) caps each file: a
bigger one isn't downloaded. A file that can't be fetched (too big, an expired
link, a rejected token) keeps its metadata and gets an `error` sentence; the
run starts anyway. A Py AI channel never downloads: Py AI reads text only, so
it is told the kind, name and type of what was attached.

```python
import os
import shutil
import urllib.request

import pyrunner_notify
import pyrunner_run

os.makedirs("receipts", exist_ok=True)
for item in pyrunner_run.event().get("media", []):
    if item.get("error"):
        pyrunner_notify.reply(f"I couldn't read that file: {item['error']}")
    elif "path" in item:
        shutil.copy(item["path"], f"receipts/{item['id']}")
    else:
        urllib.request.urlretrieve(item["url"], f"receipts/{item['id']}")
```

```python
import pyrunner_notify
import pyrunner_run

event = pyrunner_run.event()
if event.get("message_type") == "interactive" and event["details"]["reply"]["id"] == "track":
    pyrunner_notify.reply("Your order is on its way.")
```

### Sending: the 24-hour window and templates

WhatsApp accepts free-form text only within **24 hours of the person's last
message** to your number (the customer-service window). Outside it, only an
approved **template** can be sent. PyRun tracks each number's last message
and decides per send:

- **Inside the window** (with a 5-minute margin): plain text, up to 4,096
  characters (longer text is cut).
- **Outside it**: the channel's **default template**, with the message text
  as its first body variable. Create it in WhatsApp Manager → **Message
  templates** with exactly one variable, for example *"Update from PyRun:
  {{1}}"* (category Utility), and wait for approval (usually minutes). Line
  breaks become ` | ` (templates can't hold them) and the text is cut to
  1,024 characters. The message log shows `template <name>` on such sends.
- **Outside it, no default template**: the send fails with a message saying
  so, and isn't retried.

A script can also send a template of its choice (needed for templates with
several variables, buttons or a header):

```python
pyrunner_notify.send(
    "Support",
    reply_ref={"chat_id": "15551234567"},
    template={
        "name": "order_update",
        "language": "en_US",
        "components": [{"type": "body", "parameters": [{"type": "text", "text": "1042"}]}],
    },
)
```

`components` is passed to WhatsApp as written (see Meta's template message
reference). `text` is optional with a template; the message log shows
"Template order_update" without it. A template is kept with the message, so
a retry sends the same one. Sending a template to a Telegram channel is
refused (HTTP 400).

### Sending files, buttons and lists

Inside the 24-hour window a script can also send a file WhatsApp downloads
from a URL, up to three reply buttons, or a list of up to ten choices. All
three go through the delivery ledger like text (a retry sends the same
message) and are refused outside the window: the send fails with a message
saying so, as there's no template to fall back to. `send()` and `reply()`
take one of `template`, `media` or `interactive`:

```python
pyrunner_notify.reply(media={
    "kind": "document",                 # image, document, audio or video
    "url": "https://files.example.com/invoices/1042.pdf",
    "filename": "invoice-1042.pdf",     # documents only
    "caption": "Your invoice",          # not for audio
})
pyrunner_notify.reply(interactive={
    "type": "buttons",                  # 1 to 3 buttons, titles up to 20 characters
    "body": "How can we help?",
    "buttons": [{"id": "track", "title": "Track my order"}, {"id": "human", "title": "Talk to a person"}],
    "footer": "Acme support",           # optional, as is "header"
})
pyrunner_notify.reply(interactive={
    "type": "list",                     # up to 10 rows in all, titles up to 24 characters
    "body": "Which order?",
    "button": "Choose",                 # the label of the button that opens the list
    "sections": [{"title": "Recent", "rows": [
        {"id": "order-1042", "title": "#1042", "description": "2 items, shipped"},
        {"id": "order-1038", "title": "#1038", "description": "Delivered"},
    ]}],
})
```

The URL must be an http(s) link WhatsApp can reach (a presigned link works).
The person's tap comes back as a new inbound `interactive` message whose
`details.reply.id` is the id you gave. The message log shows the caption (or
the file's kind and link) for a file and the body for buttons and lists.
Telegram channels refuse both (HTTP 400).

**A support flow.** One script linked to the channel (handler **Run a
script**) answers every message: a greeting gets the menu, a tap on a button
or a list row gets its answer, and a photo of a receipt (with **Download
media** on) is saved.

```python
import os
import shutil

import pyrunner_notify
import pyrunner_run

MENU = {
    "type": "buttons",
    "body": "Hi! How can we help?",
    "buttons": [{"id": "orders", "title": "My orders"}, {"id": "human", "title": "Talk to a person"}],
}

event = pyrunner_run.event()
choice = (event.get("details") or {}).get("reply", {}).get("id", "")

if event.get("media"):
    item = event["media"][0]
    if "path" in item:
        os.makedirs("receipts", exist_ok=True)
        shutil.copy(item["path"], f"receipts/{item['id']}")
        pyrunner_notify.reply("Thanks, we got your receipt.")
    else:
        pyrunner_notify.reply("Sorry, we couldn't open that file.")
elif choice == "orders":
    pyrunner_notify.reply(interactive={
        "type": "list", "body": "Which order?", "button": "Choose",
        "sections": [{"rows": [{"id": "order-1042", "title": "#1042", "description": "Shipped"}]}],
    })
elif choice.startswith("order-"):
    number = choice.removeprefix("order-")
    pyrunner_notify.reply(media={
        "kind": "document", "url": f"https://files.example.com/invoices/{number}.pdf",
        "filename": f"invoice-{number}.pdf", "caption": "Here is your invoice.",
    })
elif choice == "human":
    pyrunner_notify.send("Ops Alerts", f"WhatsApp support: {event['sender']['display_name']} wants a person.")
    pyrunner_notify.reply("Someone from the team will answer here shortly.")
else:
    pyrunner_notify.reply(interactive=MENU)
```

### Delivery status

Each send is recorded with WhatsApp's message id (shown as **Message id** in
the Message log and on the run's Deliveries card). As Meta's status callbacks
arrive, the channel's **Message log** (and a run's **Deliveries** card) moves
the message from *sent* to *delivered* to *read*; a late or repeated
callback never moves it back. When WhatsApp reports a message **failed**
after accepting it (for example the 24-hour window had closed, error
131047), the record turns *failed* with WhatsApp's reason; such a message
isn't retried.

Errors on the send itself: a rejected or expired token and a missing
permission fail at once; WhatsApp's rate limits (codes 4, 80007, 130429,
131056), its temporary errors (1, 2, 131000, 131016) and 5xx answers are
retried by the delivery ledger; any other error (an invalid number, a
template that doesn't exist) fails at once.

### Messaging limits

- **Business-initiated conversations** (templates you send first) are limited
  per rolling 24 hours, by tier. A new or unverified business starts low
  (250 unique customers); verifying the business and keeping a good quality
  rating moves the number up through Meta's tiers. Replies inside a window a
  customer opened don't count toward it.
- **Throughput** (messages per second) and **pair rate limits** (messages to
  one person in a short time) come back as rate-limit errors, which PyRun
  retries with backoff.
- Meta charges per template message by category (marketing, utility,
  authentication); service replies inside the window are free. Check Meta's
  current pricing and limits for your account in WhatsApp Manager.
- PyRun's own **daily send cap** and **daily reply cap** on the channel apply
  on top, so a failing schedule can't send a template every minute.

### Try it live through a tunnel

With the Meta test number, no credentials leave your machine except the
tunnel:

1. Run PyRun locally and open a tunnel to it, for example
   `cloudflared tunnel --url http://localhost:8000` (or `ngrok http 8000`).
   Add the tunnel's host name to `ALLOWED_HOSTS`, and open the console
   through the tunnel URL (the callback URL is built from the address you
   use).
2. Add the WhatsApp channel with the test number's ids and your token, turn
   Inbound on, and subscribe the callback URL in Meta as above.
3. From your own phone (added as a recipient on API Setup), message the test
   number: it lands in the approval inbox; approve yourself and message
   again: the script runs, or Py AI answers.
4. Send a notification: it arrives as text. Message log: *sent*, then
   *delivered* and *read* as you open it.
5. To see the template path, set the default template (Meta's test account
   has `hello_world`, which has no variable, so create your own with
   `{{1}}`), and send to a number that hasn't written in 24 hours.
