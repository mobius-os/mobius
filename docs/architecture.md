# Möbius architecture

This is the authoritative architecture document: how the three Möbius layers
work, where they live on disk, how each updates, the boot/self-heal flow, the
recovery floor, the agent's context layers, and the chat contract. It describes
how the system works **today**; anything not yet implemented is called out as
"planned" with a pointer to the relevant `.pm/` card.

Citations are `path:line` against this repo. Where two sources can disagree at
runtime (e.g. the image build SHA vs. the served backend), the doc says which is
authoritative.

## Overview

Möbius is a single-container, single-owner personal-AI OS. The owner chats with
an agent (the Claude Code or Codex CLI, run as a subprocess) and the agent builds
mini-apps and reshapes the platform itself. **The agent is the product;
everything else is substrate the agent operates on.** This inverts the usual
app-design defaults — broken states are recoverable events, not states to
prevent; prevention lives in the agent's instruction layer, not in code-level
guardrails (see the "Design philosophy" section of `CLAUDE.md`).

Three layers make up a running instance:

1. **Core platform** — the backend (FastAPI) plus the shell (the React PWA). One
   updatable unit; the agent can edit it live.
2. **Recovery** — a sacrosanct, agent-proof floor. A plain-HTML recovery page and
   an isolated recovery chat that stay reachable when the rest of the platform is
   broken. Updated only by the image, never by an in-product agent edit.
3. **Mini-apps** — the JSX apps the agent builds, each its own slug under
   `/data/apps/`, each with its own git repo and its own update path.

The whole thing runs in one Docker container behind Caddy (TLS). uvicorn serves
both the API and the frontend static files (`backend/app/main.py`).

## The three layers and where each lives on disk

The defining trick is the **baked floor vs. live copy** split. The image ships a
read-only baked copy of the backend and shell; on first boot those are copied
into the `/data` volume, where the agent then edits them. The volume persists
across image rebuilds, so agent edits survive upgrades — but the baked floor is
always available as an integrity reference and a recovery source.

| Layer | Live (agent-editable) | Baked floor (read-only) | Git-tracked? |
|---|---|---|---|
| Backend | `/data/platform/app/`, `/data/platform/scripts/`, served via the `/app/app -> /data/platform/app` symlink | `/app/app-baked/`, `/app/scripts-baked/` (chmod `a-w`, root-owned) | `/data/platform` is its own git repo |
| Shell | `/data/shell/src/` (source) + `/data/shell/dist/` (built bundle) | `/app/static/` (baked Vite build), `/app/shell-src/` (stock source) | shell src tracked in the outer `/data` repo today; a real nested repo is planned (149/150) |
| Mini-apps | `/data/apps/<slug>/index.jsx` + per-app data, compiled to `/data/compiled/app-<id>.js` | catalog manifests on GitHub (`mobius-os/app-*`) | each `/data/apps/<slug>/.git` is its own repo |

The **backend symlink invariant** is load-bearing: `/app/app` and `/app/scripts`
are ALWAYS symlinks pointing at the live `/data/platform` copies (`entrypoint.sh`
sets them at boot, lines 220-266). uvicorn runs `cd /app && uvicorn app.main:app`;
Python resolves the symlink transparently, so `from app.config import ...` loads
the real file from `/data/platform/app/`. `__pycache__` lands inside the
mobius-owned `/data/platform/app/` (entrypoint.sh:30-44). When the platform tree
is missing or unparseable, the symlink is NOT created and uvicorn boots the baked
originals directly (see boot flow below).

