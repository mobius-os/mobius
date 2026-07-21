# Skills

The Möbius skills manager: browse public skill catalogs (Anthropic Agent Skills,
the Hermes bundled/optional catalogs, and any source you add), see each skill's
summary, install with one tap, and manage what's installed. An embedded agent
chat at the bottom can search the ecosystem for you and install on your go —
its playbook is the platform's `finding-skills.md` seed skill.

- Installs land as `SKILL.md` directories under `/data/shared/skills/` via
  `POST /api/skills/install` (gated by this app's `manage_skills` permission);
  removal git-snapshots the bytes first.
- Catalog sources are app data (`sources.json`) — ask the agent to add another
  repo or list.
- GitHub browsing goes through `/api/proxy` unauthenticated (60 req/h per IP);
  fine for browsing, and the agent can fall back to its own tools when rate-limited.

## Destination

This app's canonical home is the `mobius-os/app-skills` catalog repo
(bootstrap-installed by the platform like the App Store). This in-platform copy
is the development source — push it to the catalog repo to activate the
bootstrap entry.

## Dev loop

In a dev instance, register once from a chat/shell:

```bash
cp -r app-skills /data/apps/skills
python "$SCRIPTS_DIR/register_app.py" skills \
  "Browse and install agent skills" /data/apps/skills/index.jsx
```

Then edit files in place; the watcher recompiles on save.
