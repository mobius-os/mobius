<p align="center">
  <img src="assets/moebius.png" width="104" alt="Möbius">
</p>

<h1 align="center">Möbius</h1>

<p align="center">
  An open-source AGI app platform. Build the apps you need, shape the workspace around your life, and help useful work improve productivity for everyone.
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <a href="https://hub.docker.com"><img src="https://img.shields.io/badge/Docker-single--container-2496ED?logo=docker&logoColor=white" alt="Docker"></a>
  <a href="#launch-your-möbius"><img src="https://img.shields.io/badge/PWA-installable-5A0FC8?logo=pwa&logoColor=white" alt="Installable PWA"></a>
</p>

<p align="center">
  <a href="https://mobius.you/"><strong>Launch Möbius</strong></a> ·
  <a href="https://mobius-os.github.io/apps/">Browse apps</a> ·
  <a href="#ask-for-what-you-need">Ask your agent</a> ·
  <a href="#contribute-to-the-platform">Contribute</a>
</p>

## Build apps around the way you work

Möbius is a self-hosted workspace where your agent builds apps beside the conversation. Describe what you need, inspect the result, and keep the app in the same place where you use it.

You do not need to know how Möbius apps are built. Start with a community app or tell your agent what would make your work easier. It can create the missing piece, adapt an existing app, and keep refining it from your feedback.

<table>
  <tr>
    <td width="34%"><img src="assets/product/tandem-iphone.png" alt="Tandem showing a bilingual story with a selected word translated on an iPhone"></td>
    <td width="66%"><img src="assets/product/atlas-desktop.png" alt="Atlas showing a country sidebar beside an interactive globe"></td>
  </tr>
  <tr>
    <td><strong>Tandem:</strong> read generated stories in two languages at your chosen level.</td>
    <td><strong>Atlas:</strong> collect the places you have visited and save where you want to go next.</td>
  </tr>
</table>

Other apps can be as personal as the trip they support. Brazil 2026 keeps an itinerary, local phrases, weather, packing, and a journal together. News prepares a daily digest around the topics you care about.

## Ask for what you need

Open a chat and describe the outcome you want in your own words. For example:

- “Build me a simple meal planner that remembers our favourite recipes.”
- “Add a weekly view and quicker task entry to my planner.”
- “Make the whole workspace calmer and easier to read on my phone.”

Your agent takes care of building it, opens a working version beside the conversation, and checks it as it works. You can react to what you see and keep asking for changes. The result can stay private to your Möbius or, when it could help others, be prepared for review as a community contribution.

## Use the same workspace on phone and web

Möbius runs as a progressive web app (PWA). Your apps, files, chat, memory, and settings stay together across a computer and phone.

![Editor showing the same app project across web and iPhone](assets/product/editor-continuity.png)

## Personalize the whole platform

The workspace can change with you. Themes reshape the shell, Memory keeps durable context available, and Reflection reviews completed work for improvements worth carrying forward.

<table>
  <tr>
    <td width="36%"><img src="assets/product/memory-graph-iphone.png" alt="Memory showing connected notes on an iPhone"></td>
    <td width="64%"><img src="assets/product/themes.png" alt="Möbius in its default theme and a custom expressive theme"></td>
  </tr>
  <tr>
    <td><strong>Memory:</strong> connect facts, decisions, preferences, and projects.</td>
    <td><strong>Themes:</strong> change the full workspace, not one isolated app.</td>
  </tr>
</table>

## Grow an open-source AGI

Möbius is an open-source AGI app platform that grows with the needs of its users. Anyone can build an app or platform change for their own work. When an idea helps beyond one person, Contribute gives it a path back to the community. Shared apps and platform improvements can raise productivity for everyone.

<table>
  <tr>
    <td width="96" align="center"><img src="assets/product/memory-icon.png" width="72" alt="Memory app icon"></td>
    <td><strong>Memory</strong><br>Personalize the platform with context worth keeping.</td>
  </tr>
  <tr>
    <td width="96" align="center"><img src="assets/product/reflection-icon.png" width="72" alt="Reflection app icon"></td>
    <td><strong>Reflection</strong><br>Turn repeated friction into the next improvement.</td>
  </tr>
  <tr>
    <td width="96" align="center"><img src="assets/product/contribute-icon.png" width="72" alt="Contribute app icon"></td>
    <td><strong>Contribute</strong><br>Share apps and platform changes that can help others.</td>
  </tr>
</table>

Build for a real need, make it yours, improve what gets in the way, then share what generalizes. Community review can turn that work into a building block that makes the whole ecosystem more capable.

Möbius deliberately supports coding agents that can work across a real repository. Today, that means OpenAI Codex and Claude Code. The owner chat agent can edit the frontend and backend, while git history and `/recover` keep those changes reversible.

No autonomous rewrite ships without a person in the loop. Agents can prepare changes, run tests, and explain their reasoning. People still decide what becomes part of the shared platform.

## Start with the community catalog

The App Store includes tools for notes, tasks, skills, memory, reflection, development, news, health, and learning. Each app is a public repository under the [Möbius OS GitHub organization](https://github.com/mobius-os).

![The Möbius App Store](assets/product/app-store.png)

Install a community app from the catalog, then ask your agent to make it yours. Updates preserve the app's data and your local changes.

## Bring agent access

Möbius uses an agent account you already control. Connect one of these providers during setup:

- **OpenAI Codex**: sign in with a ChatGPT plan that includes Codex access. Usage limits depend on the plan.
- **Claude Code**: sign in with a supported Claude Code plan

Möbius uses provider sign-in, so the default setup does not require a separate API key.

## Launch your Möbius

[Möbius Launch](https://mobius.you/) creates a private deployment in a Railway account you control:

1. Sign in to Möbius Launch
2. Connect your Railway workspace
3. Review the deployment and open your Möbius instance

![Möbius Launch showing workspace health, included Railway credit, and live resource usage](assets/product/mobius-launch-deployment.png)

Your chats, files, apps, credentials, and agent activity stay inside that deployment. Möbius Launch stores only the account and infrastructure data needed to create and manage it.

### Deploy on your own server

Use a Linux server with Docker, a domain name, and Codex or Claude Code access:

```bash
git clone https://github.com/mobius-os/mobius.git
cd mobius
cp .env.example .env
sed -i 's/^DOMAIN=.*/DOMAIN=mobius.example.com/' .env
docker compose up -d
```

Caddy configures HTTPS. Open `https://mobius.example.com` and follow the setup wizard. Bookmark `/recover` before asking the agent to change the platform.

Update a self-hosted instance with:

```bash
git pull
docker compose up -d --build
```

Data under `/data` survives rebuilds.

To connect a full web service such as Tandoor, point a sibling DNS name at the same server. For example, use `services.mobius.example.com`, then set it as `MOBIUS_SERVICE_GATEWAY_ORIGIN` in `.env`. Caddy serves integrations below `/services/<slug>`, so you do not need wildcard DNS or a new record for each service. See [.env.example](.env.example) for setup and [ARCHITECTURE.md](ARCHITECTURE.md#app-execution-tiers) for the trust boundaries.

## Contribute to the platform

Möbius grows through apps, platform changes, testing, and discussion. A local improvement can stay private or become a reviewed contribution through the Contribute app and GitHub.

If you want to work on the platform itself, read [CONTRIBUTING.md](CONTRIBUTING.md) for the development loop and [ARCHITECTURE.md](ARCHITECTURE.md) for the system map.

## License

[MIT](LICENSE)
