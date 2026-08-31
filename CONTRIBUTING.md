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
After protected merges, `.github/workflows/main-image.yml` publishes the
prebuilt `ghcr.io/mobius-os/mobius:main` Railway image without repeating the
test suite.

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
against the real image — Rolldown, Node, all deps):

```bash
docker compose -p mobius-test -f docker-compose.test.yml build   # image must exist first
docker compose -p mobius-test -f docker-compose.test.yml run --rm pytest
```

### Database changes

`Base.metadata.create_all()` creates missing tables but never adds a column to
an existing installation. Every new ORM column therefore needs a new,
append-only function in `backend/app/schema_migrations.py`; never edit a
function already registered in `_SCHEMA_MIGRATIONS`, because upgraded
installations have recorded that exact history and will not run it again.
Migration numbers strictly increase: after rebasing, renumber concurrent work
rather than creating two entries with the same numeric prefix.

Test both a fresh database and an upgrade whose ledger already contains every
earlier migration. `backend/tests/fixtures/schema_0013.sql` is deliberately
frozen previous-release input; never regenerate it from current metadata,
which would make a missing `ALTER TABLE` invisible. Advance that fixture only
as an explicit release-baseline change. The upgrade contract runs production's
`create_all` → migrations order twice, requires an idempotent ledger, and then
requires `mapped_schema_gaps()` to be empty.

The semantic-history manifest freezes every published migration function. A
new migration appends its version and hash; a changed existing hash means the
old function must be restored and the correction expressed as another
migration. Before sharing any schema change, run the dependency-free history
gate and the fast contracts; before landing it, run the full backend suite:

```bash
python3 backend/scripts/check-schema-migrations.py
scripts/test.sh --fast
scripts/wt-pytest.sh backend/tests/test_db_migrations.py -q
```

A boot whose migrations fail or whose mapped tables/columns remain incomplete
deliberately starts no writer, reconciliation, scheduled work, or database
supervisors. It serves only the shell and bounded diagnostic/restart APIs with
`/api/ready` at 503. Recovery repairs the database externally; a clean restart
is then required to run the skipped startup phase as one coherent transition.

CI runs the equivalent natively: install `frontend/package-lock.json`, put its
locked `node_modules/.bin` on `PATH`, install the hashed
`backend/requirements.lock` plus `backend/requirements-static.txt`, run Ruff,
then run the platform suite with an 83% coverage floor. Recovery is a separate
service; this repository asserts that no recovery runtime or alternate boot
mode enters the Möbius image. Edit
`requirements.txt` as the human-readable input and regenerate the lock with:

```bash
cd backend
python -m pip install -r requirements-static.txt
python -m piptools compile --generate-hashes --strip-extras \
  --output-file requirements.lock requirements.txt
```

For one-off `docker run` probes, use `scripts/docker-probe.sh --timeout
SECONDS -- ...`. It gives the container an exact identity and removes it at
the daemon after a timeout, so killing the Docker client cannot leave a hidden
CPU- or disk-consuming probe behind. `scripts/docker-probe.sh --list` shows
the age, CPU, and memory of any active probes.

**Frontend unit (node:test).** From `frontend/`, after `npm ci`:

```bash
npm test           # = test:lib + test:hooks (two separate ESM loaders)
npm run lint       # correctness lint; legacy dependency-array findings warn
npm run test:coverage
```

The two scripts can't be merged: `test:lib` rewrites `import.meta.env`; `test:hooks`
aliases `react` to a hook-only shim (see `frontend/package.json` scripts).

Prefer behavioral assertions against the module that owns a transition.
Source-text assertions are reserved for generated artifacts, packaging,
security boundaries, and other build-time contracts that cannot execute as
ordinary unit behavior.

**Regression guards.** Treat a failing existing test as evidence about the
contract, not an obstacle to a green build. Before weakening or removing it,
determine whether it checks incidental implementation or an intentional
invariant. Preserve or generalize intentional guards when implementation
changes. A contribution that changes the contract must name the affected
invariant and justify the change; replacing a behavioral guard with a narrower
implementation-name check is not equivalent coverage.

**Chat scroll contract.** Before changing `ChatView`, read `ARCHITECTURE.md`
"Chat scroll + steer contract" and run the send/spacer browser specs. The first
visible user message always pins. A later direct, queued, promoted, or steered
message pins only when it was submitted at the one physical autoscroll tail;
reserved reply room remains part of that distance, so any upward reader escape
must keep the next send at the current reading position. Every pin returns to
hold until the user manually reaches the bottom again. Every visible user message gets a persistent reply-space
reservation, even after a short reply finishes or the chat remounts. Leaving or
returning always preserves the exact visible anchor and never restores
auto-scroll to a newer tail. New send and lifecycle paths must use the shared
state machine rather than deriving intent from geometry alone.

**End-to-end (Playwright).** Comprehensive browser checks run in GitHub for pull
requests. For broad or risky work, select **Draft** in Contribute and use
**Send PR** (or **Update PR**). Contribute publishes the exact reviewed branch
and opens or updates a draft pull request; it does not merge anything. Let the
hosted pull-request checks run, then use **Request review** once they are green.
From another checkout whose branch is already on GitHub, the equivalent manual
command is:

Forks inherit every workflow file from upstream even though Contribute enables
only `test.yml`. Therefore `test.yml` is the sole fork-runnable workflow: every
job in every other workflow must carry
`if: github.repository == 'mobius-os/mobius'`. The focused hosted-check contract test
enforces this safe default. A future workflow may run in forks only through an
explicit allowlist change with corresponding review; never rely on missing
secrets, package permissions, or repository variables to fail after allocating
a runner.

Contribute has one GitHub permission contract: device flow always requests
public-repository and workflow access together. Do not add a second
limited-scope publication path or adapt reviewed commits onto stale fork bases;
sync a proven-behind fork when the Tests workflow needs its current default
branch, then push the exact reviewed commit. Existing partial credentials are
rejected before any GitHub write so the owner can reconnect once through the
same path.

```bash
gh workflow run test.yml -R <your-login>/mobius --ref <branch>
```

Do not point raw Playwright, an auth setup, or a preview proxy at a live Möbius
backend. For a rare local reproduction, first commit the exact revision, then
run the host-only disposable wrapper from a Docker-capable host:

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
