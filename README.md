<p align="center">
  <img src="assets/moebius.png" width="120" alt="Möbius" />
</p>

<h1 align="center">Möbius</h1>

<p align="center">
  An AI agent that builds the app it lives in — and gets better at it over time.
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <a href="https://hub.docker.com"><img src="https://img.shields.io/badge/Docker-single--container-2496ED?logo=docker&logoColor=white" alt="Docker"></a>
  <a href="#get-started"><img src="https://img.shields.io/badge/PWA-installable-5A0FC8?logo=pwa&logoColor=white" alt="PWA"></a>
</p>

<p align="center">
  <a href="#what-is-möbius">What is it?</a> &middot;
  <a href="#what-can-you-build">What can you build?</a> &middot;
  <a href="#get-started">Get Started</a> &middot;
  <a href="#skill-and-experience">Skill &amp; Experience</a> &middot;
  <a href="#license">License</a>
</p>

---

## What is Möbius?

Möbius starts as a blank canvas with a chat interface. You describe what you want, a hiking trail guide, a morning news feed, an interactive trip planner, and the agent builds it. You see it show up on your screen, right in front of you.

It works in the browser and installs on your phone like a native app (Android and iOS). Because it runs on *your* server, your data stays yours.

The agent can also change the interface itself: the theme, the layout, features in the shell. It edits the source and rebuilds live. Some changes appear instantly, others take a few seconds.

Check out [Get Started](#get-started) for one-click setup.

---

## What Can You Build?

Software shouldn't be one-size-fits-all. Möbius lets you describe what you want and builds it in front of you. Over time it picks up on your preferences and interests to be more helpful, and your data never leaves a server you control.

Some things we've built:

- **News aggregator** — runs on a schedule, searches the web, filters stories based on preferences that evolve over time
- **Stock market dashboard** — for a local exchange with no public API; the agent figured out how to scrape it
- **Finance tool** — upload statements, categorize spending, compute taxes from your phone
- **Period tracker** — built around how you actually want to use one
- **Learning companion** — uses AI to build a curriculum around any topic, with spaced repetition that adjusts as you improve
- **Drum machine** — turns voice samples into beats

Your health data, your finances, your habits, all private by default.

When you ask for something, the agent builds it as a small app, live, no reload. Each app can store data, run on a schedule, fetch from the web, and use AI on its own. But the agent isn't limited to apps. It can change anything: the interface, the layout, the server, itself. Apps are just the most natural way to grow what it can do.

---

## Get Started

### <a href="https://railway.com/deploy/mobius?referralCode=5TQuhr"><img src="https://railway.com/button.svg" alt="Deploy on Railway" height="28"></a>

Click **Deploy Now**, log in to Railway, and deploy. Once it's finished, go to **Settings → Networking → Generate Domain**. You'll get a URL like `xxx.up.railway.app`. Open it, and the setup wizard walks you through creating your account and signing in with Claude.

Bookmark `https://xxx.up.railway.app/recover`. If the UI ever breaks, that's where you fix it.

If you're logging in on your phone, save to home screen for the best experience.

To update, go to the same deployment's **Settings → Source → Check for updates**. Railway pulls the latest image and redeploys — chats, apps, credentials, and the agent's experience all survive.

### Deploy Self-hosted

**Requirements:** a Linux server with Docker, a domain name pointing to it, and a Claude subscription.

```bash
git clone https://github.com/hamzamerzic/mobius.git
cd mobius
cp .env.example .env
sed -i 's/^DOMAIN=.*/DOMAIN=your-domain.com/' .env
docker compose up -d
```

Caddy handles HTTPS automatically. Visit `https://your-domain.com` and the setup wizard takes it from there. On a headless server, copy the auth URL to a local browser to complete sign-in.

Bookmark `https://your-domain.com/recover`. If the UI ever breaks, that's where you fix it.

To update: `git pull && docker compose up -d --build`. Everything in `/data` survives rebuilds.

---

## Skill and Experience

The agent's context is split into two layers:

- **Skill** (`skill/agent-skill.md`): checked into git, ships with every deploy. Defines what the agent can do: how to build mini-apps, how to schedule tasks, how the platform works.
- **Experience** (`/data/shared/agent-experience.md`): lives on the volume, written by the agent. Documents what it has built, what worked, what broke and why.

Skill is static knowledge that deploys can update. Experience is instance-specific knowledge that accumulates over time and survives deploys. A well-seeded experience file means the agent knows how to document its own work from the first session.

---

## The Loop

Douglas Hofstadter described strange loops as systems that, by moving through levels, unexpectedly arrive back where they started. Gödel found one in arithmetic. Escher drew one in hands that draw each other. Bach wove them into canons that modulate through every key and come home.

Möbius does the same thing in code: the agent builds the interface it runs inside, accumulates experience about the system it inhabits, and uses that experience to build better things within it. Like the strip it's named after, there's no clear inside or outside, just one continuous surface.

---

## License

[MIT](LICENSE)
