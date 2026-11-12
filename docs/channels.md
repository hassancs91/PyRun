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

---

## Telegram

Create a bot with `@BotFather`, add a channel with its token, message the bot,
then use **Find chat ID** on the channel's page to set the default chat. For
inbound, turn **Inbound** on: PyRun registers its webhook with Telegram
itself. Inbound needs a publicly reachable HTTPS instance.

---

## WhatsApp

A WhatsApp channel is one WhatsApp Business phone number. It sends text and
approved templates, receives messages (text, button and list replies,
locations, contacts, reactions; photos, documents, voice notes and videos as
metadata), and shows each message's delivery: sent, delivered, read or
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
Media are metadata only: PyRun doesn't download the file. Other message types
(system notices, orders, unsupported ones) are ignored.

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

### Delivery status

Each send is recorded with WhatsApp's message id. As Meta's status callbacks
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
