# OAuth apps: Google, Microsoft 365, GitHub, Slack and Notion

An OAuth connection (sidebar → **Connections** → **Add connection** → Google,
Microsoft 365, GitHub, Slack or Notion) signs in to the provider with an
**OAuth app** you register with that provider yourself: PyRun never ships a
shared app or client secret. This page walks through each provider's app, and
Google's consent screen in particular. How scripts use a connection is in
`docs/run-context.md` (Connections).

## The OAuth app in PyRun

Open **Connections → OAuth apps** and pick the provider:

- **This workspace's app** is used for the workspace's new connections. Any
  member of the workspace can add or change it.
- **The instance default** (a superuser ticks "For every workspace") is used by
  every workspace that has no app of its own.

Each app has a **Client ID** and a **Client secret** (stored encrypted, never
shown again; scripts never get it) and, for Microsoft, the **Tenant**. A
connection remembers the app that issued its tokens and keeps refreshing with
it, even if the workspace adds its own app later. Deleting an app stops its
connections from refreshing; reconnect them with another app.

### The redirect URI

Every provider app registers one redirect (callback) URI, shown on the OAuth
apps page:

```
https://<your PyRun address>/oauth/callback/
```

PyRun builds it from the address you opened the console with, so register it
exactly as shown (scheme, host, port and the trailing slash). Behind a reverse
proxy, the proxy must pass the original `Host` and set `X-Forwarded-Proto:
https`, or PyRun shows an `http://` address the provider will refuse with
`redirect_uri_mismatch`.

Only your browser has to reach that address: the provider sends your browser
there after its consent screen, it never calls PyRun itself. A PyRun on your
own network works as long as the provider accepts the address (Google and
Microsoft take `http://localhost`; Slack wants `https://`). With no address
that works, use **Paste a token** on the connection instead.

### Connect or paste a token

On the connection's page, tick what it may access, then either:

- **Connect with <provider>**: PyRun shows a short "Opening <provider>..."
  page that takes you on to the provider's consent screen (press **Continue
  to <provider>** if it stays there), and saves the connection when you come
  back. The sign-in has 10 minutes and must finish in the browser that started
  it; PyRun uses PKCE where the provider supports it.
- **Paste a token**: a **refresh token** issued to the same OAuth app (PyRun
  refreshes it right away to check it), or an **access token** that doesn't
  expire. PyRun calls the provider's user info with it before saving, and names
  the account.

To ask for more access later, tick it and press **Reconnect**.

## Google

Google needs a Google Cloud project with the APIs turned on, a consent screen,
and an OAuth client.