The **shell serving priority** is decided at module load in `main.py`:
`_static_dir = _live_dir if _is_complete_build(_live_dir) else _baked_dir`, where
`_live_dir = /data/shell/dist` and `_baked_dir = /app/static`
(`main.py:585-591, 836`). `_is_complete_build` requires both `dist/assets/` and
`dist/index.html`. A file that exists only in the baked build (the canonical case
is `/vendor/three/*`, which Vite doesn't emit into `dist`) falls back to
`_baked_dir` per-request (`main.py:916-919`).

What is **runtime-only and gitignored** (entrypoint.sh:782-818): `cli-auth/`,
`db/`, `compiled/`, `shell/dist/`, `shell/node_modules/`, `chats/`, `backups/`,
`apps/*/data/` (mini-app runtime data), `agent-browser-profiles/`, `logs/`,
`cron-logs/`, the identity secrets (`.secret-key`, `.recovery-secret`,
`service-token.txt`), and `platform/` (excluded from the outer `/data` repo
because it has its own git repo).

## How each layer updates

### Platform (backend) — the merge engine

`/data/platform` is a git repo with two branches: `main` (owner/agent live edits)
and `upstream` (the baked-image floor). The engine in
`backend/app/platform_update.py` is the per-app update model lifted to the whole
backend: it computes the merge verdict off the live worktree **before** touching
any served file, so a half-applied merge is impossible
(`platform_update.py:1-31`).

The shape of an apply (`_apply_sync`, `platform_update.py:566-617`):

1. Commit any uncommitted local edits so the merge has a clean base
   (`_commit_local_edits`, line 531; refuses to proceed if still dirty).
2. `seed_upstream_if_missing` creates the `upstream` branch from the ancestor
   `baked-<sha>` tag on instances predating the feature (line 323).
3. `collect_baked_floor` + `record_baked_upstream` commit the current image's
   baked `app/` + `scripts/` onto `upstream` as a child of the prior upstream tip,
   without checking the worktree out (`commit-tree` + `update-ref`,
   lines 344-409). The new commit is force-tagged `baked-<sha>` so the next
   update's merge base is exact.
4. `compute_merge_tree` runs `git merge-tree --write-tree --name-only main
   upstream` — the verdict off the live worktree, no working-tree mutation
   (lines 420-438). Clean returns a tree OID; conflict returns the paths.
5. **Clean** → `write_merged_tree_to_worktree` writes each file via temp +
   fsync + `os.replace` (whole files, never truncated), **skipping protected
   paths** (lines 470-509). `commit_clean_merge` records a two-parent merge on
   `main` (lines 512-528). It then sets `.platform-restart-needed`; nothing
   restarts on its own — the owner confirms the restart from Settings.
6. **Conflict** → the merge is NOT materialised programmatically (a bare `git
   merge` would try to rewrite root-owned protected files and fail). The new code
   stays on `upstream`; the engine records `.platform-conflict` and
   `spawn_platform_conflict_chat` opens a visible agent chat that walks the agent
   through reconciling the named non-protected files by hand
   (`platform_update.py:661-760`).

Two facts shape every operation: `/data/platform` holds the SERVED backend (so no
half-applied merge), and the protected/recovery files are root-owned `chmod 444`
in the live tree — the `mobius` user that runs the engine cannot and must not
overwrite them, which is why a plain `git merge`/`reset --hard` is off-limits and
the clean path writes the merged tree manually
(`platform_update.py:9-31`, `protected_platform_paths` at line 452).

**Today vs. planned.** Today "upstream" is the **baked image floor** of whatever
image is installed — an image pull is what advances it. The planned direction
(`.pm/features/147-unified-selfimprovement-git-spine.md`) is a real signed GitHub
`origin` that `upstream` fast-forwards to (honest ancestry, not grafted onto the
baked floor), plus the same engine generalized to the shell and skills (149/150),
and eventually owner-gated upstream PR contribution (153, a rough sketch only).
The status endpoint computes availability on demand with no daemon and no polling
(`platform_status`, line 286).

### Shell — rebuild and the #1 deploy gotcha

The shell src lives at `/data/shell/src`; the built bundle at `/data/shell/dist`.
After editing src, the agent runs `bash /app/scripts/rebuild_shell.sh` to produce
a fresh `dist`. Because `_static_dir` is resolved **at module load**, the running
uvicorn keeps serving the old bundle until the process restarts — the agent will
claim "shell rebuilt" while the user still sees the old UI. A restart (Settings →
Server → Restart, or container restart) is required to pick up an in-band shell
rebuild (see `CLAUDE.md` "Shell rebuild + static-dir resolution").

**The #1 deploy gotcha** (`CLAUDE.md` "Frontend serving priority"): the `/data`
volume persists across `docker compose build && up -d`, so a new image's fresh
`/app/static` is MASKED by the old `/data/shell/dist`. The entrypoint handles the
self-host image-pull case directly: it detects a newer baked bundle by two
OR'd signals — a `BUILD_SHA` marker change OR a content-hash change of the Vite
entry bundle (`assets/index-<hash>.js`) extracted from each `index.html` — and
atomically refreshes `/data/shell/dist` from the new `/app/static` via a
lock + temp-dir + rename swap (`entrypoint.sh:532-646`). It refreshes ONLY `dist`,
never `src`, so a user who customized the shell source keeps it (their edits just
aren't served until they rebuild). `deploy-prod.sh` refreshes dist itself and
stamps `.image-build-sha` to match, so this stays a no-op on the owner's prod.
`GET /api/version` exposes both `sha` (image build) and `shell_sha` (the dist
marker) so a client can detect a stale served UI (`main.py:516-545`).
Never delete `/app/static` — it is the only recovery fallback and is root-owned.

### Mini-apps — catalog, ETags, tombstone/recover

Mini-apps are registered in the DB (`App` rows) and served from
`backend/app/routes/apps.py`. Two cache-validated routes drive freshness; both
use `Cache-Control: no-cache` so the browser revalidates with `If-None-Match` on
every fetch and gets a bodiless 304 when nothing changed:

- `GET /api/apps/{id}/module` — the compiled ES module. ETag is
  `W/"<int(updated_at * 1e6)>"` (microsecond precision so two updates within one
  second produce different validators) (`_etag_for_app`, `apps.py:1392`; route at
  1597).
- `GET /api/apps/{id}/frame` — the SHARED `app-frame.html` runtime shell
  (importmap + postMessage init). Its ETag is a COMPOUND validator folding
  `app.updated_at` with a content hash of `app-frame.html` (`_frame_etag`,
  `apps.py:1421-1476`; route at 1479). Keying only on `updated_at` (as `/module`
  does) would let a frame edit go unseen by installed PWAs forever — the exact bug
  that pinned clients to a spinner when a `/vendor/three/` path was dropped. The
  hash is of content, not mtime, because `cp`/bind-mounts/restore rewrite mtimes
  independently of content.

App URLs are stable per `appId` (no manual `?v=` cache-buster); freshness rides
the ETag. The SW caches both routes for every installed app. Full cache strategy
lives in `docs/offline.md` + `frontend/src/sw-cache-policy.js`; the iframe
LRU-cache + postMessage protocol is documented in `CLAUDE.md`.

**Tombstone/recover** (feature 110) — `App` (like `Chat`) uses a reversible
soft-delete. `DELETE /api/apps/{id}` sets `deleted_at`, drops the app's cron, and
renames `init-cron.sh` to `init-cron.sh.tombstoned` so the boot replay glob skips
it — but preserves the on-disk source tree (`delete_app`, `apps.py:1110`). The app
vanishes from the drawer and its `module`/`frame` 404, but a reinstall (matched by
`manifest_url`) or `POST /api/apps/{id}/recover` within `APP_SOFT_DELETE_TTL`
(7 days) restores it, re-arming the cron and bumping `updated_at` so the iframe
ETag advances (`recover_app`, `apps.py:1176-1232`). `allocate_unique_slug`
deliberately scans tombstoned rows too, so a reinstall in the recovery window
can't reuse a tombstoned app's id/slug (`apps.py:318-345`). A live-row helper
(`live_app_or_404`) hides tombstoned rows from every resolution path. Catalog
updates flow through each app's own git repo (`upstream`/`main`, feature 084);
the merge/conflict shape mirrors the platform engine.

## Boot and self-heal flow

> **DIRECTION (owner 2026-06-30) — this section describes the CURRENT code, which
> is being replaced.** The baked-floor duplicate + `/app/app` symlink-swap +
> the `.platform-serve-baked` auto-heal probe (and the crash-loop `cp`-restore)
> are being removed in favour of: the platform served directly as a git repo, a
> minimal "would this boot?" **pre-flight gate** before a change goes live, and a
> **separate frozen recovery-only boot** (no full platform duplicate) whose
> recovery agent restores via git. No magic healing — a break that slips the
> gate goes to recovery, not an automatic baked overwrite. See the self-update
> redesign track.


`backend/scripts/entrypoint.sh` runs as root (to fix volume permissions), then
`exec su`s to the `mobius` user for uvicorn. The decision tree:

1. **Boot-attempt counter** (`/data/.boot-attempt`, written before uvicorn,
   lines 46-115). A crash before the health probe writes
   `/data/.last-successful-boot` increments it. On `>= 3` consecutive failures
   *and* a prior successful boot on record, the entrypoint restores
   `/data/platform` from the baked floor (`cp -a /app/app-baked/.`), re-opens
   write perms, commits the restore, and resets the counter (lines 74-108). On a
   genuinely-fresh volume (no `.last-successful-boot`) it does NOT restore — those
   are first boots, not crash loops (lines 109-115).
2. **Platform sanity** (`_platform_sane`, lines 171-195): does
   `/data/platform/app/main.py` exist AND parse? The check is a cheap
   `python3 -c "ast.parse(...)"` — it catches a `SyntaxError` (which would make
   uvicorn die at import with no health response) without running any code.
   - **Sane** → `_use_platform=1`; the symlink swap points `/app/app` at
     `/data/platform/app` and uvicorn serves the platform (lines 184-186,
     233-260).
   - **Missing** (first boot / wiped volume) → copy from baked, then serve
     platform (lines 199-218).
   - **Present but unparseable** → boot from the baked floor
     **NON-DESTRUCTIVELY**: `_use_platform=0`, the symlink is NOT created, so
     `/app/app` stays the baked real dir and uvicorn still boots. The agent's
     edits in `/data/platform` are PRESERVED, not served, and the operator is
     told to fix `main.py` or run `recovery_restore.sh platform-baked`
     (lines 187-195, 261-266). There is **no automatic destructive revert of
     `/data/platform`** in this path — that behavior was deleted; the only
     automatic baked restore is the crash-loop counter in step 1.
3. **Serving-source sentinel** — the entrypoint writes `/tmp/serving-source`
   (`platform`|`baked`) so `GET /api/version` can report the ACTUALLY-served
   backend, which can disagree with the image `build_sha` when `/data/platform`
   diverged (`entrypoint.sh:268-274`; `_served_platform_identity` in
   `main.py:466-513`).
4. **Health probe** (background, lines 1002-1035): polls `/api/health` for up to
   90s; on a 200 it writes `.last-successful-boot`, zeroes the counter, and clears
   `.platform-restore-active`. This is the "success" signal that suppresses
   false-positive crash-loop detection. It waits on the OUTCOME (a 200), never on
   a process name (no `pgrep` self-match trap).

An image-SHA change since `.baked-sha` is detected and surfaced as a flag
(`.platform-upgrade-available`) but does **not** auto-merge — the merge is the
owner-triggered engine above (`entrypoint.sh:335-366`).

`main.py`'s `lifespan` adds a second self-heal layer once uvicorn is up, every
step wrapped in try/except so a failure can't brick the recovery surface:
reconcile chats stranded by a mid-turn crash (`reconcile_interrupted_chats`),
reap leaked `*.js.staging` bundles, recompile any live `App` row whose bundle is
missing, start the single-writer chat-persistence actor, backfill `source_dir`,
and start the JSX file watcher (`main.py:78-261`).

## Recovery (the sacrosanct floor)

Recovery is the floor that stays reachable when the agent is unreachable — when
the React bundle is broken, the SDK chat stack is broken, or the boot import chain
is degraded. Three mechanisms:

**1. The `/recover` page** (`backend/app/routes/recover.py`). Static,
password-authenticated HTML, deliberately independent of both the React frontend
AND the agent's import chain. It uses raw `sqlite3` (stdlib) for the owner-row
lookup and reads `DATA_DIR` from the environment — it does **not** import
`app.database` / `app.models` / `app.config` / `app.theme`, because those are on
the agent's write surface and recovery must work when they're broken
(`recover.py:1-9, 51-68`). Auth is an HMAC-signed cookie from `recover_auth` (not
a JWT), so recovery works even if `app/auth.py` is corrupted (`recover.py:113-136`).
Live actions: download a complete backup zip, `factory_reset`, and
`reinstall_store` (`recover_action`, line 274). Deeper restores are delegated to
the recovery chat agent and to `recovery_restore.sh`.

**2. The recovery chat** at `/recover/chat`
(`backend/app/recover_chat.py` + `recover_chat_runner.py`). A vanilla
HTML page (Python f-string, no Vite/React/build step) with its own minimal stack
that **shares no code with `app.chat`, `app.providers`, or the SDK runners** — if
the agent broke the production chat path, recovery still works
(`recover_chat_runner.py:1-24`). The runner spawns the Claude or Codex CLI
directly as a subprocess, parses stdout JSON lines, and appends each turn to
`/data/recovery_chat.jsonl` (append-only; survives a broken chats-DB schema). It
deliberately omits AskUserQuestion, multi-turn resume, stop, and per-token
typewriter — minimal on purpose.

**3. The frozen recovery island** (`protected-files.txt`). The entrypoint
re-enforces `chmod 444` (or `555` for `.sh`) + root ownership on every listed path
on every boot (`entrypoint.sh:648-680`). Two categories:

- **Credential surfaces** — the login/setup/provider-auth shell components.
- **Frozen recovery island + boot-chain wiring** — `recover.py`,
  `recover_html.py`, `recover_chat.py`, `recover_chat_runner.py`,
  `recover_auth.py`, `recover_oauth.py`, plus `main.py`, `routes/__init__.py`,
  `auth.py`, `database.py`, `config.py`, `models.py`, `entrypoint.sh`,
  `recovery_restore.sh`. The wiring files are frozen because the agent could
  otherwise edit `main.py` to drop the recover routers, or break `config.py` (the
  deepest shared boot dependency) and kill uvicorn before any router loads
  (`protected-files.txt:36-59`).

`routes/__init__.py` is a **crash-tolerant import scaffold**: `main.py` does one
`from app.routes import (...)` over ~15 names; an ImportError in any one
unprotected route module would otherwise kill uvicorn at boot. Each router loads
through `_load(name)`, which returns the real `router` on success or a stub
`APIRouter` that 503s every path (pointing the user at `/recover/chat`) on import
failure — so the frozen `main.py` keeps importing cleanly and a single broken
route never takes the whole app (and recovery) down. The scaffold is itself
frozen, or the defense would be meaningless.

If a frozen file is somehow corrupted, `recovery_restore.sh` re-copies it from the
baked floor. Per epic 147's principles, recovery is sacred: immutable, agent-proof,
offline-complete, and never gains a network dependency.

## Agent context layers

The agent reads three layers of instructions (shipped 2026-06-03; see `CLAUDE.md`
"Agent context — three layers"):

1. **Constitution** — `skill/core.md`. Owner-curated, baked to `/app/skill/core.md`,
   passed as the system prompt (`--system-prompt-file`) on the first message of a
   chat. Small by design: who the agent is, its write surface, recovery URLs, the
   memory protocol, and the creative-task workflow; the per-task detail lives in
   skills it `Read`s on demand (`skill/core.md:1-3`). `providers.get_skill_path()`
   resolves `core.md` only — there is no `agent-skill.md` fallback.
2. **Skills** — `/data/shared/skills/*.md`, agent-editable. Seeded
   **create-if-absent** from `backend/scripts/seed-skills/` by
   `init_skills.py` on first boot; later boots only ADD seed skills the instance
   is missing and NEVER overwrite an existing file, so the agent's (and the
   nightly Reflection agent's) edits survive (`init_skills.py:10-24, 61-84`).
   Current seed skills: building-apps, theming, cron, notifications, images,
   recovery, memory, reflection, app-component-shapes, embedded-app-agent,
   resolving-app-git.
3. **Memory** — `/data/shared/memory/`, a knowledge graph of small linked
   markdown notes: a root router (`index.md`), topic maps (`mocs/`), atomic notes
   (`notes/`), and a node per chat (`chats/<id>/index.md`). Session start injects
   the router plus the full summaries of the ~10 most-recently-touched chats;
   the deeper graph is pulled on demand via the `memory_search.py` read-only
   subagent. **Each chat is a memory node the agent maintains every turn** (a
   growing full summary it only adds to, never deletes from); the nightly
   "reflection" pass consolidates chat notes into the graph
   (`skill/core.md:46-83`). When the `.ready` sentinel is absent,
   `build_memory_block` returns an empty block — there is no flat-file fallback,
   and nothing reads the legacy `/data/shared/agent-experience.md`.

This is the platform's chosen lever for prevention: when the agent learns a
gotcha, the fix is "record it so future-self avoids it," not "block the operation
server-side" (`CLAUDE.md` "Design philosophy" points 2-3).

## Chat contract

When the owner sends, queues, or steers a message, how the chat scrolls and how
position is restored on leave/return is a precise, lock-in-tested contract —
first message pins to the viewport top; subsequent sends always reserve bottom
space but only move the scroll position when the reader is at the bottom; steered
queued messages render as separate user rows joined with `"\n\n"` into one agent
turn; leave-and-return restores the exact prior position even mid-stream. The
backend persistence side (the single-writer actor, the per-chat queue lock, the
stop-chat semantics) is documented in `CLAUDE.md`. The authoritative scroll/steer
rules, the two observed prod bugs they guard against, and the lock-in Playwright
specs live in **[docs/chat-scroll-steer-contract.md](chat-scroll-steer-contract.md)**.

## Testing and the determinism principle

Möbius's e2e suite (`tests/`, Playwright) plus the backend suite are the safety
net for the load-bearing contracts (scroll/spacer, navigation back-stack, SSE
reconnect, the chat queue). The guiding principle for keeping those tests honest:

**e2e flakiness is a symptom of app-level non-determinism, not a test problem to
paper over with retries.** The principled fixes, in order of preference:

1. **Eliminate the race at the source.** When a test flakes because two code paths
   race, fix the app, not the test. The canonical case was `auth.setup` flaking on
   the first-load service-worker reload (`1edf72c`); the fix made the first-install
   reload deterministic rather than adding a sleep. Prefer optimistic-state-
   authoritative reconciliation over polling that clobbers — the chat persistence
   actor exists precisely because concurrent JSON read-modify-write loses updates.
2. **Mock the clock for genuinely time-dependent behavior** (soft-delete TTLs,
   token expiry, debounce windows) so the test asserts the real branch
   deterministically instead of waiting real wall-time.
3. **Wait on a signal, not a duration.** Wait for the text/element/condition that
   proves the state was reached (`wait --text`, `wait --fn`, an ETag advance, a
   `done` event), never a fixed `sleep`. A fixed delay is either too short (flake)
   or too long (slow); a signal is correct by construction.

CI uses bundled chromium with retry-once for known flakes; local runs use the
system Chrome channel with NO retries, so a local flake should be investigated
and fixed at one of the three levels above, not retried away.
