# Changelog

All notable changes to the Brand Tracker plugin are documented here. This
project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- The tracker script uses PyRun's **Skip** concurrency policy (PyRun 2.5)
  instead of the plugin's own "a run is already in progress" check. The check
  only covered the page's Run now button; the policy covers the weekly schedule
  too, so a scheduled run can no longer overlap a manual one. A Run now while a
  run is going still shows "A run is already in progress.", and the skipped
  attempt is listed on the script's runs. An install saved before this change
  gets the policy on its next Run now (or settings save).

## [1.1.2] - 2026-10-23

### Security
- The email digest put scraped titles, URLs and snippets into its HTML unescaped;
  they are now escaped, and a link is only rendered for http(s) URLs.
- The public report share is bound to the workspace its data lives in (the
  default one) and is refused from another workspace with a message, so it is
  always listed (and revocable) under Settings → API Tokens there.

## [1.1.1] - 2026-10-23

### Fixed
- The shareable public report showed its template comment ("Public, script-free
  report — served under a strict CSP …") as text at the top of the page: a
  multi-line `{# #}` is not a Django comment. It is now a `{% comment %}` block.

## [1.1.0] - 2026-07-15

External read-only API via PyRun's plugin API seam (SDK 2.3, PyRun ≥ 1.16).

### Added
- **External API** at `/api/v1/plugins/brand_tracker/…` (plugin-scoped API
  token required; create one under Settings → API Tokens):
  - `mentions/` — the deduped mention feed, filterable by
    `keyword` / `source` / `sentiment` / `since` and paginated
    (`page` / `page_size`, max 100).
  - `stats/` — window/all-time totals, per-keyword and per-source counters,
    and the recent run history.
- Manifest now declares the resources under `provides.api_resources`
  (`api: "2.3"`, `min_pyrunner: "1.16.0"`).
- **Shareable public report** — a "Share report" button on the dashboard
  publishes a read-only, script-free mentions report at a private
  `/p/<token>/` capability URL (no login). Revoking kills the link
  permanently; re-sharing issues a new URL. Manage links under
  Settings → API Tokens.

## [1.0.0] - 2026-06-28

First release — a self-provisioning PyRun plugin (SDK / `core.plugins.api`).

### Added
- One-form setup that idempotently provisions a managed Script, owner-scoped
  secrets, an owned DataStore, and a **weekly** schedule.
- **Multi-source tracking** with a pluggable source layer: Serper web + news
  (credit-bearing), Hacker News (Algolia) and Reddit (OAuth) — all free. Each
  keyword is searched as an exact phrase over the past week.
- **Canonical-URL dedup** across sources (https/www/m./AMP/utm + click-IDs
  collapse) so the same article is never reported twice; proper domain exclusion
  (subdomain-aware, not substring).
- **90-day retention** prune of the dedup index + feed, with a 2000-item cap.
- **First-run seed** — the first run fills the feed without emailing, so a fresh
  install never floods you.
- **Serper credit tracking** — a per-month counter (1 credit ≤10 results, 2
  above) with an optional monthly cap that pauses Serper while free sources keep
  running; a live cost estimate in the form.
- **Two-tab UI** (Mentions · Settings): a live mention feed with source pills +
  client-side filters, stat cards, a credit gauge, run history, **Run now / Stop**
  (`ScriptAPI.queue_run` / `cancel_latest_run`), a live status poller, and a
  **Test Serper key** button.
- **Optional AI enrichment** (source type + sentiment): `off` / Claude
  (platform `pyrunner_ai`) / OpenRouter (BYO key). Only new mentions, batched
  ≤25/call with a 100/run ceiling, and degrades silently when unavailable.
- **Optional email report** via Resend (the dashboard feed works with no email
  setup); operational failure alerts via PyRun's built-in notifications.
- Test suite (`tests.py`) over the engine (dedup/exclusion/retention/enrichment)
  and the web/provisioning surface, plus README **Troubleshooting**.

### Notes
- Requires PyRun **1.13.0+** (plugin SDK API `2.1`: run-lifecycle surface).
- Runs in a PyRun environment that has `requests` (add `claude-agent-sdk` for
  Claude enrichment).
- Tracks the indexed web + open communities; walled social (X/Instagram/TikTok/
  LinkedIn) is out of scope (the source layer is pluggable for a future paid
  provider).
