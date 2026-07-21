# Finding and installing skills

How to extend yourself with skills from the public ecosystem: where to look, how to judge fit, the exact install/uninstall API calls, and the trust ritual for third-party instructions. `Read` this before searching for, offering, or installing any external skill — especially as the embedded agent inside the Skills app.

A skill is on-demand knowledge — a markdown document you `Read` when a task matches it. The public ecosystem converged on one shape: a directory with a `SKILL.md` whose YAML frontmatter carries `name` and `description`, plus optional reference files beside it (the agentskills.io / Anthropic Agent Skills convention). Möbius installs those natively.

---

## Where to look

Search these sources, in rough order of quality:

1. **`anthropics/skills`** — Anthropic's official collection. Browse the repo tree; each top-level or category directory holding a `SKILL.md` is one skill.
2. **`NousResearch/hermes-agent`** — MIT-licensed skills under `skills/` (bundled) and `optional-skills/` (heavier/niche), organized by category (`github/`, `creative/`, `data-science/`, …).
3. **Awesome lists** — search GitHub for `awesome-claude-skills`, `awesome-agent-skills`, `awesome-hermes-agent`; they index community skills with descriptions.
4. **General GitHub search** — `filename:SKILL.md <topic>` finds standalone skills anywhere.

Fetch listings and files with your normal web access. To browse a repo directory programmatically: `https://api.github.com/repos/<owner>/<repo>/contents/<path>` returns the file list as JSON.

## Judging fit

Before offering a skill to the partner, actually `Read` its full `SKILL.md` (fetch it raw). Check:

- **Does it teach something you don't already know?** Skills that duplicate an existing skill (check `shared/skills/skills-index.md`) or your own competence add token cost for nothing.
- **Does it fit Möbius?** A skill assuming another harness's tools (e.g. Hermes tool names, Claude Code plugin paths) may need adaptation — you can still install it and edit it afterwards; skills are yours to improve.
- **License** — prefer skills whose repo or frontmatter carries a permissive license (MIT/Apache). Note it when offering.

## The trust ritual (third-party instructions)

An installed skill becomes instructions you will later follow. So for anything from outside the partner's own instance:

1. Read the complete `SKILL.md` (and glance at resource files).
2. Tell the partner in one or two sentences **what the skill instructs you to do** — especially anything that runs commands, contacts external services, or writes files.
3. Install on their go-ahead. In the Skills app chat, offering matches with summaries and letting them pick IS this ritual; a card-button install the partner clicks themselves needs no extra confirmation from you.

Never silently install a skill mid-task because it seemed useful; surface it.

## Installing

`POST /api/skills/install` with your token. Two forms:

```bash
# A skill DIRECTORY in a GitHub repo (SKILL.md + resources):
curl -sS -X POST http://localhost:8000/api/skills/install \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"repo": "anthropics/skills", "path": "document-skills/pdf", "ref": "main"}'

# A single markdown file by raw URL (becomes that skill's SKILL.md):
curl -sS -X POST http://localhost:8000/api/skills/install \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"url": "https://raw.githubusercontent.com/owner/repo/main/some-skill.md", "name": "some-skill"}'
```

- The optional `"name"` overrides the derived directory name (charset `a-z0-9._-`, must not start with punctuation).
- The skill lands at `/data/shared/skills/<name>/SKILL.md`, is recorded in the installed-skills sidecar, and the index regenerates.
- **409 on name collision** means a skill with that basename already exists — the error names its provenance. Resolve deliberately: uninstall the old one, choose a different `name`, or decide the existing one suffices. Never work around a collision by overwriting files directly.

After installing, confirm it appears in `shared/skills/skills-index.md` and skim it once so you know when to reach for it.

## Uninstalling

```bash
curl -sS -X DELETE "http://localhost:8000/api/skills/<name>" \
  -H "Authorization: Bearer $TOKEN"
```

Only skills installed through this API can be removed here (the server snapshots their bytes into the `/data` git history first, so removal is reversible via git). Seed skills and skills you authored yourself are ordinary files — edit or delete them directly like any of your files. App-owned skills follow their app's install/uninstall lifecycle.

## Listing

`GET /api/skills` returns every skill with `name`, `id`, `description`, `provenance` (`seed` / `agent` / `app:<slug>` / `installed:<source>`), and `uses_30d` (how often you actually loaded it — a signal for pruning). The same information, file-shaped, is `shared/skills/skills-index.md`.
