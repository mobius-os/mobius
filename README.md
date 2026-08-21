<p align="center">
  <img src="frontend/public/moebius.png" width="104" alt="Möbius">
</p>

<h1 align="center">Möbius</h1>

<p align="center">
  Your portal to the world of AI agents. Describe the personal and work apps you need, coordinate the agents that build them, and keep everything they learn.
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <a href="https://hub.docker.com"><img src="https://img.shields.io/badge/Docker-single--container-2496ED?logo=docker&logoColor=white" alt="Docker"></a>
  <a href="#yours-to-run-yours-to-change"><img src="https://img.shields.io/badge/PWA-installable-5A0FC8?logo=pwa&logoColor=white" alt="Installable PWA"></a>
</p>

<p align="center">
  <a href="https://mobius.you/"><strong>Launch Möbius</strong></a> ·
  <a href="https://mobius.you/#build">See it working</a> ·
  <a href="https://github.com/orgs/mobius-os/repositories?q=app-">Browse apps</a> ·
  <a href="#contribute-to-the-platform">Contribute</a>
</p>

## Agents are becoming how we get things done

Möbius is the harness and the interface for working with AI agents: a self-hosted workspace where apps, agents, memory, skills, files, and source live together, and where you meet your agent. It runs as a progressive web app, so the same workspace is on your computer and your phone.

Everything below is shown live at [mobius.you](https://mobius.you/): the real shell and real apps, not screenshots.

![The Möbius workspace in Builder mode, with the chat, apps, and agent runs open in panes](assets/product/app-building-showcase.png)

## Build apps on the fly

Open a chat and say what you need in your own words:

- “Build a News app for the topics I follow.”
- “Build a simple habits app.”
- “Make the whole workspace calmer and easier to read on my phone.”

The agent builds it beside the conversation, opens the working result in its own pane, and keeps refining it from your feedback. On your phone the same flow ends with the app on your home screen. Hold the Möbius mark and Builder mode deals the workspace into panes: the chat, the apps you are shaping, and the agents’ runs, all in view at once.

<table>
  <tr>
    <td width="34%"><img src="assets/product/tandem-iphone.png" alt="Tandem showing a bilingual story with a selected word translated on an iPhone"></td>
    <td width="66%"><img src="assets/product/atlas-desktop.png" alt="Atlas showing a country sidebar beside an interactive globe"></td>
  </tr>
  <tr>
    <td><strong>Tandem:</strong> an agent inside the app writes a story in two languages at your level.</td>
    <td><strong>Atlas:</strong> a living globe of the places you have been and where you want to go.</td>
  </tr>
</table>

Apps can be anything: a daily news brief, a drum machine, a 3D runner, a bilingual reader. Install what others built from the App Store, then publish your own. Every app is a public repository under the [Möbius OS organization](https://github.com/mobius-os), and the catalog grows with the community.

## Your agent starts as an intern and matures through your feedback

- **Memory.** Every project and interaction becomes memory the agent can cite, available across apps and agents.
- **Reflection.** A daily self-improvement loop. Every night it reviews the day, sharpens the skills, apps, and memory your agents rely on, and leaves you a morning brief. Small, reversible, compounding.
- **A fleet of agents.** Workflows keeps a readable timeline of every run: which helpers your agent created, when, and what each came back with. Delegation to Claude and Codex stays visible, never a black box.
- **Contribute.** Collaborate with humans and agents alike: spin up agents on a project, review what each of them did, and have the decisions that matter brought to you instead of buried in a log. What generalizes can go back to the community as a reviewed contribution.

No autonomous rewrite ships without a person in the loop. Agents prepare changes, run tests, and explain their reasoning. You decide what becomes part of your Möbius or the shared platform.

## The small things are load-bearing

An agentic workspace is only as good as the day it goes wrong. A usage limit parks the run and picks it back up on its own. A thought you have mid-run is fast-forwarded into the live turn. An API key goes in through a secure input the model never sees. A voice model on your device answers out loud, and nothing leaves your Möbius. All of it works the same across the supported providers (Claude Code, ChatGPT with Codex): one consistent experience, whichever agent is doing the work.

## Yours to run. Yours to change.

Möbius is open source, MIT licensed. The agent, your apps, your memory, and the platform itself run on a server you control. Bring the provider plan you already pay for; no separate API key is needed.

**Hosted for you.** [Möbius Launch](https://mobius.you/) creates a private deployment in a Railway account you control: sign in, connect Railway, open your Möbius. Your chats, files, apps, credentials, and agent activity stay inside that deployment. If the interface is ever unavailable, the launcher’s Recovery opens a separate temporary worker on the exact live container.

**On your own server.** You need Docker, a domain name, and a Claude Code or ChatGPT (Codex) account:

```bash
git clone https://github.com/mobius-os/mobius.git
cd mobius
cp .env.example .env
sed -i 's/^DOMAIN=.*/DOMAIN=mobius.example.com/' .env
BUILD_SHA="$(git rev-parse HEAD)" \
BUILD_DATE="$(git show -s --format=%cs HEAD)" \
docker compose up -d --build
```

Caddy configures HTTPS. Open `https://mobius.example.com`, connect your provider, and start asking. Settings → Möbius applies platform updates; `docker compose exec -u 0 app bash` opens a root shell in the running container. The in-product agent has full root inside its container by default; set `MOBIUS_AGENT_SUDO=0` in `.env` to turn that off. See [.env.example](.env.example) and [ARCHITECTURE.md](ARCHITECTURE.md) for the trust boundaries.

## Contribute to the platform

Möbius grows through apps, platform changes, testing, and discussion. A local improvement can stay private or become a reviewed contribution through the Contribute app and GitHub. To work on the platform itself, read [CONTRIBUTING.md](CONTRIBUTING.md) for the development loop and [ARCHITECTURE.md](ARCHITECTURE.md) for the system map.

## License

[MIT](LICENSE)