1. In the [Google Cloud console](https://console.cloud.google.com/), create a
   project (or pick one).
2. **APIs & Services → Library**: enable each API your scripts call, for
   example the Google Sheets API, Gmail API, Google Drive API and Google
   Calendar API. A connection can hold a scope for an API that is off, but
   calls to it fail with `accessNotConfigured`.
3. **Google Auth Platform** (formerly the OAuth consent screen): fill in the
   app name and support email, then pick the audience:
   - **Internal** (only for a Google Workspace organization): any account in
     your organization can connect, with no verification and no token expiry.
   - **External**: any Google account. It starts in **Testing**: add your own
     account under **Test users**. In Testing, Google ends every refresh token
     **7 days** after it was issued: the connection turns **Expired** and its
     creator is emailed, and you Reconnect it. To keep tokens for good, press
     **Publish app** (In production).
4. **Verification.** A published External app that asks for *sensitive* scopes
   (Calendar, Sheets, Gmail send) shows a "Google hasn't verified this app"
   warning until Google verifies it; *restricted* scopes (Gmail read or modify,
   full Drive) also need Google's security assessment before the general public
   may use the app. For an app only you and a few people you know use, you can
   stay unverified: on the warning, choose **Advanced → Go to <app>**. Prefer
   the narrow scopes where they do the job: **Drive: files it made or opened**
   (`drive.file`) is not restricted, full **Drive** is.
5. **Clients → Create client → Web application**. Under **Authorized redirect
   URIs** add PyRun's redirect URI. Copy the client ID and secret into PyRun
   (**OAuth apps → Google**).

PyRun asks Google for offline access with a fresh consent each time, so every
Connect gives a refresh token, and always adds `openid email` so the
connection can name the account.

**No redirect URI your browser can reach?** Get a refresh token from Google's
[OAuth 2.0 Playground](https://developers.google.com/oauthplayground):

1. Add `https://developers.google.com/oauthplayground` to the client's
   authorized redirect URIs.
2. In the Playground, open the settings (gear), tick **Use your own OAuth
   credentials** and enter the same client ID and secret.
3. Enter the scopes (for example
   `openid email https://www.googleapis.com/auth/spreadsheets`), **Authorize
   APIs**, then **Exchange authorization code for tokens**.
4. Paste the **Refresh token** into the connection's **Paste a token** and save.

The 7-day Testing limit applies to a pasted refresh token too.

## Microsoft 365

1. In the [Microsoft Entra admin center](https://entra.microsoft.com/), open
   **App registrations → New registration**.
2. Pick who can sign in: your organization only (single tenant), any
   organization, or any organization plus personal Microsoft accounts.
3. **Redirect URI**: platform **Web**, PyRun's redirect URI.
4. **Certificates & secrets → New client secret**. Copy its **Value** (not the
   secret ID). Client secrets expire (6 to 24 months): add a new one and save
   it in PyRun before the old one runs out, or refreshes start failing.
5. **API permissions → Microsoft Graph → Delegated**: `User.Read`,
   `offline_access` and what the connection asks for (`Mail.ReadWrite` and
   `Mail.Send`, `Calendars.ReadWrite`, `Files.ReadWrite.All`). Some
   organizations need an admin to **Grant admin consent**.
6. In PyRun (**OAuth apps → Microsoft 365**) enter the application (client) ID
   and the secret value. **Tenant**: your directory (tenant) ID for a
   single-tenant app; leave it blank (`common`) otherwise.

Microsoft hands out a new refresh token on every refresh; PyRun stores it. A
refresh token unused for 90 days expires: a connection no script used for that
long has to be reconnected.

## GitHub

1. **Settings → Developer settings → OAuth Apps → New OAuth App** (or under an
   organization's settings).
2. **Authorization callback URL**: PyRun's redirect URI. The homepage URL can
   be your PyRun address.
3. **Generate a new client secret**, and copy the client ID and secret into
   PyRun (**OAuth apps → GitHub**).

An OAuth app's tokens don't expire until they are revoked, so there is nothing
to refresh. Without a redirect URI, paste a
[personal access token](https://github.com/settings/tokens) (fine-grained or
classic) as the connection's **Access token**; no OAuth app is needed for that.

## Slack

1. At [api.slack.com/apps](https://api.slack.com/apps), **Create New App →
   From scratch**, in the Slack workspace scripts should post to.
2. **OAuth & Permissions → Redirect URLs**: add PyRun's redirect URI (Slack
   wants `https://`).
3. **Basic Information → App Credentials**: copy the client ID and client
   secret into PyRun (**OAuth apps → Slack**).

Connect installs the app in the Slack workspace with the bot scopes the
connection asks for (`chat:write` to post); scripts get the bot token. Invite
the bot to a channel before posting there.

Without a redirect URI, add the bot scopes under **OAuth & Permissions → Bot
Token Scopes**, press **Install to Workspace**, and paste the **Bot User OAuth
Token** (`xoxb-...`) as the connection's **Access token**. It doesn't expire
unless you turn on token rotation; with rotation on, use Connect (or paste the
refresh token) so PyRun can refresh it.

## Notion

Notion keeps its integrations at
[app.notion.com/developers/connections](https://app.notion.com/developers/connections)
(**Developer tools → Developer connections**). **New connection** asks for a
name and an **Authentication method**:

- **OAuth (Connect).** A user-scoped connection (formerly a "public
  integration"). In the connection's OAuth settings, add PyRun's redirect URI
  and copy its OAuth client ID and secret into PyRun (**OAuth apps →
  Notion**). On Connect, Notion asks which pages the connection may use.
- **API token (paste).** A workspace-scoped static token (formerly an
  "internal integration"). Copy its secret (`ntn_...`) and paste it as the
  PyRun connection's **Access token**; no OAuth app is needed. Then share each
  page or database with it in Notion (the page's menu → **Connections**).

What it may do (read, update, insert content) is set on the Notion
connection in Notion, not on the PyRun connection.
