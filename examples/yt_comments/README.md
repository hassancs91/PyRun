# YouTube Comments AI

AI-powered comment intelligence for your YouTube channel, as a self-provisioning
PyRun plugin. Connect a channel once; every day the plugin fetches new
comments in a single channel-wide API sweep, classifies them with your
instance's AI provider into your own tag taxonomy, scores sentiment, stores
everything in a managed Postgres database, and emails you batched urgent
alerts, testimonial finds and a digest. Connect the channel's Google account
and it also **replies for you** — drafts in your voice queued for approval, or guarded
auto-publish for the tags you trust it with.

## What you get

- **Comment inbox** — every comment (and reply), filterable by tag, video and
  status, with sentiment, the AI's reasoning, and a deep link that opens the
  exact comment on YouTube.
- **Urgent alerts** — comments the AI flags as urgent (frustrated viewers,
  time-sensitive issues) arrive as ONE batched email per run, never one per
  comment.
- **Testimonial collector, publish-graded** — praise worth featuring is
  gathered on its own tab, and a focused AI pass grades each one for
  publish-worthiness: **⭐ Feature** (a story with a specific outcome — real
  social proof), **Solid** (substantive praise), **Generic** ("great video").
  Filter by grade, export Markdown/CSV with the grade and a one-line why.
- **Archived avatars** — each testimonial author's profile picture is
  downloaded into your instance's object storage (Services → Object Storage)
  under a stable per-author URL, so you can publish testimonials on your own
  site with faces that never 404 and update automatically when the author
  changes their picture. Opt-out in Settings; needs an assets storage
  connection, and a **public base URL** on it for permanent hot-linkable
  links (otherwise links are presigned and expire).
- **Your taxonomy** — the tag list and descriptions are editable; the
  descriptions are literally the AI's instructions. `urgent`, `testimonial`,
  `question` and `spam` are built-in behaviors and always kept.
- **Daily digest** — counts by tag and totals after each run that found
  something new.
- **Weekly insights** — once a week (you pick the day) the AI clusters the
  week's repeated questions into **FAQ** entries (suggested answers) and
  **video ideas**, alongside a per-video sentiment trend vs the prior week.
  No AI provider? The email still reports counts and trends. Empty week?
  No email at all.
- **Reply automation (needs the YouTube connection)** — per-tag policy: *draft for approval* queues
  AI replies on the Replies tab (edit → approve posts immediately, or reject);
  *auto-publish* posts unattended behind hard guardrails. The **Reply Brain**
  (your voice, knowledge/links, rules) is injected into every draft.
- **Spam moderation (optional)** — comments the AI tags `spam` are sent to
  YouTube's held-for-review queue (`setModerationStatus`), capped per run.

## Auto-publish guardrails (why auto mode is safe to turn on)

- `urgent` and `spam` tags can **never** be auto-published — at most drafted.
- Auto replies may only contain links that appear in the Brain's Knowledge.
- Comments that themselves contain a link are never auto-replied.
- Length + refusal/meta-text checks; comments older than 14 days are excluded;
  a daily auto-post cap (default 10, per UTC day).
- Anything a guardrail rejects drops into the approval queue with the reason —
  it is demoted, never silently posted or dropped.
- One reply per comment, ever (database-enforced), each fully audited (draft,
  final text, actor, YouTube id).
- The first (seed) run never drafts, posts or moderates anything.

## Requirements

- PyRun **1.16.0+** with a **data server attached** (`PYRUNNER_DATA_DB_URL`
  — see the Databases documentation). The plugin refuses to provision without
  one; comments live in a real Postgres schema, not a key-value store.
- **Optional, for avatar archiving**: an assets connection under *Services →
  Object Storage* (any S3-compatible bucket). Without one the plugin simply
  skips avatars and everything else works. Set a public base URL on the
  connection (e.g. an R2 public domain) to make the exported avatar links
  permanent.
- An **AI provider** configured under *Services → AI Provider* (any of the
  supported providers — the plugin brings no AI key of its own). Optional:
  without it, comments are fetched and stored as *pending* and are classified
  once a provider is active.
- A **YouTube Data API v3 key** (Google Cloud Console → enable *YouTube Data
  API v3* → Credentials → API key). Read-only public data; the default 10,000
  units/day quota is far more than reading uses (a typical run costs 1–5
  units). Posting costs 50 units per reply/moderation — even 50 replies a day
  is only 2,500 units.
- An **Environment** with `requests`, `psycopg[binary]` and (for AI)
  `claude-agent-sdk`.
