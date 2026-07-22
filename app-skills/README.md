# app-skills — Skills

A [Möbius](https://github.com/mobius-os) catalog mini-app. Based on the
upstream `mobius-os/app-skills` read-only browser (v1.1.2, MIT — see LICENSE);
this v2 keeps its reading experience and adds the write half of the skills
story.

**Browse & read** — the list comes from `GET /api/skills`, so it shows every
skill shape (flat `<name>.md` and directory `<name>/SKILL.md`) with provenance
(built-in seed / agent-authored / app-owned / installed-from) and 30-day usage
counts. Tap a skill to read it as sanitized markdown (full markdown fetched
lazily from shared storage).

**Find (agent-first)** — the ✦ header button opens an agent chat with a
prefilled draft. The agent's playbook is the platform's `finding-skills.md`
seed skill: where to look, how to judge fit, the trust ritual (read a
third-party SKILL.md fully and summarize what it instructs before installing),
and the exact install API call.

**Catalog screen** — the ▤ header button opens a curated list of public repos
that host SKILL.md skills. One recursive git-trees call per source (through
`/api/proxy`) finds every skill — flat cards, no folder dead ends; summaries
prefetch in the background (raw-file fetches, no API rate cost). Cards install
via `POST /api/skills/install` (gated by this app's `manage_skills`
permission). Sources are app data (`sources.json`) — ask the agent to add a
repo.

**Uninstall** — install-provenance skills get a two-tap remove button in the
detail view (`DELETE /api/skills/<name>`; the server git-snapshots the bytes
first). Seeds, agent files, and app-owned skills keep their own lifecycles —
editing routes to the agent, as in v1.

## File layout

| File | Role |
|------|------|
| `index.jsx` | Default-export React component: list, detail, catalog screen, all UI/state. |
| `domain.js` | Dependency-free core: parsing, link classification, nav state machine, provenance/usage formatting, content-path selection. |
| `catalog.js` | Dependency-free catalog core: source list, tree-scan filtering, summary parsing, prefetch pool. Network is injected. |
| `test/` | Regression tests for both cores (`npm test` → `node --test`). |
| `mobius.json` | App manifest (id, permissions incl. `manage_skills`, runtime deps). |

## Dev loop

In a dev instance, register once from a chat/shell:

```bash
cp -r app-skills /data/apps/skills
python "$SCRIPTS_DIR/register_app.py" skills \
  "Browse and install agent skills" /data/apps/skills/index.jsx
```

Then edit files in place; the watcher recompiles on save. Note that
`register_app.py` does not apply manifest permissions — grant `manage_skills`
through the platform when testing installs.

## Destination

Canonical home is the `mobius-os/app-skills` catalog repo; this in-platform
copy is the development source for a v2 proposal upstream.
