# PyRun

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/hasanaboulhasan/pyrun)
[![Version](https://img.shields.io/badge/Version-2.0.0-green.svg)](https://github.com/hassancs91/PyRun/releases)
[![Discord](https://img.shields.io/badge/Discord-Join%20Server-5865F2?logo=discord&logoColor=white)](https://discord.gg/BjkmTn7XSd)

A self-hosted Python script automation platform. Upload a script, schedule it, monitor it — nothing else to configure.

**Docs:** [pyrun.ai](https://pyrun.ai) &nbsp;·&nbsp;
[Getting started](https://learnwithhasan.com/pyrunner/docs/?utm_source=github&utm_medium=readme&utm_campaign=PyRun&utm_content=docs) &nbsp;·&nbsp;
[Install it on a VPS with Coolify](https://learnwithhasan.com/guide/install-pyrunner-on-coolify/?utm_source=github&utm_medium=readme&utm_campaign=PyRun&utm_content=body)

## Features

- **Script Management** — Create, edit, and organize Python scripts from your browser
- **Flexible Scheduling** — Run scripts manually, at intervals, daily/weekly/monthly/yearly at specific times, or on any [cron expression](docs/scheduling.md) — all timezone-aware
- **Virtual Environments** — Isolated Python environments with custom pip packages per script
- **Run History & Logs** — Track every execution with stdout/stderr capture
- **Secrets Management** — Store encrypted environment variables and secrets
- **AI for Scripts** — Call AI from your scripts using your Claude subscription, an Anthropic API key, or other providers (Z.AI GLM, OpenRouter, local Ollama, any Anthropic-compatible endpoint) — with saved provider profiles and one-click switching
- **Workspace Brains** — Versioned knowledge with file browsing, tiered grants, reviewed AI proposals, cited context and optional semantic search ([Brain guide](docs/brain.md))
- **Unified REST and MCP** — Connect AI clients over MCP or call the same permission-scoped tools from REST, scripts and plugins; discover them from Connect and OpenAPI ([API guide](docs/api.md))
- **Vector collections** — Embedding providers, Local and Qdrant storage, shared filters and a credential-free script helper
- **Reviewed GitHub sync** — Pull repository changes into a Brain's inbox for review and export a revision to a dedicated branch
- **Notifications** — Get alerts via email, webhook, Telegram or WhatsApp on script completion/failure
- **Invite-Only Auth** — Admin-invited accounts with email + password login and email-based reset (no open registration)
- **Single Container** — Deploy with one Docker command

## Quick Start

### Using Docker Compose

```bash
# Clone the repository
git clone https://github.com/hassancs91/PyRun.git
cd PyRun

# Copy environment template
cp .env.example .env

# Start PyRun
docker compose up -d

```

Open `http://localhost:8000` in your browser.

### Using Docker Hub Image

```bash
# Generate the two required keys first (copy the output somewhere safe):
docker run --rm --entrypoint python hasanaboulhasan/pyrun:latest \
  -c "import secrets; print(secrets.token_urlsafe(50))"
docker run --rm --entrypoint python hasanaboulhasan/pyrun:latest \
  -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

docker run -d \
  --name pyrunner \
  -p 8000:8000 \
  -v pyrunner_data:/app/data \
  -v pyrunner_plugins:/app/plugins \
  --pids-limit 2048 \
  -e SECRET_KEY='<generated-secret-key>' \
  -e ENCRYPTION_KEY='<generated-fernet-key>' \
  -e DEBUG=False \
  -e ALLOWED_HOSTS=localhost \
  -e SECURE_SSL_REDIRECT=False \
  -e SESSION_COOKIE_SECURE=False \
  -e CSRF_COOKIE_SECURE=False \
  hasanaboulhasan/pyrun:latest
```

The three `False` overrides are for plain-HTTP access (like `http://localhost:8000`).
Without them, `DEBUG=False` redirects every request to HTTPS and marks cookies
`Secure`, so a plain-HTTP deployment is unreachable. If you serve PyRun behind
an HTTPS reverse proxy, remove them to keep the secure defaults.

`/app/data` holds the database, scripts, environments and secrets, and
`/app/plugins` holds installed plugins; keep both volumes. `--pids-limit` caps
how many processes the container can start: scripts run as root, which ignores
the per-run process limit, so this is what stops a fork bomb in a script from
taking down the server.

### Upgrading from 1.x

Download a backup first (**Settings → Backup**). The image is now
`hasanaboulhasan/pyrun` (1.x used `hasanaboulhasan/pyrunner`), so change the
image name when you update. Some things behave differently in 2.0: *Selected*
secret injection gives a script only the secrets attached to it, each script
runs in its own working folder, and datastore and plugin API tokens are stored
hashed. Read the [upgrade notes in the changelog](CHANGELOG.md#upgrade-notes-from-1x)
before you update. A Coolify Docker Compose resource also needs the 2.0
`docker-compose.coolify.yml` pasted in: Redeploy keeps the compose file the
resource was created with.

## Configuration

Copy `.env.example` to `.env` and configure:

| Variable | Default | Description |
|----------|---------|-------------|
| `SECRET_KEY` | **Required** | Django secret key (container exits if unset — see [.env.example](.env.example)) |
| `ENCRYPTION_KEY` | **Required** | Fernet key for encrypting stored secrets — save this somewhere safe |
| `DEBUG` | `False` | Debug mode (disable in production) |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Allowed hostnames |
| `Q_WORKERS` | `2` | Background task workers |

See [.env.example](.env.example) for all options.

## Locked out? Manage accounts from the shell

If you forget your password, get locked out after too many failed sign-ins, or
have no admin who can sign in, fix it from the server. No email setup is needed.

```bash
# Set a new password (prompts for it), sign out old sessions, clear the lockout
docker exec -it pyrunner python manage.py user reset-password you@example.com

# No terminal? Print a random password instead of prompting
docker exec pyrunner python manage.py user reset-password you@example.com --generate
```

| Command | What it does |
|---------|--------------|
| `user list` | Show every account, its role and last sign-in |
| `user reset-password <email>` | Set a new password, sign out its sessions, clear its lockout |
| `user create <email> [--admin]` | Create an account with a password, no invite needed |
| `user promote <email>` / `user demote <email>` | Give or remove admin rights (the last admin can't be demoted) |
| `user unlock <email>` | Clear a lockout without changing the password |

Outside Docker, run the same commands as `python manage.py user ...`.

From the console, an admin can also open **Users → Reset link** for an account:
it makes a single-use password reset link (valid 24 hours) to hand over
directly, and works with email turned off.

### Sign-in security

- **Lockout.** After `AXES_FAILURE_LIMIT` (default 5) failed sign-ins in a row
  for one account from one address, that pair is locked for 15 minutes. Behind
  a reverse proxy, set `RATELIMIT_TRUSTED_PROXY_DEPTH` so the lockout sees the
  real client address.
- **Forgot password** answers every address the same way and sends the email
  from the background worker, so it can't be used to find out which accounts
  exist. It allows 3 requests per email and 10 per client address per hour.
  The worker must be running for the email to go out.
- **Session length.** A console sign-in lasts `SESSION_COOKIE_AGE` seconds
  (default `1209600`, two weeks).
- **Sign-in alerts.** Each user can turn on an email after every sign-in, on
  the Change password page (off by default; needs email set up).
- **Security events.** Sign-ins, failed sign-ins, lockouts, password changes
  and resets, reset links and new API tokens are listed under
  **Users → Security** (admins only) and kept for 90 days.

## AI in your scripts

PyRun can run AI directly inside your Python scripts so your automations can
research, summarize, and reason. It works with **your own** account on any of
several providers — profiles are saved (keys encrypted) so you can switch the
active provider with one click:

| Provider | Credential | Notes |
|---|---|---|
| **Anthropic (Claude)** | Claude subscription token or API key | Full experience incl. **web search/fetch** tools |
| **Z.AI (GLM)** | Z.AI API key | GLM models via Z.AI's Anthropic-compatible endpoint; strong tool-calling |
| **OpenRouter** | OpenRouter API key | Hundreds of models; ids are namespaced (`deepseek/deepseek-chat`) |
| **Ollama (local)** | none needed | Local models, requires Ollama ≥ 0.14; from Docker use `http://host.docker.internal:11434` |
| **Custom** | your token | Any Anthropic-compatible endpoint (LiteLLM proxy, llama.cpp server, …) |

> **A note on subscription auth:** this is intended for *self-hosters using their
> own Claude subscription for their own automations* — equivalent to running
> Claude Code headless on your own machine. Anthropic's Agent SDK terms restrict
> *offering* claude.ai login to other people as part of a product/SaaS; don't use
> a shared or pooled subscription to serve multiple end-users.

### Setup

1. **Add a provider** under **Services → AI Provider**:
   - Pick a provider type — the endpoint URL and hints fill in automatically —
     then paste your credential and optionally set a default model.
   - For an Anthropic subscription token, run `claude setup-token` on a machine
     where you're logged into Claude, then paste the result.
   - **Save**, make sure it's the **active** provider, and hit **Test** —
     Anthropic runs a real web search; other providers run a real tool-call
     round-trip, so you also learn whether your chosen model handles tools
     reliably before wiring it into automations.
   - Tick **Enable AI for scripts** and save the settings.
2. **Install the SDK** into the Environment your script uses:
   Environments → *(your env)* → Packages → add `claude-agent-sdk`.
   (The Claude Code CLI itself ships with the PyRun Docker image.)

### Use it

```python
from pyrunner_ai import ask_claude

# Web search + fetch are on by default
answer = ask_claude("Search the web for today's top AI story and summarize it")
print(answer)

# Restrict tools, pick a model, add a system prompt
summary = ask_claude(
    "Summarize https://peps.python.org/pep-0008/",
    tools=["WebFetch"],
    model="claude-sonnet-4-6",
)

# Full details (tools used, cost, turns)
result = ask_claude("Research the latest Django release", raw=True)
print(result.text, result.tools_used, result.cost_usd)

# Stream the answer
from pyrunner_ai import stream_claude
for chunk in stream_claude("Write a short poem about automation"):
    print(chunk, end="", flush=True)

# Lean mode: only load the tools you ask for (cuts ~50k cached tokens/call)
answer = ask_claude("Search the web for today's AI news", lean=True)
```

Available tools: `WebSearch`, `WebFetch`, `Read`, `Glob`, `Grep`. File-writing
and shell tools (`Write`, `Edit`, `Bash`) are **off by default** for safety —
scripts run with full access to the PyRun host, so only enable those if you
fully trust the prompt.

Scripts always use the **active provider**. Two provider-specific caveats:
`WebSearch`/`WebFetch` are Anthropic server-side tools and don't exist on other
providers, and agentic tool-calling quality varies by model — the per-provider
**Test** button checks exactly that, and frontier-class or GLM-4.7+ models are
recommended for tool-heavy scripts and Py AI.

**A note on token counts:** an agentic call carries the agent's tool definitions
as context, sent once and **prompt-cached** — that's the large "cache tokens"
figure on the usage page, not your content. Pass `lean=True` to define only the
tools you requested and slash that overhead. The connection test always runs
lean.

## Tech Stack

- **Backend**: Django, django-q2
- **Frontend**: Tailwind CSS
- **Database**: SQLite (default) or PostgreSQL (`DATABASE_URL`)
- **Deployment**: Docker

## Requirements

- Docker Engine 20.10+
- Docker Compose v2.0+
- 1GB RAM minimum (2GB recommended)

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

<!-- lwh-footer -->

---

## 📘 The free book

Boring building blocks are what keep an app alive in production. The book has 47 of them.

**[Vibe Engineering Blocks](https://learnwithhasan.com/blocks/?utm_source=github&utm_medium=readme&utm_campaign=PyRun&utm_content=footer)** is my free 74-page book.
47 building blocks for shipping real apps with AI. One block per page, each with the exact
prompt to hand your AI.

Built by **[Hasan Aboul Hasan](https://learnwithhasan.com/?utm_source=github&utm_medium=readme&utm_campaign=PyRun&utm_content=footer)**. I build real products with AI and
write down exactly how.
[Guides](https://learnwithhasan.com/guides/?utm_source=github&utm_medium=readme&utm_campaign=PyRun&utm_content=footer) &nbsp;·&nbsp;
[YouTube](https://www.youtube.com/@HasanAboulHasan) &nbsp;·&nbsp;
[Community](https://learnwithhasan.com/community/?utm_source=github&utm_medium=readme&utm_campaign=PyRun&utm_content=footer)
