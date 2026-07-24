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
| `backend/app/bootstrap.py` | First-boot installation of the Store, Memory, and Reflection from their catalog repositories. Installed apps remain locally editable and updatable like every other catalog app. |
| `tests/` | Playwright end-to-end suite (repo root). |
| `backend/tests/` | pytest backend suite. |

`Dockerfile` builds the single image (frontend build + backend + pinned CLI
tools). `ARCHITECTURE.md` is the deep architecture reference — read it before
any non-trivial change.

## Tests

Required PR CI is `.github/workflows/test.yml`; the commands below mirror it.
After protected merges, `.github/workflows/image-cache.yml` refreshes the
shared Docker cache without repeating the test suite.

## Submitting a session branch

Install the repository's shared privacy and quality gates once after cloning,
and re-run the installer after pulling hook changes:

```bash
scripts/install-hooks.sh
```

The roots `docs`, `demo-logs`, `.claude`, `.pm`, `AGENTS.md`, and `CLAUDE.md`
are private workspace state. Keep them outside the public clone (local symlinks
are ignored), never force-add them, and never bypass a privacy hook failure.

Publish work through `scripts/submit-pr.sh` from the session worktree. It
refuses dirty, detached, `main`, or privacy-unsafe checkouts; rebases onto the
latest `origin/main`; updates the topic branch with lease protection; and opens
or reports its pull request. Required GitHub checks test the synthetic merge
before the PR can land.

```bash
scripts/submit-pr.sh
```

**Backend (pytest).** Hermetic Docker path (no local venv, tests current source
against the real image — esbuild, node, all deps):

```bash
docker compose -p mobius-test -f docker-compose.test.yml build   # image must exist first
docker compose -p mobius-test -f docker-compose.test.yml run --rm pytest
```

CI runs the equivalent natively: install `frontend/package-lock.json`, put its
locked `node_modules/.bin` on `PATH`, install `backend/requirements.txt`, then
run `pytest -q` from `backend/`.

For one-off `docker run` probes, use `scripts/docker-probe.sh --timeout
SECONDS -- ...`. It gives the container an exact identity and removes it at
the daemon after a timeout, so killing the Docker client cannot leave a hidden
CPU- or disk-consuming probe behind. `scripts/docker-probe.sh --list` shows
the age, CPU, and memory of any active probes.

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
worker, and tears the stack down. It intentionally refuses uncommitted source
changes so the browser tests and served runtime cannot drift apart.

## Dev loop: explicit app apply

Edit the complete mini-app source tree under `/data/apps/<slug>/`, then run
`python3 /app/scripts/apply_app.py /data/apps/<slug>`. Apply snapshots one exact
Git tree, validates its manifest, compiles that tree, and publishes it as the
live app. A failed or partial draft leaves the previous live bundle unchanged.
There is no mini-app source watcher: saving files changes the draft, and apply
is the deliberate acceptance boundary. Editing the **shell** in the served platform clone
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
