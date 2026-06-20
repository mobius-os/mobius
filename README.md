<p align="center">
  <img src="assets/moebius.png" width="120" alt="Möbius" />
</p>

<h1 align="center">Möbius</h1>

<p align="center">
  A self-hosted AI agent that builds the apps you need, edits its own interface, and improves itself overnight. Your data stays on your server.
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <a href="https://hub.docker.com"><img src="https://img.shields.io/badge/Docker-single--container-2496ED?logo=docker&logoColor=white" alt="Docker"></a>
  <a href="#get-started"><img src="https://img.shields.io/badge/PWA-installable-5A0FC8?logo=pwa&logoColor=white" alt="PWA"></a>
</p>

<p align="center">
  <a href="#what-is-möbius">What is it?</a> &middot;
  <a href="#batteries-included">Batteries included</a> &middot;
  <a href="#you-grow-it">You grow it</a> &middot;
  <a href="#apps-that-work-together">Apps that work together</a> &middot;
  <a href="#build-an-app">Build an app</a> &middot;
  <a href="#it-improves-itself-for-you">It improves itself for you</a> &middot;
  <a href="#how-the-agent-itself-gets-better">How the agent gets better</a> &middot;
  <a href="#get-started">Get started</a>
</p>

---

## What is Möbius?

Möbius is a personal AI agent you self-host. Chat sits on one side, a full-screen canvas on the other. Describe what you want and the coding agent builds it — a small app that runs in your browser and persists next to the chat. The agent is not limited to apps: it can also edit the interface it runs inside, the theme, the layout, the features in the shell, by editing the source and rebuilding live.

The unusual design decisions worth knowing upfront:

- **The agent can modify its own platform.** It has write access to the entire backend and frontend under `/data/platform`, with full git history. Every change is reversible. If the shell breaks, `/recover` resets it without touching your chats, apps, or data.
- **Crash-loop safety.** A broken shell or backend auto-falls back to the pristine copy baked into the container image, so the agent can make bold changes without taking the instance down.
- **Offline-first.** Mini-apps cache to your phone and keep working with no network. Writes queue locally and sync when you reconnect.
- **No API key.** It runs on Codex (free or paid ChatGPT plan) or Claude Code (any paid plan) via OAuth.
- **Single-owner, self-hosted.** This is not a SaaS product. It runs in one Docker container on a server you control. Your data never leaves it.

