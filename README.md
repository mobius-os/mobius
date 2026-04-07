<p align="center">
  <img src="assets/moebius.png" width="120" alt="Möbius" />
</p>

<h1 align="center">Möbius</h1>

<p align="center">
  An AI agent that builds the app it lives in — and gets better at it over time.
</p>

<p align="center">
  <a href="#what-is-möbius">What is it?</a> &middot;
  <a href="#what-can-you-build">What can you build?</a> &middot;
  <a href="#skill-and-experience">Skill &amp; Experience</a> &middot;
  <a href="#get-started">Get Started</a> &middot;
  <a href="#mini-apps">Mini-Apps</a>
</p>

---

## What is Möbius?

Möbius starts as a blank canvas with a chat interface. You describe what you want — a habit tracker, a morning news feed, a budget dashboard, a little game — and the agent builds it. You see it show up on your screen, right in front of you.

It works in the browser and installs on your phone like a native app (Android and iOS). Because it runs on *your* server, your data stays yours.

The agent can also change the interface itself: the theme, the layout, features in the shell. It edits the source and rebuilds live — some changes appear instantly, others take a few seconds.

Under the hood, the "agent" is the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) running as a subprocess inside the Docker container. When you send a message, the backend spawns a `claude` process, streams its output back to your browser, and saves the result.

**What you need:** a [Claude](https://claude.ai) subscription (Pro or higher) and somewhere to host it. See [Get Started](#get-started) for one-click deployment.

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

## Skill and Experience

The agent's context is split into two layers:

- **Skill** (`skill/agent-skill.md`) — checked into git, ships with every deploy. Defines what the agent can do: how to build mini-apps, how to schedule tasks, how the platform works.
- **Experience** (`/data/shared/agent-experience.md`) — lives on the volume, written by the agent. Documents what it has built, what worked, what broke and why.

Skill is static knowledge that deploys can update. Experience is instance-specific knowledge that accumulates over time and survives deploys. A well-seeded experience file means the agent knows how to document its own work from the first session.

---

## Get Started

### Railway (one click)

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/mobius?referralCode=5TQuhr)

Once deployed, open the provided URL. The setup wizard walks you through creating your account and signing in with Claude.

### Self-hosted

**Requirements:** a Linux server with Docker, a domain name pointing to the server (A record), and a Claude subscription.

```bash
git clone https://github.com/hamzamerzic/mobius.git
cd mobius
cp .env.example .env
sed -i 's/^DOMAIN=.*/DOMAIN=your-domain.com/' .env
docker compose up -d
```

Visit `https://your-domain.com`. The setup wizard walks you through creating your account and signing in with Claude. On a headless server, copy the auth URL to a local browser to complete sign-in.

### Updates

```bash
git pull && docker compose up -d --build
```

Everything in `/data` survives rebuilds. Bookmark `/recover` — it's your lifeline if the UI breaks.

---

## Mini-Apps

Each app the agent builds is a React component compiled on the server and rendered in a sandboxed iframe. Mini-apps:

- **Persist data** across sessions via a simple storage API
- **Run on a schedule** — the agent can set up cron jobs that update the app automatically
- **Fetch external data** server-side, bypassing browser CORS restrictions
- **Have a built-in AI** — each app can stream Claude responses for chat, analysis, or tool use
- **Inherit your theme** — change the shell's colors and all apps update instantly

You see it appear live — no reload, no deploy.

---

## Security

Single-owner, self-hosted. Your data stays on your server. Mini-apps run in sandboxed iframes with scoped tokens that can't access auth, settings, or chat history. See [SECURITY.md](SECURITY.md) for the full model.

If something breaks, visit `/recover` to rebuild the interface, download a backup, or reset.

---

## The Loop

Douglas Hofstadter described strange loops as systems that, by moving through levels, unexpectedly arrive back where they started. Gödel found one in arithmetic. Escher drew one in hands that draw each other. Bach wove them into canons that modulate through every key and come home.

Möbius does the same thing in code: the agent builds the interface it runs inside, accumulates experience about the system it inhabits, and uses that experience to build better things within it. Like the strip it's named after, there's no clear inside or outside — just one continuous surface.

---

## License

[MIT](LICENSE)
