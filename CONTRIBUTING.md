# Contributing to Möbius

Möbius is a single-owner, self-hosted PWA where the owner chats with an AI agent
(Claude Code or Codex, driven through its Agent SDK; the SDK runs the pinned CLI
binary as a subprocess) to build mini-apps and
edit the platform itself. The whole thing ships as one Docker container. This
guide gets a fresh clone to a running dev/test loop.

## Repo layout

| Path | What lives here |
|------|-----------------|
| `backend/app/` | FastAPI backend (app factory `main.py`, routers under `routes/`, SQLAlchemy models, chat/SSE plumbing). |
| `frontend/src/` | React 19 + Vite shell (chat UI, drawer, mini-app iframe canvas). |
| `skill/core.md` | The agent's system prompt (the "constitution"); read from the live checkout after restart, with an image-baked degraded-boot fallback. |
| `backend/scripts/seed-skills/` | Per-topic agent skills (building-apps, theming, cron, …), seeded into `/data/shared/skills/` create-if-absent on first boot. |
| `core-apps/` | Legacy snapshots only. Memory and Reflection ship from `mobius-os/app-memory` and `mobius-os/app-reflection`: they are auto-installed on first boot, then remain editable and updatable like any catalog app. |
| `tests/` | Playwright end-to-end suite (repo root). |
| `backend/tests/` | pytest backend suite. |

`Dockerfile` builds the single image (frontend build + backend + pinned CLI
tools). `ARCHITECTURE.md` is the deep architecture reference — read it before
any non-trivial change.

## Tests

CI is `.github/workflows/test.yml`; the commands below mirror it.

## Landing a session branch

Install the repository's shared privacy and quality gates once after cloning,
and re-run the installer after pulling hook changes:

```bash
scripts/install-hooks.sh
```

The roots `docs`, `demo-logs`, `.claude`, `.pm`, `AGENTS.md`, and `CLAUDE.md`
are private workspace state. Keep them outside the public clone (local symlinks
are ignored), never force-add them, and never bypass a privacy hook failure.

Parallel work should land through `scripts/land.sh` from the session worktree.
It refuses dirty or detached checkouts, backs up the branch tip under
`origin/preserve/session-*`, rebases onto the latest `origin/main`, then pushes
to `main` without force or hook bypass flags. If a sibling lands first, follow
the recovery loop it prints:

```bash
git fetch origin && git rebase origin/main && scripts/land.sh
```

**Backend (pytest).** Hermetic Docker path (no local venv, tests current source
against the real image — esbuild, node, all deps):

```bash
docker compose -p mobius-test -f docker-compose.test.yml build   # image must exist first
docker compose -p mobius-test -f docker-compose.test.yml run --rm pytest
```

CI runs the equivalent natively: from `backend/`, `pip install -r requirements.txt`,
`npm install -g esbuild@0.25.12` (compile path shells out to it), then `pytest -q`.

**Frontend unit (node:test).** From `frontend/`, after `npm ci`:

```bash
npm test           # = test:lib + test:hooks (two separate ESM loaders)
```

The two scripts can't be merged: `test:lib` rewrites `import.meta.env`; `test:hooks`
aliases `react` to a hook-only shim (see `frontend/package.json` scripts).

**Chat scroll contract.** Before changing `ChatView`, read `ARCHITECTURE.md`
"Chat scroll + steer contract" and run the send/spacer browser specs. The first
visible user message always pins. A later direct, queued, promoted, or steered
message pins only when it was submitted from gesture-entered auto-scroll at the
real-content tail; every pin returns to hold until the user manually reaches the
bottom again. Every visible user message gets a persistent reply-space
reservation, even after a short reply finishes or the chat remounts. Leaving or
returning always preserves the exact visible anchor and never restores
auto-scroll to a newer tail. New send and lifecycle paths must use the shared
state machine rather than deriving intent from geometry alone.

**End-to-end (Playwright).** Comprehensive browser checks run in GitHub after a
PR is opened. Do not point raw Playwright, an auth setup, or a preview proxy at
a live Möbius backend. For a rare local reproduction, first commit the exact
revision, then run the host-only disposable wrapper from a Docker-capable host:

```bash
npm ci && npx playwright install --with-deps chrome
scripts/playwright-local.sh --allow-local-e2e tests/navigation.spec.mjs
```

The wrapper clones that committed revision into temporary storage, builds a
separate backend/database/credential set on random ports, uses one browser
worker, and tears the stack down. It intentionally refuses tracked uncommitted
changes so the browser tests and served runtime cannot drift apart.

## Dev loop: live app rebuild

You rarely restart anything to iterate on a mini-app. Edit
`/data/apps/<slug>/index.jsx` (inside the container's `/data` volume) and
`backend/app/app_watcher.py` picks it up: a `PollingObserver` watches the apps
tree, debounces 1s, recompiles the entry via esbuild, persists the bundle, and
publishes an `app_updated` event so the shell reloads the iframe without a
manual register step. Polling (not inotify) is used because the Docker volume
drops inotify events. Note: editing the **shell** in the served platform clone
(`/data/platform/frontend/src`) IS live — `backend/app/frontend_watcher.py` runs
a debounced `vite build` into the served `dist/` (git operations fire no edit
events; `touch` a changed file to force a rebuild).

## Where to start

Read `ARCHITECTURE.md` first — it covers the backend/frontend module map, the
mini-app contract, the SSE streaming model, and the chat persistence actor. The
feature backlog lives in `.pm/` (a gitignored, local-only kanban: one markdown
file per feature with YAML frontmatter, viewed via `.pm/bin/pm board`); it is
intentionally not part of the public repo, so a fresh clone does not include
open work items.

## Code style

Python: 2-space indent, 80-char lines, Google-style docstrings. Comments are full
sentences (no Title Case, no enumerated steps). JS/JSX follows Vite defaults.
There is no enforced linter config in-repo — match surrounding code.