[Get Started](#get-started) has one-command setup.

---

## Batteries included

Möbius ships with a curated app store. Tap to install; each app is yours to use, edit, or rebuild.

<p align="center">
  <img src="assets/screenshots/store-catalog.png" width="720" alt="The Möbius app store: News, Workout, Atlas, Memory, LaTeX, and Reflection, each with its own icon and a one-line description." />
</p>

<sub>A curated starter pack ships in the catalog. <strong>News</strong> writes a morning digest on a schedule. <strong>Workout</strong> logs training in plain language, on-device. <strong>Atlas</strong> is a 3D globe of where you have been. <strong>Memory</strong> is a browsable graph of everything the agent has learned. <strong>LaTeX</strong> is an Overleaf-style editor with a real engine. <strong>Reflection</strong> reviews the day's work and leaves a one-page morning brief.</sub>

Every app is a public repo with a `mobius.json` and an `index.jsx`, open under [github.com/mobius-os](https://github.com/mobius-os). Installing means pasting a URL. Updating means pasting the same URL again — it patches the code and keeps your data. There is no submission queue.

---

## You grow it

Möbius starts small: a chat and a canvas. You grow it from there. Ask for a feature and the agent writes it, end to end, in the same conversation. I asked for file upload in one prompt — _"I'd like to send files and images along with my messages"_ — and got the endpoint, the schema, the drag-and-drop overlay, the paste handler, and the thumbnails, all in one chat.

<p align="center">
  <img src="assets/screenshots/upload-flow.gif" width="280" alt="The file-upload build animated in three steps: ask the agent, answer a few clarifying questions, then attach an image inline in the chat." />
</p>

<sub>One chat, from empty composer to working feature.</sub>

The same loop builds whole apps. Some of what it has built me:

- **News aggregator** that runs on a schedule, searches the web, and filters stories around preferences that evolve over time
- **Stock dashboard** for a local exchange with no public API, which the agent figured out how to scrape
- **Finance tool** to upload statements, categorize spending, and compute taxes from your phone
- **Learning companion** that builds a curriculum around any topic with spaced repetition that adjusts as you improve
- **Period tracker**, a habit log, a drum machine that turns voice samples into beats

<p align="center">
  <img src="assets/screenshots/apps-cycle.gif" width="280" alt="A few of the apps Möbius has built, ISS tracker, habit log, HN dashboard, earthquake map, flashcards deck" />
</p>

<sub>Each was a single prompt. The agent wrote the JSX, compiled it, mounted it, and the app lives in the same shell the chat does.</sub>

You can reshape the platform the same way. "Make it warmer." "Restyle the whole shell as a 1970s synth panel." The agent edits the CSS and the new look is live in seconds.

<p align="center">
  <img src="assets/screenshots/theme-switch.gif" width="240" alt="The same Möbius new-chat screen cycling through several themes the agent built, ending on a meme theme" />
</p>

<sub>The same new-chat screen across a range of looks, from a medieval manuscript to a deep-blue ambient theme to fully meme-worthy. Themes and layout changes go live immediately, no rebuild.</sub>

---

## Apps that work together

Möbius apps share a storage layer and a permission model. With your say-so, one app can read another's data. Ask for a dashboard that pulls your **Workout** log and your habit tracker into one view and the agent can build it. The cross-app compose flow is designed but not fully wired yet; tweaking individual apps and reading across two is what works today.

---

## Build an app

The usual path is to ask for one: describe it in chat and the agent writes the JSX, compiles it, and mounts it next to the conversation. The contract is open, so you can also write one by hand or fork an existing one.

An app is a `mobius.json` manifest and an `index.jsx`. The component receives `{ appId, token }` and persists through `/api/storage/apps/{appId}/...`. The full contract is in the seed skills the agent itself reads: [`backend/scripts/seed-skills/building-apps.md`](backend/scripts/seed-skills/building-apps.md) and [`backend/scripts/seed-skills/app-component-shapes.md`](backend/scripts/seed-skills/app-component-shapes.md). Every `mobius.json` field is in [`docs/mobius-json.md`](docs/mobius-json.md).

The whole starter catalog under [github.com/mobius-os](https://github.com/mobius-os) is working code to read or fork. Each `app-*` repo is one installable app. Install by pasting its repo URL; update by pasting the same URL again.

---

## It improves itself for you

The agent keeps a **Memory** — a knowledge graph of everything it has learned, separate from your chat transcripts. Notes link to related notes, the important ones are indexed, and the whole thing loads into the agent's context at the start of each session. Instead of re-reading ten old conversations to remember a gotcha, it reads one note.

Every night the **Reflection** agent wakes up and tends it. It merges duplicate notes, drops stale ones, surfaces patterns that span multiple builds, and looks for things worth suggesting to you in the morning. It also audits your instance — scheduled jobs that have been failing quietly, apps whose data is growing in ways that will bite later, theme rules that hurt contrast. It commits its changes to the same git history everything else uses, so a bad night's reorganization is recoverable.

The nightly loop is the same reflect-and-refactor cycle the developers run by hand when improving the agent. Reflection is that loop, scheduled, on your instance.

---

## How the agent itself gets better

Möbius improves through a self-improvement harness run during development. An outer agent watches the inner one build, asks it _why_ it made each decision (with the transcript still in context), and rewrites its instructions between sessions. A few things that surprised us running that loop:

1. Reading transcripts and patching the prompt stalls after a few rounds, because every rule added tends to surface a regression somewhere else. Asking the inner agent why it acted, with the transcript in front of it, produced more durable fixes than reasoning from outside did. An agent reflecting on its own session beats a bigger agent theorizing about it.
2. Confrontational prompts get binary compliance or defiance. Warm, curious framings get the model to push back on wrong premises and cooperate on correct ones.
3. Once the loop works, the bottleneck is no longer the model. It becomes the meta-goals you optimize for, which come from real users hitting real friction.

---

## It's yours

Möbius runs in a single Docker container you control. Your chats, apps, data, theme, and the agent's memory live on your server. The whole platform is tracked in git, so any bad change can be read back and undone.

If the UI ever breaks, `/recover` resets the shell without touching your chats, apps, or data. It renders from a server-side path the agent cannot edit, so it survives even a shell rewrite that hides everything else. Installs are atomic: a failed one restores the previous working version.

The trust model is explicit: this is single-owner software. The agent has full write access to the platform because it is your agent, on your server, and the undo chain is the safety net.

---

## Get started

### <a href="https://railway.com/deploy/mobius?referralCode=5TQuhr"><img src="https://railway.com/button.svg" alt="Deploy on Railway" height="28"></a>

Click **Deploy Now**, log in to Railway, and deploy. New accounts get a free month, then around $5/month. Once the deploy finishes, go to **Settings → Networking → Generate Domain** to get a URL like `xxx.up.railway.app`. Open it, and the setup wizard walks you through creating your account and connecting Codex or Claude.

Bookmark `https://xxx.up.railway.app/recover`. On your phone, save to home screen for the best experience.

To update: **Settings → Source → Check for updates**. Railway pulls the latest image and redeploys; chats, apps, credentials, and memory all survive.

### Deploy self-hosted

**Requirements:** a Linux server with Docker, a domain name, and a coding provider (Codex free or paid, or Claude Code paid).

```bash
git clone https://github.com/mobius-os/mobius.git
cd mobius
cp .env.example .env
sed -i 's/^DOMAIN=.*/DOMAIN=your-domain.com/' .env
docker compose up -d
```

Caddy handles HTTPS automatically. Visit `https://your-domain.com` and the setup wizard takes it from there. On a headless server, copy the auth URL to a local browser to complete sign-in.

Bookmark `https://your-domain.com/recover`.

To update: `git pull && docker compose up -d --build`. Everything in `/data` survives rebuilds. On boot the container detects that the rebuilt image is newer than the shell bundle it last served and refreshes `/data/shell/dist` from the new build, so the updated UI and CLI tooling come through on the next start. A shell you customized in `/data/shell/src` is left untouched — only the served build is refreshed; rebuild it (the in-product agent's rebuild step, or `npx vite build` in `/data/shell`) to fold your edits back in. `GET /api/version` reports both the image's build sha and the served shell's build sha for verifying an update landed.

---

## Contributing

Möbius is built to be extended — by its own in-product agent and by people. To work on the platform itself:

- **[ARCHITECTURE.md](ARCHITECTURE.md)** maps the system: the single-container layout, what each backend module and frontend component does, and where to make a given change.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** covers the dev loop: running it locally, the backend and frontend test suites, and the live-rebuild model.

To build a **mini-app** rather than work on the platform, just ask the in-product agent in a chat — it writes and installs the app for you. The authoring contract, if you'd rather write one by hand, lives in `backend/scripts/seed-skills/building-apps.md`.

---

## License

[MIT](LICENSE)