- **For reply automation only**: PyRun 2.5's **Connections** and a Google
  **OAuth client** (type *Web application*) from the same Cloud project, added
  under **Connections → OAuth apps** (that page shows the redirect URI to
  register). **Set the OAuth consent screen to "In production"**: apps left in
  *Testing* expire refresh tokens after 7 days and the connection dies weekly
  (the one-time "unverified app" warning is fine for personal use).

## Install

1. Upload the plugin zip under **Plugins**, activate it.
2. Open **YT Comments** in the sidebar → **Settings**.
3. Paste your channel (ID, `@handle`, or URL) + API key, pick the start date,
   review the tags, choose the environment and daily time → **Save**.
4. **Run now** to backfill. The first run seeds the archive silently — no
   alert emails, no replies — and later runs process only what's new.
5. (Optional, for replies) Add the Google OAuth app under Connections → OAuth
   apps, then **Connect Google** on the plugin's Settings tab with the account
   that owns the channel (or pick a Google connection you already have that
   allows YouTube). Teach the **Brain** your voice, then set per-tag reply
   policies.

**Upgrading from 0.5.x**: the plugin's own Google client and refresh token
(`YT_OAUTH_*` secrets) are no longer used. Add the same client under
Connections → OAuth apps (and its redirect URI on the Google client), connect
on the Settings tab, then save the settings once: that deletes the old secrets.

## How the fetch stays cheap

The plugin never walks your videos. One `commentThreads.list` call with
`allThreadsRelatedToChannelId` returns the newest 100 comment threads across
the whole channel per quota unit; the plugin pages down to its stored
watermark and stops. Replies come along for free (threads with more than 5
replies get a deep fetch). A crash-safe watermark guarantees comments are
never skipped: it only advances after a completed sweep.

The **first run** backfills from your start date, bounded by *Max fetch pages
per run* (default 30 pages ≈ 3,000 threads). On a big channel, raise the cap
if you want deeper history; the run log states clearly when the cap trimmed
the backfill.

## Honest limitations

- **Replies to old threads**: the channel-wide listing is ordered by
  thread creation time, so a new reply landing on a months-old comment thread
  may not be picked up. Replies to recent threads are captured.
- **Held-for-review comments aren't fetched.** Reads use the API key (stable
  and quota-cheap), which only sees published comments. Spam moderation acts
  on comments the AI tagged — comments YouTube already held are invisible to
  the plugin.
- **Replies post as the connected Google account.** Connect the channel owner
  (or its brand account): the card shows the account's email, not its channel,
  so check it's the right one.
- **A failed classification never guesses.** Comments the AI couldn't process
  stay `pending` and are retried on later runs.
- **If Google refuses the connection's refresh** (an expired *Testing* token, a
  changed password, revoked access), PyRun marks it expired; monitoring
  continues on the API key, approved replies wait, and the card offers
  Reconnect.
- **Avatars are other people's photos.** The plugin re-downloads an author's
  avatar whenever a newer comment shows it changed (staying on the refreshed
  side of YouTube's stored-data policy), but whether to publish a commenter's
  face next to their quote on your site is your editorial call.

## Managed resources

One Save provisions (all owned by `yt_comments`, removed with the plugin):

| Resource | Name | Purpose |
|---|---|---|
| Script | `YouTube Comments AI` | the daily analyzer |
| Secret | `YT_API_KEY` | your YouTube key (write-only in the form) |
| Connection grant | the plugin's Google connection (`youtube`) | optional, for replies; the connection stays in Connections when the plugin goes |
| Database | `yt_comments:data` | `comments` + `videos` + `replies` tables |
| DataStore | `yt_comments:state` | config + Brain + run history + live progress |
| Schedule | daily at your chosen time | |
| Storage objects | `avatars/<channel_id>.jpg` | archived testimonial avatars (in the plugin's own prefix on the assets connection; removed with "delete data") |

## Development

```bash
# Dev mode — live-edit without uploading (inside a PyRun checkout)
export DEBUG=True
export PYRUNNER_PLUGIN_DEV=/abs/path/to/examples/yt_comments
python manage.py runserver

# Validate + test
python manage.py plugin_doctor --path examples/yt_comments
python manage.py test core.test_yt_comments_plugin

# Package
cd examples && zip -r yt_comments.zip yt_comments -x '*/__pycache__/*'
```

MIT licensed. Built by [Hasan Aboul Hasan](https://learnwithhasan.com).
