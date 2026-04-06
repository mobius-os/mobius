<p align="center">
  <img src="assets/moebius.png" width="120" alt="Möbius" />
</p>

<h1 align="center">Möbius</h1>

<p align="center">
  An AI agent that builds the app it lives in — and gets better at it over time.
</p>

<p align="center">
  <a href="#what-is-möbius">What is it?</a> &middot;
  <a href="#skill-and-experience">Skill and Experience</a> &middot;
  <a href="#what-can-you-build">What can you build?</a> &middot;
  <a href="#get-started">Get Started</a> &middot;
  <a href="#development">Development</a>
</p>

---

## What is Möbius?

Möbius starts as a blank canvas with a chat interface. You describe what you want — a habit tracker, a morning news feed, a budget dashboard, a little game — and the agent builds it. You see it show up on your screen, right in front of you.

It works in the browser and installs on your phone like a native app (Android and iOS). Because it runs on *your* server, your data stays yours.

The agent can also change the interface itself: the theme, the layout, features in the shell. It edits the source and rebuilds live — some changes appear instantly, others take a few seconds.

Under the hood, the "agent" is the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) running as a subprocess inside the Docker container. When you send a message, the backend spawns a `claude` process, streams its output back to your browser, and saves the result.

**What you need:** a [Claude](https://claude.ai) subscription (Pro or higher) and somewhere to host it. Other AI providers could be added if there's interest — contributions are welcome.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.app/template/new?template=https://github.com/hamzamerzic/mobius)

One click gets you a running instance with HTTPS. Add a persistent volume mounted at `/data` in Railway's service settings to keep your data across deploys. Or self-host on a $5/month VPS — see [Get Started](#get-started).

---

## Skill and Experience

The agent's context is split into two layers:

- **Skill** (`skill/agent-skill.md`) — checked into git, ships with every deploy. Defines what the agent can do: how to build mini-apps, how to schedule tasks, how the platform works. Updated by the developer, applies immediately to new chats.
- **Experience** (`/data/shared/agent-experience.md`) — lives on the volume, written by the agent. Documents what it has built, what worked, what broke and why. The seed file (`backend/scripts/seed-agent-experience.md`) establishes the format and teaches the agent how to maintain the file — what's worth recording, how to structure entries so future sessions can act on them.

The split matters because skill is static knowledge that deploys can update, while experience is instance-specific knowledge that accumulates over time and survives deploys. A well-seeded experience file means the agent knows how to document its own work from the first session.

---

## What Can You Build?

You probably have an app you wish existed — one that would be perfect if it just did this one thing differently, or tracked the thing you actually care about instead of what the developer assumed. Möbius lets you just build it. And unlike one-shot AI tools that generate something and stop there, what you build here **sticks around** — persistent storage you own, scheduled tasks, shared data between apps, embedded AI. Things get more useful the longer you use them.

Some ideas, to give you a feel:

A **habit tracker** built around your actual goals, not someone else's categories. Start with something basic, then come back and ask the agent to add streaks, gamify it, send you a weekly score. It grows with you. Or a **learning companion** — spaced repetition, your own curriculum, a progression system with challenges tailored to how you actually learn. Study for an exam, pick up a language, go deep on a subject.

A **morning briefing** that runs as a cron job every day, fetches what you care about, cross-references your other apps (habit streak at risk, budget running low), and sends a push notification before you're out of bed.

A **finance tracker** that you add to each week — one app for transactions, a separate one for tax estimates reading the same data, a third for subscription renewals. The agent knows all three exist and can connect them when you ask for something new. Or an entire **health picture** — workouts, sleep, and energy levels all saving to shared storage, with a weekly synthesis that finds patterns across all of it. Your data, on your server.

A **work tool** shaped around your actual workflow — your client names, your process, your terminology — with an AI chatbot inside that can answer questions from real stored data. The kind of thing where every off-the-shelf product gets close but never quite fits.

You start with one small app. A month later you have a little ecosystem of tools that know about each other, shaped entirely by your ideas and your preferences. Half the fun is letting it grow — coming back, extending things, connecting apps that didn't talk to each other yet. The agent gets better at it with every session.

---

## Get Started

**What you need:**
- A Linux server with Docker (2 GB RAM is enough — a $5 VPS works fine)
- Docker Engine 20.10+ with Compose V2 (`docker compose`, not `docker-compose`)
- Ports 80 and 443 open on your server's firewall
- A domain name (required for HTTPS and mobile PWA install — ~$10/year)
- A Claude subscription (Pro or higher)

> **No domain?** You can run without Caddy over HTTP — the app works as a web app but won't install as a PWA on mobile. Set these in `.env`:
> ```
> DOMAIN=
> FRONTEND_ORIGIN=http://YOUR_SERVER_IP:8000
> ```
> Then start only the app service (skip Caddy):
> ```bash
> docker compose up -d app
> ```

```bash
git clone https://github.com/hamzamerzic/mobius.git
cd mobius
cp .env.example .env
# Set DOMAIN to your domain name
# Set SECRET_KEY (at least 32 chars): python3 -c "import secrets; print(secrets.token_hex(32))"
# Or leave SECRET_KEY blank — one will be auto-generated on first start
docker compose up -d
```

Visit `https://your-domain`. The setup wizard walks you through creating your account and signing in with Claude. On a headless server, copy the auth URL to a local browser to complete sign-in.

Bookmark `/recover` — it's your recovery lifeline if the UI breaks.

### Existing Caddy setup

```bash
cp docker-compose.override.example.yml docker-compose.override.yml
# Edit the network name to match yours
docker compose up -d
```

Add to your Caddyfile:

```
mobius.example.com {
    encode gzip
    reverse_proxy mobius:8000
}
```

### Updates

```bash
git pull && docker compose up -d --build
```

Everything in `/data` — your database, built apps, credentials, experience file, theme — survives rebuilds.

> **Update not showing?** If the agent has rebuilt the shell UI, `/data/shell/dist/` takes priority over the freshly deployed code. To pick up the new version:
> ```bash
> docker exec mobius rm -rf /data/shell/dist && docker restart mobius
> ```
> Or propagate your change through the agent's build — see `CLAUDE.md` for details.

> **Back up your data.** The `mobius_app_data` Docker volume contains everything: chats, mini-apps, credentials, and theme. Running `docker volume rm` on it is **irreversible**. Download a backup from `/recover` → "Download backup" before any destructive operation.

## Security notes

**JWT storage:** Auth tokens are stored in `localStorage` for simplicity. This is an accepted trade-off for a single-owner self-hosted app — the attack surface is your own browser on your own device. If you are security-sensitive, use a dedicated browser profile for Möbius access.

**Session duration:** Tokens expire after 30 days. This is an intentional choice for a single-owner app — you shouldn't need to log in frequently on your own server.

### Recovery

If something breaks, go to `/recover` — a separate password-protected page (same password as your login) that lets you rebuild the shell from the original source, download a backup of all your data, or perform a factory reset. It works even if the React app is broken, since it's a static HTML page served directly by the backend.

**Install on mobile:** On Android, open the site in Chrome and tap "Add to Home Screen" from the browser menu. On iOS, open in Safari and tap the Share button → "Add to Home Screen". HTTPS is required for installation.

---

## Mini-Apps

Each app the agent builds is a React component compiled on the server and rendered in a sandboxed iframe. Mini-apps:

- **Persist data** across sessions via a simple storage API
- **Run on a schedule** — the agent can set up cron jobs that update the app automatically
- **Fetch external data** server-side, bypassing browser CORS restrictions
- **Inherit your theme** — change the shell's colors and all apps update instantly

You see it appear live — no reload, no deploy.

---

## Development

Single Docker container: FastAPI serves the API and frontend, the Claude CLI runs as a subprocess per chat message, esbuild compiles JSX on the fly, SQLite stores everything on the `/data` volume.

```
backend/app/
├── chat.py        spawns the CLI, streams events, saves to DB
├── broadcast.py   per-chat in-memory event bus (decouples CLI from SSE)
├── compiler.py    esbuild wrapper: JSX string → ES module
├── providers.py   CLI adapters (Claude, extensible)
└── routes/        auth, apps, chats, ai, storage, recover

frontend/src/
├── App.jsx        setup → login → shell
├── Shell/         navigation state, logo bar
├── Drawer/        chat history, app list
├── ChatView/      block-memoized markdown, typewriter streaming
└── AppCanvas/     sandboxed iframe for mini-apps
```

```bash
cp .env.example .env   # fill in DOMAIN and SECRET_KEY
docker compose up -d --build
docker compose logs -f
```

Rebuild after changes with `docker compose up -d --build`. Everything in `/data` persists across rebuilds.

See [CLAUDE.md](CLAUDE.md) for architecture details, streaming gotchas, and notes on things that were hard to get right.

---

## Troubleshooting

**TLS certificate not issuing**
Check that ports 80 and 443 are open on your server's firewall, and that your domain's DNS A record points to the server. Run `docker compose logs caddy` to see Let's Encrypt errors.

**Container won't start**
Run `docker compose logs app` to see the startup error. Common causes: `SECRET_KEY` not set or too short, `DOMAIN` left blank, port 8000 already in use.

**Claude auth not persisting after rebuild**
CLI credentials are stored at `/data/cli-auth/claude/`. Check that the `/data` volume is mounted correctly with `docker volume ls`. If credentials are missing, go through the setup wizard again or copy `.credentials.json` manually.

**UI broken after the agent made changes**
Visit `/recover` — a password-protected recovery page (same password as your login). Use "Restore interface" to rebuild the shell from the original source without touching your chats, apps, or data.

**Update not visible after `git pull && docker compose up -d --build`**
See the update warning above. The agent's shell build at `/data/shell/dist/` takes priority. Clear it with `docker exec mobius rm -rf /data/shell/dist && docker restart mobius`.

---

## The Loop

Douglas Hofstadter described strange loops as systems that, by moving through levels, unexpectedly arrive back where they started. Gödel found one in arithmetic. Escher drew one in hands that draw each other. Bach wove them into canons that modulate through every key and come home.

Möbius does the same thing in code: the agent builds the interface it runs inside, accumulates experience about the system it inhabits, and uses that experience to build better things within it. Like the strip it's named after, there's no clear inside or outside — just one continuous surface.

Whether that's a profound observation or just a fun excuse for the name is left as an exercise for the reader.
