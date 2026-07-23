# Möbius architecture

Read this first if you just cloned the repo. It maps the system so you can find the file that owns a behavior in your first hour. Every row was verified against the source on `main`; if you find a row that no longer matches the code, fix the row.

## What Möbius is

Möbius is a self-hosted PWA where one owner chats with an in-product AI agent to build mini-apps and modify the platform itself. The "agent" is a coding-agent (Claude Code or Codex) running as a subprocess inside the container; a chat message spawns a turn, the backend streams the agent's output back over SSE, and the agent can compile JSX into mini-apps, edit the shell UI, manage files, and schedule tasks. The whole platform runs in a single Docker container and installs on Android/iOS as a PWA.

The design has one line behind it: **low floor, high ceiling, no walls.** The agent is the product; everything else is substrate it operates on. Möbius bets on rising AI capability and inverts the usual defaults: **make the good path easy** — design, examples, prompts, and a clean script for any step that's identical every time — and **make the bad path harder but never impossible.** The owner can tell the agent to delete everything and it can; the net under it is the recovery floor, not a wall. **Code empowers the agent, it does not police it**: prevention lives in the instruction layer and learned memory, never in code-level validators or in removing a capability.

**Intelligence over scripts.** A script, validator, or fixed procedure earns its place only for the unambiguous and identical-every-time — clone/pull to install or update recovery, rebuild the served frontend, a deterministic migration. Everything ambiguous — why something broke, how to reach the last good state, fixing what another agent did — is the agent reasoning in context. Branching logic to cover cases, or bespoke machinery to detect-and-auto-handle a situation, is the tell that you're building the wrong thing: script the certain step, **instruct** the agent to run it (sharpen the prompt if it forgets), and trust intelligence for the rest. The only automation worth keeping is one a tool already ships (a real watcher, HMR) — never flimsy glue invented to avoid instructing the agent. **Recovery** is this made concrete: a small, separate, always-up agent that can't break its own code (updated only by that one unambiguous script) but reaches and fixes everything else by *reasoning* about what broke, not from a menu of canned reversions.

The flip side: infrastructure the agent never sees — provider plumbing, the persistence actor, the streaming protocol, the navigation back-stack — gets whatever complexity makes it correct. Maximal expressive surface for the agent, ironclad substrate underneath.

**If you're an agent building Möbius, attend to this on every change.** You were trained on products for careless or adversarial users, where the job is to validate, sanitize, and prevent. Here the user is the owner and you are trusted. When you reach for a guard, a validator, or background auto-magic to prevent a mistake, stop and ask whether you're policing — or substituting brittle automation for intelligence. A change that fights this philosophy is a sign you're solving the wrong problem.

This split is why a section can read either "this is intentionally hackable, don't add a guardrail" or "this is load-bearing, don't touch it without reading the full reference." Both are true; which one applies depends on whether the agent sees the surface.

## Deployment — single container

```
Dockerfile (root)     Single-container image: frontend build + backend + CLI tools
docker-compose.yml    Self-hosted: Caddy (TLS) + app + recoveryd
├── caddy             HTTPS reverse proxy — forwards everything to app:8000
├── app               FastAPI serves the API + the frontend static files
└── recoveryd         frozen recovery floor — same image, own container, serves :8001 (the bundled Caddyfile routes /recover* to it ahead of the app catch-all; see the self-heal section)
```

The image bundles everything the agent needs at runtime (the Claude and Codex CLIs, esbuild, Node) so the platform works out of the box. To join an existing Caddy setup instead of the bundled one, use `docker-compose.override.example.yml`.

### Frontend serving priority

At startup `backend/app/main.py:1000` picks one static directory **at module load time**, not per request (though a request for a file missing from the chosen `/data/platform/frontend/dist` still falls back per-request to the baked `/app/static`):

```
/data/platform/frontend/dist/  ← preferred (the served platform clone's live build; persists across image rebuilds)
/app/static/                   ← fallback (baked into the image, current with git HEAD)
```

The `/data` volume persists across `docker compose build && up -d`, so a new image's `/app/static/` is masked by an old `/data/platform/frontend/dist/`. After a frontend deploy, refresh both source and dist and verify the bundle hash changed in `/data/platform/frontend/dist/assets/index-*.js`. Because the choice is made at module load, an in-container shell rebuild does not take effect until the uvicorn process restarts. Never delete `/app/static/` — it is the only recovery fallback and is root-owned.

### Security updates — who patches what

Möbius is meant to be self-hosted on a user-provisioned host — a managed platform (Railway/Render/Fly/PikaPods) or a raw VPS — so "apply a security update" splits into three tiers by who can even act:

- **Image userspace** — the Python wheels, npm globals, apt packages, and vendored mini-app libs baked into the image. The agent owns these end-to-end: bump the pin (`Dockerfile` / `backend/requirements.txt` / `frontend/package.json`), rebuild, recreate. Never `apt upgrade` / `pip install -U` a *running* container — that mutation is ephemeral and drifts the live container away from the reproducible image. `deploy-prod.sh` is the apply path. One deliberate runtime exception: the `mobius` user (the in-product agent) has scoped NOPASSWD sudo for `apt-get`/`apt`/`dpkg` only (`/etc/sudoers.d/mobius-apt`, baked and visudo-validated in the `Dockerfile`), so it can install a genuinely needed OS package at runtime without full root — such installs stay ephemeral until pinned into the image, and the recovery floor deliberately depends on zero apt-installed packages, so a bad package can never take recovery down.
- **Host OS userspace + the Docker engine** — outside every container; patched on the host (`unattended-upgrades` covers the OS packages; the engine is a separate host upgrade).
- **Host kernel** — *not in the container*; it shares the host's and cannot be patched from inside. On a managed platform the operator patches+reboots the kernel underneath you (the safe default for non-devops owners); on a raw VPS it's the owner's job, via `unattended-upgrades` + livepatch + a scheduled reboot window.

Two invariants follow. (1) **Möbius never patches the kernel from inside the container** — it only *surfaces* "host reboot pending / kernel CVE outstanding" to the owner; the platform/OS applies it. (2) **The in-container agent cannot recreate its own container** (the swap would kill its own process), so the shape is *propose-in* (agent scans → bumps → tests → commits) / *dispose-out* (a host-driven `deploy-prod.sh`, or blue-green, does the rebuild+recreate). Detection is the agent's leverage on every tier: `pip-audit` + `npm audit` + an image scanner (Trivy / `docker scout`) over the built image → triage → bump → test → deploy (tier 1) or surface a reboot window (tiers 2/3).

**The in-product platform updater does not update host deployment files.**
Settings advances the served `/data/platform` clone inside the app volume; the
bundled self-hosted Caddy service instead reads `./Caddyfile` from the host
checkout, and image/dependency changes likewise need a host rebuild/recreate.
An incoming change to `Caddyfile`, `docker-compose.yml`, `Dockerfile`, or a
dependency manifest therefore carries a separate host action (and may require a
particular ordering) even when the live clone rebases cleanly. Never describe
Apply + server restart alone as activating those files.

**lodash is pinned to 4.18.1 via `overrides`.** `@openai/apps-sdk-ui` pulls lodash transitively — only through its `Slider` component, which the shell does not import. The 4.17.x line sat unfixed against several advisories for a long stretch; 4.18.x restored maintenance and patched them, so `frontend/package.json` `overrides` forces the transitive lodash to 4.18.1 (`npm audit` is clean). As defense-in-depth, `frontend/src/lib/__tests__/appsSdkLodash.test.js` also fails if the shell ever imports `Slider`, which keeps lodash tree-shaken out of the shipped bundle regardless of the pin.

## Self-update model — `upstream` / `main`, replay on update

Möbius is the rare app whose own agent edits its live code: the in-product agent customizes its mini-apps (`/data/apps/<slug>`) and the whole platform repo (`/data/platform`, a real clone of `mobius-os/mobius`, including the frontend) while the platform runs. A deploy then ships a *new pristine version* of that same code. One small model keeps every such surface up to date without clobbering the owner's customizations and without a deploy ever silently dropping them.

**Two branches per surface, and the update is a rebase.** Each updatable surface is a git repo with:

- **`upstream`** — pristine history (`A → B → …`). The exact bytes of each *released* version, committed only by the installer / image, never the agent.
- **`main`** — the owner/agent's edits (`X`), and what the surface actually serves. It sits on top of the `upstream` version it was last updated to.

So the repo is `A → X` (release `A`, then local edits `X`). An update fetches the new release and does exactly what a developer would:

```
record the new release as a new upstream commit:   A → B   (and  A → X  locally)
rebase the local edits onto it:                    A → B → X
```

The owner's customizations end up *on top of* the current release, as if they'd just been made against it. Mechanically: commit any stray working-tree changes onto `main` first (`app_git.commit_local`, so the merge has a committed base), advance `upstream` to `B`, then compute the three-way verdict with `git merge-tree --write-tree` (`app_git.merge_upstream`) and, when clean, write the merged tree back and replay it as a single-parent commit on the new `upstream` tip — rebase-shaped linear history (`A → B → X`) without ever running `git rebase`.

- **Clean merge** → the merged tree is replayed as a single-parent commit on the new `upstream` tip and the surface recompiles (apps) or restarts (backend) onto the new code.
- **Conflict** (the release and the local edits touched the same lines) → an **owner-clicked agent chat** resolves it. The update attempt records the new upstream plus a durable receipt bound to every fetched source/static/icon/seed byte, and leaves live files untouched. When the owner chooses "Resolve in chat", apps materialize standard conflict markers (`start_conflict_merge`, a `git merge --no-commit --no-ff upstream`) for the agent to edit; the platform updater leaves the live tree untouched and the resolver chat runs the merge itself. Saving marker-free source records a *single-parent replay* — `--no-ff` points `MERGE_HEAD` at the upstream tip and the commit takes only that one parent, so even a resolved conflict stays linear (`A → B → X`), never the 2-parent commit a plain `git merge` would leave. The canonical installer then verifies the receipt and promotes source, bundle, static files, DB metadata, icon, seeds, cron, and skills through its normal lifecycle. If fetch/materialization fails after the source commit, the previous app remains served and the receipt survives for startup/user retry. Both app and platform conflicts are click-gated: the update surfaces `mode=conflict` / conflict paths or a Settings conflict state, and the owner chooses "Resolve in chat" before an agent turn starts. The owner never hand-merges; back out with `git merge --abort`.

**"Update available" is an ancestry question, not a version-string compare:** an update is available iff `upstream`'s tip is **not yet an ancestor of `main`** (a new release hasn't been rebased in). This is the content question — "does my working tree already contain this release" — that a `image_sha != recorded_sha` proxy can't answer on a customized instance, and it's what eliminates phantom "update available" rows after a deploy that changed nothing the owner hadn't already.

### The recovery/auth files are NOT in the model — they're gitignored

The one thing that makes a self-editing platform tricky is the recovery/core island (`protected-files.txt`: currently the baked `entrypoint.sh` and `recovery_restore.sh`; recoveryd is the separate HTTP recovery floor). These are root-owned `chmod 444/555`; the `mobius` user that runs the updater genuinely cannot write them.

The simple answer is that **they are not part of the git model at all** — each surface's `.gitignore` excludes them. They live on disk, managed wholly by the image (the root entrypoint re-enforces root-owned 444/555 on them every boot; their contents refresh from the baked floor only on first boot, a crash-loop restore, or a `recovery_restore.sh` run — the deploy/recovery path, not every reboot). Because they're untracked, neither `record upstream` nor the merge/replay ever touches them, so there is no special "protected-file" machinery in the update engine — the replay only ever moves agent-editable files, and the recovery island updates the one way it should: via an image deploy. (Recovery therefore stays agent-proof, and becomes current with the image once the deploy's restore/reconcile step runs.)

### Where each surface stands

| Surface | Repo | On the model | Engine |
|---------|------|------------------|--------|
| **Mini-apps** (`/data/apps/<slug>`) | `.git` per app (installed apps; agent-built bespoke apps have no upstream to track) | yes — whole source tree on `upstream`, single-parent replay, so **multi-file apps update cleanly** | `backend/app/app_git.py` + `install.py` |
| **Platform** (`/data/platform` — backend *and* frontend) | `.git`, recovery files gitignored | yes — clone-native `git fetch origin` + rebase of local `main` onto `origin/main` (commit-stray-edits-first, conflict-abort, post-rebase import probe with rollback); ancestry availability (`origin/main` not yet an ancestor of local `main`) | `backend/app/platform_update.py` |

Mini-apps use **one** small tree-aware engine (`app_git.py`): `record_upstream` commits the *whole source tree* on `upstream`, `merge_upstream` verdicts a clean-vs-conflict via `git merge-tree`, and a clean apply replays the merged tree as a **single-parent** commit on top of `upstream` (linear `A→B→X`). Mini-apps are thin callers of that primitive — they pass their own source tree. The platform (backend + frontend, one served clone) is clone-native instead: it uses `git fetch origin` plus a rebase of local `main` onto `origin/main`, with ancestry-based availability (`origin/main` not yet an ancestor of local `main`). Mini-app update discovery is different: the store compares the catalog manifest version against the installed `App.version` (the new release lives in the remote catalog, so a local ancestry check can't see it). There is no per-surface protected-file scaffolding.

## Backend (`backend/app/`)

FastAPI app. `main.py` is the factory (CORS, rate limiting, routers, static serving). `routes/__init__.py` is a crash-tolerant import scaffold: every router is loaded through `_load(name)`, and an import failure returns a 503 stub instead of killing uvicorn, so `/recover/chat` stays reachable. To add a route, write the module under `routes/`, expose a `router`, and register it in `routes/__init__.py` (both the `_load(...)` line and `__all__`), then mount it in `main.py`. (One documented exception: `routes/chats.py` exposes a *second* router, `app_chat_router` (`/api/app-chats`), which `main.py` imports and mounts directly because `_load` returns only each module's primary `router`.)

### Core app + chat runtime

| File | Role |
|------|------|
| `main.py` | App factory: CORS, rate limiting, security headers (`_SecurityHeadersMiddleware` — authoritative on every response, strips-and-replaces same-named route headers; deliberately no CSP, see SECURITY.md), router mounting, static file serving; resolves `_static_dir` at load (`main.py:1000`); serves `GET /api/version` (image + served-platform identity) |
| `frontend_watcher.py` | Polling watcher that auto-rebuilds the served frontend clone (`/data/platform/frontend`) on edit — debounced `vite build`, atomic `.dist-next`→`dist` swap |
| `config.py` | `Settings` via pydantic-settings; reads `.env` |
| `database.py` | SQLAlchemy engine, `SessionLocal`, `Base`, `get_db`, and `run_migrations()` (idempotent boot-time additive `ALTER TABLE`s) |
| `models.py` | ORM tables: `Owner`, `Chat`, `ChatRun`, `App`, `PushSubscription`, `Notification` |
| `schemas.py` | Pydantic request/response models |
| `auth.py` | bcrypt hashing, JWT creation/decoding, Fernet encryption |
| `deps.py` | FastAPI auth dependencies: `get_current_owner` (owner-only), `get_current_owner_or_app` (owner + app token), `get_principal`, `require_app_permission`, and `reject_cross_site` (CSRF) |
| `compiler.py` | `compile_jsx()` — calls the esbuild CLI to compile a JSX string into an ES module |
| `providers.py` | `BaseProvider` adapters (`ClaudeProvider`, `CodexProvider`) + the `PROVIDERS` registry; identity/auth/env shaping for the SDK runners (`build_env`), and `get_skill_path()`. |
| `claude_sdk_runner.py` | Claude SDK turn runner; passes `cli_path="/usr/local/bin/claude"` so the SDK drives the same pinned binary recovery + cron use |
| `codex_sdk_runner.py` | Codex SDK turn runner (Thread/TurnHandle + steer) |
| `codex_appserver.py` | Small helper module: `codex_sdk_runner.py` imports its one surviving function, `_extract_bash_command`, which pulls the bash command string out of a shell tool item. The SDK runner does its own event/tool classification locally. |
| `chat.py` | `run_chat()` background task: spawns the turn, publishes events, routes persistence through the actor |
| `chat_writer.py` | Single-writer chat-persistence actor — one thread owns the DB session + a FIFO command queue; ALL `Chat.messages` / `Chat.pending_messages` mutations route through it (do not write those columns directly) |
| `chat_queue.py` | Per-chat queue lock + turn-end `drain_and_release` / `promote_pending_messages_locked` + the `TerminalDisposition` state machine; the awaited bridge between `chat.py` and the writer actor |
| `broadcast.py` | `ChatBroadcast` per-chat in-memory event bus; decouples the turn runner from SSE clients |
| `events.py` | Pure data transforms accumulating streaming events into the persisted message structure |
| `compaction.py` | Cross-provider chat compaction (portable plain-text summary; native SDK compaction is within-provider only) |
| `runner_registry.py` | Runner lifecycle registry shared across chat backends |
| `pending_questions.py` | Shared `PendingQuestion` dataclass for AskUserQuestion interception (split out to break the `questions`↔runner import cycle; the registry itself lives in `questions.py`) |
| `tool_summaries.py` | Tool-input summary strings (shared by SDK + subprocess paths) |
| `tool_sources.py` | `normalize_tool_sources()` — normalizes provider web-search results into bounded `{title, url, snippet}` metadata stored on WebSearch blocks and rendered once in the message-level Sources row; an iterative count/depth budget and HTTP(S)-only URL gate keep provider payload cost fixed before SSE or persistence |
| `sdk_emit.py` | Helpers for emitting "unknown" SDK events on the SSE wire |
| `restart_util.py` | `restart_this_worker()` — arms a daemon SIGKILL fallback, then SIGTERMs its own pid; shared by `/api/admin/restart` and `/api/platform/restart` so the two restart paths can't drift. Pairs with uvicorn's `--timeout-graceful-shutdown 10` (entrypoint.sh) — without a bound, an open chat SSE stream held graceful shutdown open forever and the container never cycled (6ac51b0) |

### Mini-apps, storage, files

| File | Role |
|------|------|
| `install.py` | Atomic install + update lifecycle for mini-apps from a manifest |
| `app_git.py` | Per-app git repo (`/data/apps/<slug>/.git`): pristine `upstream` history + a local working branch |
| `app_watcher.py` | File watcher that auto-recompiles a mini-app's source on edit |
| `storage_io.py` | Filesystem helpers for per-app + shared storage; lives apart from `routes/storage.py` so `install.py` can reuse it. Also owns `etag_matches()`, the If-Match CAS compare — RFC 9110-correct (wildcard, weak tags, multi-value) plus deliberate tolerance for a proxy content-encoding suffix (Caddy `encode` rewrites `"<tok>"` to `"<tok>-gzip"`; a strict compare would 412 every compressible CAS write) |
| `fs_locks.py` | In-process async locks serializing storage-tree / source-tree mutations against app uninstall |
| `app_compile_contract.py` | Canonical self-contained mini-app compiler contract, dependency list, and runtime ABI |
| `app_runtime_inject.js` | React + `mobius-runtime` bridge injected into every compiled app bundle |
| `runtime_types.py` | Shared runtime type definitions |
| `net_utils.py` | SSRF-safe URL validation shared by the install fetcher and the proxy |
| `resource_access.py` | Resource-access helpers, incl. `live_app` / `live_app_or_404` (tombstone-aware app resolution) |
| `path_utils.py` | Path-safety helpers |

### Memory, skills, activity, scheduling

| File | Role |
|------|------|
| `memory.py` | `build_memory_block()` — assembles only bounded recent-chat Digests; graph/app data is never injected here |
| `reflection_checkpoint.py` | Reflection's last-run marker (what to review tonight) |
| `activity.py` | Append-only JSONL platform-activity log (app_open, app_install, storage_write, …) |
| `self_reminders.py` | Agent self-scheduling: append-only store of relational check-ins |
| `theme.py` | Theme CSS management and HTML injection |
| `push.py` | VAPID key management and Web Push delivery |

### Recovery (the frozen island)

Recovery is a SEPARATE always-up container, `recoveryd`, not a module inside `backend/app/`. Its code is a distinct package, `backend/recovery/` (baked root-owned to `/app/recovery/`, outside `backend/app/`), deliberately isolated from the SDK/chat stack — stdlib `http.server`, zero `app.*` imports — so a broken platform install cannot take recovery down. The only recovery-adjacent module left in `backend/app/` is `recovery_seed.py` (it mirrors the owner row into a DB-independent seed for a wiped-DB login; see the self-heal section).

| File | Role |
|------|------|
| `recoveryd.py` | The recovery daemon: stdlib `http.server`, zero `app.*` imports; serves `/recover*` on :8001 and owns the Sec-Fetch-Site/Origin cross-site reject |
| `recovery_pages.py` | Dependency-free HTML for the recovery pages |
| `recovery_chat_pages.py` | HTML + page surface for the recovery chat |
| `recovery_chat_runner.py` | Minimal CLI runner for the recovery chat; shares no code with `chat`/`providers`/SDK (runs the standalone `claude` binary as its own subprocess) and appends to its own per-chat jsonl under `/data`, never the `Chat.messages` column |
| `recovery_restore.sh` | Baked git-reset restore tool (`backend/scripts/recovery_restore.sh` → `/app/scripts/`) — what the "Restore platform" button drives |

`recovery_auth.py` (HMAC-cookie auth) and `recovery_db.py` (raw-`sqlite3` owner-row read, no ORM, with a DB-independent `/data/.recovery-owner.json` fallback for a wiped DB) round out the package. See the self-heal section below for the container itself.

### Misc shared helpers

Agent-editable general-purpose modules — several sit on live chat paths, so despite living near `recovery_seed.py` they are NOT part of the frozen island.

| File | Role |
|------|------|
| `bootstrap.py` | First-boot bootstrap (`ensure_bootstrap_apps_installed`) that auto-installs the App Store, Memory, and Reflection; called idempotently from the FastAPI lifespan |
| `chat_log_redaction.py` | Server-side structural redaction for the gated chat-log read API |
| `chat_media.py` | One-way startup migration that moves old chat images and stored URLs onto the canonical `/media/` path |
| `http_caching.py` | Range/206 hardening for revalidating `FileResponse`s |
| `timeutil.py` | `now_naive_utc()` + `SOFT_DELETE_TTL`; SQLite stores naive datetimes (mixing aware/naive `TypeError`s on compare) |
| `presence.py` | Chat-broadcast presence (`has_watchers(chat_id)`) — `push.notify_owner` uses it to skip a push when a live SSE subscriber is already watching |
| `questions.py` | The AskUserQuestion pending-future registry + lifecycle (`_pending` dict; `register`/`deliver_answer`/`get`/`claim`/`claim_if`/`cancel`) — both SDK runners insert into it, `POST /messages` resolves, Stop cancels |

### Routes (`backend/app/routes/`)

Each module exposes a `router`; registration is in `routes/__init__.py`.

| File | Role |
|------|------|
| `auth.py` | Setup, login, CLI provider auth (`/api/auth/provider/*`) — Claude via self-managed PKCE OAuth, Codex via a `codex login --device-auth` subprocess |
| `apps.py` | Mini-app registry CRUD, `/module` and `/frame` serving (ETag revalidation), `POST /{id}/publish` (site snapshot → `/sites/<token>/`), and `DELETE /{id}/data` — wipe an app's runtime storage keeping it installed (no tombstone/recovery window; takes only the innermost `app_storage_lock`, liveness re-checked under it) |
| `chat.py` | `POST /api/chat/stop` — interrupts the agent turn |
| `chats.py` | Chat CRUD + reversible soft-delete with recovery; the chat-load serializer drops tool outputs >4KB to an `output_truncated`/`output_full_len` marker (read-side only — the stored message keeps the full text; blocks ≤4KB or without a message `ts` stay inline), lazy-fetched by `ToolBlock` on expand via `GET /{id}/tool-output?ts=&i=`; also `GET /{id}/agent-context` — read-only inspection of the assembled prompt (system prompt + injected memory / app-context / compaction blocks) |
| `chats_stream.py` | `POST /messages` (starts a turn, returns 202) + `GET /stream` (SSE) |
| `chat_logs.py` | Gated, redacted chat-log read API for mini-apps |
| `storage.py` | Per-app and shared file storage, plus confined immutable blob reads from full commits reachable on a shared repository's `main` branch (`GET /api/storage/shared-git/{repo}?revision=&file=`). The Git route applies the same Memory capability gate, rejects traversal/symlinks/submodules, and never reads the mutable worktree. |
| `secrets.py` | Bounded encrypted secret storage scoped to an app; an app can write/delete/check its own values, while only the owner or owner-scoped agent can decrypt them; no cross-app access or listing surface |
| `fs.py` | Owner-facing filesystem + git oversight API |
| `uploads.py` | Per-chat file upload management |
| `media.py` | Owner-authenticated per-chat image serving from the canonical `/media/` path |
| `proxy.py` | Server-side CORS-bypass proxy for mini-apps |
| `local_services.py` | Guarded loopback proxy plus the shared gateway-origin adapter for owner-trusted backend web apps. Each service requires explicit `upstream_auth` and gateway opt-in; Möbius authority headers are stripped, cookies and redirects stay confined to `/services/<slug>`, the gateway hostname is reserved to enabled prefixes, frame blockers are relaxed only there, and invalid configuration fails closed |
| `standalone.py` | Top-level install/manifest shell for mini-app PWAs. **Known boundary gap:** its current loader executes the app component in the top-level shell origin; it is not yet equivalent to the opaque in-shell app frame |
| `published.py` | Serves published site snapshots at `/sites/<token>/` — token-validated, traversal-confined static files from `/data/published/<token>/` (created by `POST /api/apps/{id}/publish` in `apps.py`; token stable per project) |
| `platform.py` | Owner-gated platform self-update: `GET /api/platform/status`, `POST /apply`, `POST /restart` (drives Settings → Updates; thin caller of `platform_update.py`) |
| `notify.py` | System-event notifications to active broadcasts |
| `notifications.py` | Push notification sending + history |
| `push.py` | Web Push subscription management |
| `theme.py` | `GET /api/theme` — effective theme CSS + bg with default fallback |
| `settings.py` | Owner-level configuration |
| `github.py` | GitHub connect status + read-only REST/GraphQL passthrough for in-product upstream contributions (pairs with `github_auth.py` + the `contributing.md` skill) |
| `self_reminders.py` | Agent self-scheduling endpoints |
| `admin.py` | Admin / introspection endpoints (service-token gated) |
| `debug.py` | Observability: active SDK clients/sessions, broadcasts, chat logs |
| `client_error.py` | `POST /api/client-error` — record an uncaught client/app JS error |
| `recover.py` | Recovery page at `/recover` (reset/backup/rebuild) — frozen island |
| `recover_html.py` | HTML templates for the recovery page (no `router`; used by `recover.py`) |

Note: there is no `routes/ai.py` and no `POST /api/ai`. An older mini-app AI proxy lived there and was removed; mini-apps reach the agent via `window.mobius.chat`, `POST /api/apps/{id}/run-job`, or cron — not a synchronous AI endpoint.

## App execution tiers

Host-mediated device/browser access uses the versioned capability broker; see
[`CAPABILITIES.md`](CAPABILITIES.md) for the manifest, app API, wire protocol,
provider contract, lifecycle rules, and trust-tier escape hatches.

| Tier | Boundary and capability | UX / standalone consequence |
|---|---|---|
| Ordinary mini-app | Shell-owned iframe without `allow-same-origin`; opaque origin, app-scoped JWT, memory-backed localStorage facade and `window.mobius.storage` | Safest default inside the shell. The shell owns install/offline identity; a home-screen shortcut may deep-link through the shell, but opacity alone does not create an independent PWA |
| Packaged nested document | `/app-embeds/by-id/<id>/…`; every response carries CSP `sandbox` without `allow-same-origin`, scoped `Access-Control-Allow-Origin: null`, and no frame denial | For a game/tool build nested below an ordinary wrapper. Relative subresources work online but an opaque child is not controlled by the shell SW, so only the entry document may be SW-cached; readiness must be a source-bound post-commit heartbeat, never iframe `load` or a null-origin prefetch probe |
| Owner-trusted full web service | Shared service-gateway origin distinct from the shell, shell-owned direct adapter, path-scoped host-only cookies and exact shell+gateway ancestor policy; never nested below the opaque wrapper | Lowest-friction path for existing full web apps. The gateway isolates the trust group from Möbius, but services on it share an origin and can reach one another |
| Independent or mutually untrusted service/PWA | Dedicated distinct origin (prefer a same-site subdomain), host-only cookies and exact shell+service ancestor policy | Strong service-to-service isolation plus independent manifest/SW/storage identity; costs one managed origin per isolated service |

For an ordinary mini-app, `window.mobius.storage` is implemented by a narrow
RPC bridge to a runtime in the shell realm. The shell runtime owns IndexedDB,
the read-through cache, and the durable outbox that the opaque child cannot
open. Every request is attributed to an exact mounted `contentWindow`; the host
runtime is keyed by both app id and immutable installation nonce, and token
rotation cannot reuse an old ready or in-flight runtime. Subscriptions are host
desired state, so writes from a buffered sibling frame and refreshes observed by
that host runtime repaint every subscribed frame while detached documents are
removed synchronously.

Opacity simplifies permissions: no ambient owner JWT, shell storage bleed, DOM
reach or cross-app authority. It does **not** by itself improve installability,
offline outboxes, cookies, media APIs or other origin-bound capabilities.

The shared service gateway is intentionally a trust-group boundary, not a
virtual per-path origin. Paths do not partition localStorage, DOM authority or
same-origin fetch. Deployment configures one gateway hostname once (a generated
Railway domain or one self-hosted DNS record), while each service must still opt
in through `local-services.json`. The gateway host serves no shell, API,
recovery or non-enabled service paths.

**Standalone gap (not resolved by the table above).** `/apps/<slug>/` currently
boots the mini-app component directly in a trusted top-level Möbius document and
reads the owner JWT there. The component can therefore read shell localStorage
even though its in-shell version cannot. The bounded follow-up is to turn that
route into a trusted installable outer shell which owns auth/manifest/SW/error
chrome and hosts the existing `app-frame.html` protocol in an opaque iframe.
Until that lands, do not describe ordinary mini-app isolation as applying to
standalone launches or use `/apps/<slug>/` as the security boundary for
untrusted/high-capability code. CubeRun's nested game document remains opaque,
but its current standalone wrapper does not; Tandoor's service surface opens
only through the authenticated shell adapter and is not a standalone mini-app.

## Frontend (`frontend/src/`)

React + Vite. Entry is `main.jsx` → `App.jsx`. `App.jsx` checks setup status and renders one of `SetupWizard` (first boot), `LoginForm` (no token), or `Shell` (authenticated). `Shell` owns drawer state and system-event handling; navigation and theme are extracted to hooks (`useNavigation`, `useTheme`).

### Top-level components (`frontend/src/components/`)

| Component (dir) | Role |
|-----------------|------|
| `Shell/Shell.jsx` | Logo bar, drawer, content area, system events; owns the app-iframe LRU cache (`appCache`, cap 4) |
| `Drawer/Drawer.jsx` | Slide-in nav: current chat, new chat, collapsible history, apps; `InstallSheet.jsx` is the PWA install prompt |
| `ChatView/` | Chat surface (its own subtree — see below) |
| `AppCanvas/AppCanvas.jsx` | Sandboxed `<iframe>` host for a mini-app + the postMessage init handshake |
| `ChatEmbed/` | In-app embedded chat surface (agent chat inside a mini-app). Chats can be project-scoped: `window.mobius.chat({ projectId })` forwards `project_id` on the app-chat create (create-time only — `AppChatPatch` has no `project_id`, so the resume PATCH ignores it); `chat.py` then scopes the injected `<app_context>`/`APP_STORAGE_DIR` to the `projects/<id>/` subdir of the app's storage dir and sets `APP_PROJECT_ID` |
| `SettingsView/` | Theme, provider auth, owner config, and the update/restart surface: platform + shell update status/apply ("Restart to finish"), two-step confirmed server Restart, version display (sha · build date from `/api/version`) |
| `SetupWizard/` | First-boot: account + provider auth |
| `LoginForm/` | Subsequent logins |
| `ProviderAuth/` | Provider-auth UI: `ProviderAuth.jsx` (Claude OAuth), `CodexAuth.jsx` (Codex device-auth), `ProviderRow.jsx` (shared per-provider row) |
| `ProviderModelPicker/` | `CLAUDE_MODELS`/`CODEX_MODELS` constants shared with `ChatSettingsPanel` (the old radio-list picker was superseded by the composer popover and is no longer rendered) |
| `ErrorBoundary/` | Top-level React error boundary |
| `Walkthrough/` | First-run walkthrough |
| `ui/` | Shared primitive UI components |

### Chat subtree (`frontend/src/components/ChatView/`)

The chat is large and self-contained; its hooks live beside it, not in `src/hooks/`. The scroll/spacer/keyboard behavior here is load-bearing — see `ChatView.css` and the lock-in tests in the repo-root `tests/` (`spacer.spec.mjs`, `second-send-pin.spec.mjs`).

| File | Role |
|------|------|
| `ChatView.jsx` | Message history, streaming render, scroll/spacer management, `handleStop` |
| `ChatInputBar.jsx` | Composer input |
| `ComposerPopover.jsx` | The `+` popover: attach files, model/effort/provider picker (rendered by `ChatSettingsPanel.jsx`), and the agent-context inspector entry |
| `AgentContextInspector.jsx` | "What the agent knows" sheet — renders `GET /api/chats/{id}/agent-context`; opened from the `+` popover |
| `MsgContent.jsx` | Per-message rendering: markdown, tool blocks, attachments |
| `ToolBlock.jsx` | Collapsible tool-execution block with status |
| `StreamingMessage.jsx` | The live, in-progress assistant message, incl. the collapsed reasoning disclosure for `thinking` stream events (Claude `thinking_delta` and Codex reasoning deltas publish the identical provider-agnostic event via each runner's `_thinking_event()`); the block is promoted and persisted (`streamPromotion.js` + `events.py`) and re-rendered post-turn by `MsgContent.jsx`, so it is durable, not stream-only |
| `QuestionCard.jsx` | AskUserQuestion UI (gates the turn) |
| `QueuedMessages.jsx` | Tray of messages queued while a turn streams |
| `CompactionCard.jsx` | Compaction summary affordance |
| `Attachments.jsx` | File/image attachment previews |
| `ConnectionStatus.jsx` | SSE reconnection indicator |
| `ManageModelsModal.jsx` | Model management modal |
| `streamReducers.js` | Stream-event reducers |
| `resolveStopResend.js` | Stop → collapse-queue → re-send logic |
| `chatRuntimeState.js` | Pure queue/stream branch helpers (`canFastForwardQueue`, referenced by the steer contract below) |
| `streamPromotion.js` | Pure helpers sealing live stream items into durable assistant messages on promote/steer (`promoteAssistantStream`, `streamItemsHaveRenderableContent`) — ChatView owns *when* promotion happens, this owns *how* |
| `streamSnapshotCache.js` | Versioned `sessionStorage` cache of the visible streaming items (the R4 leave-and-return restore) |
| `msgText.js` | Strips `<agent_experience>` blocks + the hidden attachment manifest from message text |
| `useStreamConnection.js` | SSE connection, text buffering, typewriter drain, sleep/wake reconnect |
| `useScrollMode.js` | Scroll-mode state machine |
| `useVoiceInput.js` | Web Speech API with Android-Chrome workarounds |
| `useFileUpload.js` | File-upload state + API calls |
| `hooks/usePendingQueue.js` | Owns the pending-queue state + all its mutations (optimistic vs server-confirmed `serverTs` rows); its `pendingMessagesRef` is what `handleStop` snapshots and the steer/fast-forward gate reads |
| `hooks/useBridgePartial.js` | One-shot mount-time decision: REPLACE the kept partial of an in-flight turn on first promote vs APPEND a new assistant message (ts-keyed, not role-keyed) |
| `markdown/` | `BlockRenderer.jsx`, `blocks.jsx`, `InlineContent.jsx`, `ImageLightbox.jsx`, `highlight.js` (lazy highlight.js), `math.js` (KaTeX) |

### Hooks (`frontend/src/hooks/`)

| Hook | Role |
|------|------|
| `useNavigation.js` | Navigation stack, pushState/popstate, the Navigation API (back-stack contract in *Navigation back-stack + drawer model* below) |
| `useTheme.js` | Theme CSS fetch, `@import` extraction, CSS-variable injection |
| `useSystemEventStream.js` | System-event SSE consumed by `Shell` |
| `useOnlineStatus.js` | Connectivity verdict (page-side `/api/health` probe; feeds SW connectivity) |
| `useProviderAuthStatus.js` | Provider auth status polling |
| `usePushSubscription.js` | Web Push subscription after login |
| `queries.js` | TanStack Query setup + query definitions |

### App runtime, service worker, libs

| File | Role |
|------|------|
| `frontend/public/mobius-runtime.js` | The `window.mobius` runtime injected into mini-apps; used by the opaque in-shell iframe and the current (not yet opaque) standalone loader. Offline outbox + read-through cache live here |
| `frontend/public/app-frame.html` | The opaque mini-app frame: error UI, parent module broker, runtime bootstrap, and postMessage isolation |
| `frontend/src/sw.js` | Service worker: precache + cache strategy, incl. the offline-capable-app handler |
| `frontend/src/sw-cache-policy.js` | Authoritative cache-route policy (see *Service worker + offline* below) |
| `frontend/src/lib/` | Cross-cutting helpers: `appToken.js`, `chatEmbed.js`, `themeService.js`, `onlineStatus.js`, `navHistory.js`, `errorLog.js`, etc. |

**Mini-app modules are self-contained.** `app_compile_contract.py` points esbuild at the pinned production dependencies in `frontend/package.json`, injects React plus `mobius-runtime`, and bundles every used static import into one ESM artifact. The opaque frame asks its exact controlled parent to fetch and transfer that artifact, so a cold offline load performs no dependency subrequests. A compiler banner carries both a host ABI and an artifact revision: bump the revision to rebuild installed bundles for additive runtime changes, and bump the ABI only when old and new hosts are incompatible. Public `/vendor/` files remain only for true browser assets that code refers to by URL (currently the pdf.js worker, KaTeX CSS/fonts, and the D3/Pixi classic scripts); they are not a package resolver.

## Where do I make a change?

| Task | Start here |
|------|------------|
| New API route | New module in `backend/app/routes/` exposing `router` → register in `routes/__init__.py` (`_load(...)` line + `__all__`) → mount in `main.py` |
| New ORM table / column | `backend/app/models.py` plus an idempotent `ALTER TABLE` entry in `database.py:run_migrations()` (runs at boot; `create_all` never ALTERs an existing table) |
| Change request/response shape | `backend/app/schemas.py` + the owning route |
| Add an auth dependency / change CSRF | `backend/app/deps.py` |
| Persist anything chat-domain | A domain command in `backend/app/chat_writer.py` — never write `Chat.messages`/`Chat.pending_messages` directly |
| Add an AI provider | New `BaseProvider` subclass + a row in `PROVIDERS` (`backend/app/providers.py`) plus the matching SDK runner |
| Change chat streaming UI | `ChatView/ChatView.jsx` + `ChatView/useStreamConnection.js` (+ `streamReducers.js`) |
| Change chat scroll/spacer/keyboard | `ChatView/ChatView.jsx` + `ChatView.css`; run the spacer/send-pin tests in repo-root `tests/` |
| Change drawer / back-stack nav | `frontend/src/hooks/useNavigation.js` + `Shell/Shell.jsx` (read *Navigation back-stack + drawer model* below first) |
| Change the mini-app iframe / cache | `AppCanvas/AppCanvas.jsx` + `Shell/Shell.jsx` (`appCache`); ETag logic in `routes/apps.py` |
| Add an app-runtime capability | `frontend/public/mobius-runtime.js` + `app_runtime_inject.js`; bump the compiler artifact revision for additive compiled-bridge changes, or the ABI only for host-incompatible changes |
| Add a supported app package | Pin it in `frontend/package.json`, add it to `BUNDLED_RUNTIME_LIBS`, and run the compiler/offline-frame contracts |
| Change offline / SW behavior | `frontend/src/sw.js` + `frontend/src/sw-cache-policy.js` (read *Service worker + offline* below first) |
| Change the in-product agent's instructions | `skill/core.md` (constitution) or `backend/scripts/seed-skills/*.md` (per-task skills) — see below |
| Change a built-in core app (Memory / Reflection) | The catalog repo (`mobius-os/app-<slug>`) is the source of truth — `core-apps/` is a committed snapshot, never hand-edited. Bump the pinned commit in `core-apps/SOURCES`, run `scripts/sync-core-apps.sh`, commit the diff; CI (`scripts/check-core-apps-sync.sh`) fails on drift. The snapshot is baked to `/app/core-apps` (Dockerfile) and installed at boot by `backend/scripts/install-core-apps.sh` (which prefers `/data/platform/core-apps` when the platform clone exists, falling back to the baked `/app/core-apps` floor) |
| Theme CSS / tokens | `backend/app/theme.py` + `routes/theme.py` + `frontend/src/hooks/useTheme.js` |

## In-product agent context — three layers

The in-product agent is a first-class reader of this code, and its behavior has three layers. (1) **Base constitution** — the live platform checkout's `skill/core.md`; `chat._read_skill_text()` caches only this tracked platform text for the process lifetime, so edits and platform updates take effect after a server restart. `/app/skill/core.md` is only the image-baked degraded-boot fallback when the live checkout is unavailable. (2) **Installed system-app contributions** — a manifest may declare one root-level `system_prompt` markdown file only with explicit `system_app: true`. When a chat starts its first turn, live (`deleted_at IS NULL`) app fragments are composed in stable id order with its effective base constitution and stored as one content-addressed prompt snapshot. Every later turn, provider switch, and compaction uses those exact bytes. Install, update, and uninstall affect chats started afterwards, while an existing chat keeps the prompt it began with. (3) **On-demand skills** — `/data/shared/skills/*.md`; base skills are seeded create-if-absent, while app-owned skills arrive through manifests and are deactivated/restored with their owner app. Independently of optional apps, every chat maintains its name, a bounded `## Digest`, and an uncapped cumulative `## Summary` under `/data/shared/memory/chats/<id>/index.md`. New sessions receive only recent descriptions + Digests. `chat_note.py` is the tool-free, compare-and-swap turn-end writer, and compaction prefers the chat's cumulative Summary. The optional Memory app owns graph instructions, its skill, reader, seeds, builder, Git publisher, and retrieval telemetry; no router/fact note is injected. Uninstall changes future chat prompts and removes the skill/jobs while leaving existing prompt snapshots and core chat summaries intact.

## Data layout (`/data/` volume)

```
/data/
├── db/ultimate.db          SQLite database
├── compiled/app-*-<sha256>.js  immutable esbuild output selected by each App row
├── apps/<slug>/index.jsx   agent-editable JSX source (keyed by app slug)
├── apps/<slug>/...          per-app runtime data + per-app git repo
├── app-secrets/<id>/       encrypted app-scoped credentials (outside app repos)
├── shared/                 cross-app shared files (theme.css, skills/, memory/)
├── shell/                  agent's editable shell copy (src/ + dist/)
├── cli-auth/claude/        CLI credentials
├── cron-logs/              output from scheduled task scripts
├── published/<token>/      published site snapshots (shareable /sites/<token>/ URLs)
└── service-token.txt       owner JWT used only by the platform job wrapper (chmod 600)
```

`/data` is itself a git repo owned by the `mobius` user, tracking `shared/memory/` and `shared/skills/` with a nightly safety-net commit, so a bad memory consolidation or skill overwrite is recoverable. Inspect it as that user (`docker exec -u mobius ... git -C /data ...`) — as root it dies with "dubious ownership," which reads misleadingly as an empty/non-repo tree.

## Boot, self-heal, and how each layer updates

**Layers + where they live:** core platform = `/data/platform`, a git repo whose
backend is served from `backend/` and frontend from `frontend/dist`; mini-apps
(`/data/apps/<slug>`, each a git repo); recovery (the frozen island, below).
Runtime trees are gitignored (db, compiled, app-secrets, cli-auth).

**Updates** flow through git. `backend/app/platform_update.py` is clone-native:
`/data/platform` is a real `git clone` of the canonical repo, so an update
`git fetch origin`s and rebases the local `main` (the agent's edits) onto the new
`origin/main` — committing any stray working-tree edits first so the rebase can
only replay them, aborting back to the last-served commit on conflict, and
running a post-rebase `import app.main` probe that rolls back rather than serve a
broken tree. (It reuses `app_git`'s isolated git env + `commit_local` but drops
the pre-slice-B baked-floor `upstream`-record model; card refs below point at the
maintainers' local `.pm/` backlog, gitignored and absent from a fresh clone.) The
served bundle is `/data/platform/frontend/dist` if a complete build exists else
baked `/app/static` (the #1 deploy gotcha — the volume masks a new baked dist;
always byte-check the served hash). Mini-apps update through each app's git repo + the store; freshness
rides ETags (`/module` = `updated_at` µs; `/frame` = compound
`updated_at`+content-hash). The backend has the same served-vs-baked gotcha as
the shell: a new image's `sha` advances on every deploy even while
`/data/platform` keeps serving the previous deploy's Python. `GET /api/version`
(`backend/app/main.py`) therefore reports the image identity (`sha`, `shell_sha`)
plus the SERVED-platform identity — `serving_source` (from the `/tmp/serving-source`
stamp `entrypoint.sh` writes at boot), `platform_sha`, `platform_dirty`,
`baked_sha`, `served_frontend`, and `frontend_source` — and
`scripts/deploy-prod.sh`'s verify step asserts these match its sync decision
(it also hard-blocks deploying a checkout strictly BEHIND
`origin/main`).

**Built-in apps (Memory, Reflection)** come from the tracked top-level
`core-apps/<slug>/` trees — committed SNAPSHOTS of their catalog repos
(`mobius-os/app-*`), pinned by commit in `core-apps/SOURCES`, baked to
`/app/core-apps` (Dockerfile), and registered/re-synced at boot by
`backend/scripts/install-core-apps.sh` (which prefers `/data/platform/core-apps`
when the platform clone exists, else the baked `/app/core-apps` floor;
backgrounded post-launch by the entrypoint; registration goes through the API
with the service token). Never edit
`core-apps/` directly: update the catalog repo, bump `SOURCES`, and run
`scripts/sync-core-apps.sh`; CI (`scripts/check-core-apps-sync.sh`) fails the
build on drift.

**Recovery and self-heal.** Recovery is deliberately outside the editable
platform. `recoveryd` is a separate `restart: unless-stopped` container with its
own cgroup, read-only root filesystem, and root-owned frozen code under
`/app/recovery`. The edge proxy routes `/recover*` to it before the platform
catch-all, so a broken or OOM-killed app container does not take the recovery UI
down with it. The recovery container mounts `/data` to repair the instance, but
the platform container does not mount recoveryd's private `/recovery-live`
volume.

The dashboard exposes two complementary paths:

- **Reasoned repair:** `/recover/chat` runs a fresh Claude or Codex recovery
  agent as root. Its runner, auth, pages, and per-chat JSONL history are frozen
  recovery modules with zero `app.*` imports, so broken production chat code is
  not in the recovery dependency chain. The root filesystem remains read-only;
  the agent can repair `/data/platform` and owner data but cannot rewrite its
  own lifeboat.
- **Deterministic floor:** `Restore platform` resets uncommitted changes in the
  served clone; `Reset to baked floor` quarantines the clone and atomically
  reseeds it from `/app/platform-baked`. recoveryd writes
  `.recover-pending` before `.platform-restart-requested`; the platform
  entrypoint consumes those files as root and its poller cycles pid 1 without a
  Docker socket.

Recovery auth normally reads the owner bcrypt hash through raw SQLite and falls
back to `/data/.recovery-owner.json` only when the DB is unreadable. The
platform writes that seed after owner setup, refreshes it idempotently at boot,
and deletes it on factory reset. Recovery session HMAC state and an optional
self-updated recovery bundle live on the recoveryd-only `/recovery-live`
volume. An update is cloned only from the pinned recovery repository, hardened
root-owned, syntax-checked, and atomically swapped; a persisted three-attempt
guard quarantines a trusted live bundle that crash-loops back to the baked
floor.

Normal platform boot serves `/data/platform/backend` directly after an import
probe. It fetches `origin/main`, commits stray local edits, and rebases them onto
the update; a conflict or failed post-rebase import returns to the exact
pre-reconcile commit and leaves a visible flag. An invalid existing clone serves
the baked backend without overwriting the broken tree. Independently, three
consecutive boots that never reach `/api/health` quarantine the served clone and
reseed it on the next attempt. These mechanisms recover code; owner-data disaster
recovery is the separate `backup-data.py` / `restore-data.py` flow and is not
automatically armed by installing Möbius.

## Chat scroll + steer contract

**Owner-authoritative contract — v1.6 (2026-07-22).** This section is the
canonical source of truth for how a chat scrolls and steers. When implementation,
comments, and this contract disagree, the implementation/comments are the bug:
fix behavior to match this contract. If a real case is unspecified or the desired
behavior changes, agree the new rule with the owner first, update this versioned
section explicitly, and add or change the matching regression test; never silently
rewrite the contract around the behavior that happened to ship.

The Chat Issue Reporter mini-app carries an owner-readable snapshot of these rules
and attaches their rule ids to new diagnostic chats. The Playwright lock-in specs
(`tests/send-rule`, `spacer`, `second-send-pin`, `steer-queued`, `stream-reconnect`,
`backend/tests/test_chats_stream_steer`) encode this:

- **R0 — Two modes; two explicit auto-scroll entrances.** A chat is either in **auto-scroll**
  (`FOLLOW_BOTTOM`, following the real-content tail as the reply streams) or **hold**
  (`PIN_USER_MSG` or `ANCHOR_AT`, staying at a pinned prompt or frozen reading
  position). Auto-scroll engages only through (a) the gesture-gated scroll handler
  after the user manually reaches an ordinary bottom with no reservation remaining,
  or (b) the live-send pin handoff when the streaming reply has consumed its exact
  reserved room. The physical bottom while reservation remains is the prompt's pin
  target, not the real-content tail: reaching it preserves (or repairs) pin hold and
  waits for the spacer-exhaustion handoff. A viewport /
  keyboard change, foreground return, mount, or chat restoration must never create
  auto-scroll.
- **R1 — Permanent exact reservation.** Every non-empty chat keeps enough dynamic
  bottom spacer for its latest visible user message to reach the viewport top,
  including after leaving and reopening the chat. The reservation is exact — no
  extra scrollable blank beyond that target — and shrinks as the reply fills it.
  `FOLLOW_BOTTOM` follows real conversation content, excluding the reservation, so
  a short restored chat cannot open on an empty viewport.
- **R2 — One send rule everywhere.** The first visible user message always pins to
  the viewport top. Every subsequent direct, queued, promoted, or steered message
  pins when its submit-time DOM snapshot is at the real-content tail. Geometry is
  authoritative because `ScrollMode` can lag an input/layout frame; requiring both
  made identical bottom sends behave inconsistently. A real user scroll after
  submission invalidates an automatic delayed queue promotion (a tap without
  scrolling does not). Explicit fast-forward is itself the visibility action, so it
  captures fresh bottom geometry when pressed; a real scroll during its request
  invalidates that snapshot. Missing delayed intent degrades to hold, never to an
  inferred pin.
- **R3 — Pin holds until the reservation is filled.** A legitimate live pin
  transitions to `PIN_USER_MSG`, not immediately to `FOLLOW_BOTTOM`; the response
  first grows below the prompt without moving it. Exactly when the streaming reply
  consumes the reservation (spacer reaches zero), the armed pin hands off once to
  `FOLLOW_BOTTOM`. If the reply settles while any reservation remains, that handoff
  is retired only after committed geometry is stable across consecutive layout
  frames, and the prompt stays pinned; a one-frame terminal check cannot disarm
  just before final buffered text fills the reservation. Later idle layout changes
  cannot create follow. A non-pinning send preserves the exact reading anchor. `PIN_USER_MSG`
  survives the complete
  mobile-keyboard open/close cycle even though the full-height reservation makes its
  scroll position temporarily look away from the physical bottom; viewport geometry
  is not reader intent, so apart from the explicit filled-reservation handoff, only a
  gesture-gated reader scroll may retire the pin.
  Terminal promotion makes this decision against the committed settled DOM,
  before paint, so a final browser clamp cannot race the pin or its exact
  filled-reservation handoff.
- **R4 — Exact leave-and-return.** Leaving, backgrounding, and returning restore the
  same visible anchor, even if the chat had been auto-scrolling and content grew while
  it was inactive. Return never jumps to the new tail and does not restore
  auto-scroll; the user must manually reach the bottom again. If there is no saved
  location, or its target row is no longer available, return shows the latest real
  conversation content at the viewport bottom once as a settled anchor. It must not
  manufacture a top-of-chat location or engage live following. That automatic tail
  fallback is not a reader-chosen location and must not be persisted on pagehide or
  shell reload; only a deliberate scroll/send/pagination position earns restoration.
- **R5 — Reader owns gestures and layout-only sends.** From the first wheel/touch/key
  input until its scroll event lands, no layout path may write `scrollTop`: stream
  resize, spacer handoff, terminal promotion, catch-up, and viewport/keyboard resize
  all share the same ownership gate. Only an actual gesture-driven scroll invalidates
  delayed send intent. Send is a newer explicit action than the gesture that
  positioned it: after submit snapshots the synchronous geometry, a delayed browser
  `scroll` event from that pre-send gesture cannot cancel the new pin. Any input begun
  after submit opens fresh reader ownership and still wins. Queueing behind a live
  turn adds no transcript row, so it
  freezes the visible message before the queue tray/composer/keyboard reflow; the
  separately captured submit snapshot still controls the row when it is promoted.
  Never replace the input-to-first-scroll handoff with a fixed short window: under
  rendering load the browser may deliver that scroll later. Ownership begins only for
  inputs whose default action can scroll the transcript; ordinary typing, Enter, and
  control activation are not reader scroll intent. Pointer/touch release handles taps,
  while scrolling-key input that produces no scroll releases on the next frame. Wheel
  input gets that early release only when its direction is exactly clamped at the
  matching scroll edge. An elapsed frame is not evidence that an in-range wheel was a
  no-op: renderer/compositor load can update geometry before the main-thread `scroll`
  handler runs. Only after a real scroll lands does the short momentum window begin. A
  bounded dead-man remains the final escape hatch for any interrupted gesture.
- **R5a — Attention nudges reveal the usable tail.** Tapping an offscreen question
  or paused-turn nudge is an explicit one-shot reading action: it lands at the
  physical tail, including the list's composer-clearance padding, so the card's
  Submit or Resume control is visible above the overlaid composer. It becomes a
  settled `ANCHOR_AT` hold rather than `FOLLOW_BOTTOM`; revealing an attention
  control must not manufacture future live-follow intent. Both actions route
  through the scroll controller instead of calling `scrollIntoView`, because
  viewport intersection alone cannot detect that the absolutely-positioned
  composer is covering the target.
- **R6 — One lossless active assistant row.** Live stream items, a persisted partial,
  and the settled transcript are alternate sources for one active assistant row, not
  separate answers. The answer response declares this ownership independently as
  `answer_turn: "same" | "new"`: an in-process question answer (`answer_delivered`)
  resumes that same row and turn, so answering must not retire its source bridge.
  Submitting an in-message answer is also a deliberate reading action: before the
  card enters its pending state or output resumes, the controller snapshots the
  currently visible message and its exact viewport offset as `ANCHOR_AT`. Resumed
  output grows without dragging the reader, even when the chat had been following
  the tail before Submit. If a mobile viewport grows before that output arrives,
  the dynamic spacer temporarily reserves enough room to keep the anchor target
  reachable; the reservation disappears as real content replaces it. A failed
  answer keeps that settled reading anchor for the retryable card rather than
  manufacturing follow intent again.
  The source handoff
  preserves the question, its answer, and every pre/post-answer thinking, tool, and
  text block in event order, without hiding, duplicating, or reordering them. Only a
  recovered answer whose POST returns `started` creates a new hidden continuation.
  Switching sources preserves the active row's anchor identity and writes no scroll.
- **R4a — Attention nudges reveal the usable tail.** Tapping an offscreen question or
  paused-turn nudge lands at the physical tail, including composer-clearance padding,
  so the real Submit or Resume action is visible above the overlaid composer. This is
  a settled `ANCHOR_AT` hold, not `FOLLOW_BOTTOM`; one-shot navigation must not create
  live-follow intent for later content. It routes through the scroll owner rather than
  `scrollIntoView`, whose viewport intersection cannot detect composer coverage.

The transition table is intentionally exhaustive; adding a new send or lifecycle
path means routing it through the same entries rather than inventing another rule:

| Event | Before | After | Scroll write |
|---|---|---|---|
| First direct/queued/steered user row becomes visible | any | `PIN_USER_MSG` | New row to top |
| Later send submitted at real-content tail (mode may be one frame stale) | any | `PIN_USER_MSG` | New row to top |
| Later send submitted anywhere else | hold or stale follow | `ANCHOR_AT`/existing hold | None |
| Reader reaches physical bottom while live reservation remains | any | armed `PIN_USER_MSG` | User-owned; then keep prompt fixed |
| Reader reaches physical bottom while idle reservation remains | any | settled `PIN_USER_MSG` | User-owned; keep prompt fixed |
| Reader reaches bottom with no reservation remaining | any | `FOLLOW_BOTTOM` | User-owned |
| Reader scrolls manually away from bottom | any | `ANCHOR_AT` | User-owned |
| Reply grows while an armed live pin still has reserved room | pin hold | same pin hold | Keep prompt fixed |
| Streaming reply consumes the armed pin reservation | pin hold | `FOLLOW_BOTTOM` | Follow real-content tail |
| Short reply settles before consuming the reservation | armed pin hold | settled pin hold | Keep prompt fixed; retire automatic handoff |
| Other layout grows while pinned or anchored | hold | same hold | Reapply only the held target |
| Viewport/keyboard changes | `PIN_USER_MSG` | same `PIN_USER_MSG` | Reapply pin after resize; never infer intent from keyboard-open geometry |
| Viewport/keyboard changes | follow or anchor hold | same follow if still at tail, otherwise hold anchor | Never creates follow |
| Chat exits/backgrounds/returns | any | `ANCHOR_AT` | Restore exact saved anchor |
| In-process question is answered | any | `ANCHOR_AT` on current visible row; same active assistant row | Hold exact visible anchor through card reflow and resumed output |
| Live assistant row settles to the durable transcript | any | same mode and row identity | None (except R3's exact spacer handoff) |
| Offscreen question or paused-turn nudge tapped | any hold | `ANCHOR_AT` at physical tail | User-requested one-shot move; clears the overlaid composer |

Controller structure is part of the contract, not an implementation detail:

- `ChatView` may read `modeRef` for a submit snapshot but must not assign it.
  It emits send, queue, pagination, and lifecycle events through the semantic
  methods returned by `useScrollMode`.
- Every live mode mutation goes through `transitionMode`; every mode-owned
  `scrollTop` write goes through `writeMode`. The exported `applyMode` executor
  is for the controller and pure unit tests, not a second live writer.
- The gesture-gated `scroll` event reads physical-bottom geometry directly.
  Do not reintroduce a sentinel or asynchronous observer as a second bottom
  authority: its delayed state can contradict the viewport that caused the
  event.
- `window.__mobiusChatScrollTrace` keeps bounded, content-free transition and
  actual-write history for diagnosis. It records mode kinds, armed state, and
  geometry only—never message text, keys, or cids.

Thinking/reasoning deltas also carry a semantic `segment_id` end to end. Token
deltas with the same id concatenate verbatim; a new provider summary/content index
adds a paragraph boundary before live rendering and durable reduction. The renderer
repairs the legacy glued-bold seam (`****`) for already-saved chats, while legacy
events without ids retain raw token concatenation so mid-word fragments are never
split heuristically.

The live thinking timer is runner-time, not component lifetime. Each delta keeps
its server `ts`; `catch_up_done` carries the server clock at replay completion, and
the frontend re-anchors only a trailing live thinking block from those two server
values before committing the replay. Reconciliation may move that clock forward
but never backward. Do not derive a remounted timer solely from `Date.now()` or the
client arrival time of replayed deltas: catch-up arrives as a burst and that makes a
minutes-old turn visibly restart at one second.

Every visible user row also makes R1's reservation current, whether or not that row
pins. Reservation lifetime and pin decisions are independent.
- **A restored send is one logical message.** The frontend scopes the draft
  identity to the chat and reuses its client-minted `cid` when an ambiguous
  failed POST restores an unchanged composer. The route checks that durable
  identity before queue or provider side effects; `StartTurn`,
  `AppendPending`, and steer persistence retain actor-level de-duplication as
  backstops. A cid already in the transcript is acknowledged without appending
  a row or waking the provider. A cid still pending keeps its existing queue
  position behind an active turn; an idle stale queue follows the normal
  single-run self-heal. If a later turn is active, retry reconciliation
  preserves that unrelated live stream.
- **Steer = separate rows, one turn.** Steered queued messages render as separate
  transcript rows in send order (`insertMessageBatchByTs`), never one stranded
  after the reply. The agent receives them joined by `\n\n` (clean paragraphs,
  not a `\n` blob). The request binds to specific queued rows by their stable
  `cid` (`consume_pending_cids`; `_selected_force_steer_pending` selects by cid) —
  the earlier byte-for-byte content match existed only because no shared id
  crossed the wire, and is gone. The fast-forward button shows only when every
  queued row is server-confirmed (`canFastForwardQueue`; the `serverTs` flag).
  A steer landing before any renderable assistant output seals nothing — the
  empty/whitespace pre-steer segment is dropped symmetrically on the live path
  (`streamPromotion.streamItemsHaveRenderableContent`) and the persisted path
  (`events.blocks_have_renderable_content`, gating the seal in `chat.py`), so no
  stray empty assistant bubble precedes the steered row; a single real token still
  seals, correctly placed before it (card 166). Keep the two predicates aligned.
- **Regression guards (owner-observed prod bugs):** an at-bottom send must not land
  mid-viewport; a steered row must not render after the agent's reply.

### Automatic continuation after limits and planned restarts

Automatic continuation reuses one durable transition. Provider-limit exits mark
their exact `ChatRun` as `parked` until the parsed reset time; a planned restart
that successfully stops a live handle marks that exact run `parked` with
`park_reason="restart"` and a due time of now, before SIGTERM. The next process
runs the same due-park sweep immediately. There is no restart ledger and startup
never infers intent from transcript text.

| Event | Durable result | Boot/sweep result |
|-------|----------------|-------------------|
| Provider usage/rate limit | exact run `parked` until reset | notify; continue if the chat policy is on |
| Planned restart, handle stopped and exact transition committed | exact run `parked`, reason `restart`, due now | continue immediately if eligible |
| Crash, stuck handle, missing exact token, or failed park commit | generic `running` evidence remains | reconcile to a manual resumable interruption |
| Policy off, unanswered question, app-owned run, or app-queued work | due park resolves without an automatic send | notify/manual owner action |
| Owner sends, switches provider, deletes the chat, or a newer run wins | old park is superseded by the existing latest-run fence | no stale continuation |

Eligibility is rechecked under the per-chat transition lock immediately before
promotion, and the global idle gate permits only one automatic turn at a time.
The provider still receives a synthetic user `continue`, but the durable row is
tagged `kind="auto_continuation"` with reason `restart` or `usage_limit`; the UI,
copy behavior, title selection, time context, compaction, provider-switch
handoff, chat-note summarization, and redacted chat logs treat it as a product
marker rather than owner speech.

The sweep is cheap: one indexed due-row query immediately at boot, on
`chat_run_finished`, and on a 60-second fallback. It does not create per-chat
workers or poll at a short interval. The shared chat-local policy retains its
legacy wire name `auto_resume_on_limit` for compatibility, while product copy
states that it covers both limits and restarts.

### Tool output rendering

Tool runs are **grouped** so the reader sees at a glance what is running vs finished
(`ToolActivityGroup` folds adjacent runs into one collapsed-by-default card; per-tool
status only ever goes `running → done`, with failure derived from a nonzero exit
code, never a block status). Output is **lazy**: the chat-load payload ships a reduced
form (outputs over ~4KB are dropped to a length marker in the `routes/chats.py`
serializer), and the FULL output is fetched only when the block is expanded (`GET
/api/chats/{id}/tool-output`). Small outputs stay inline; live streaming is unchanged.

### Chat summary + continuity contract

Each chat maintains a **growing per-chat note** at
`/data/shared/memory/chats/<chat-id>/index.md` — a bounded `## Digest`, durable
facts + the partner's intent, an uncapped cumulative `## Summary`, and a one-line
**gist that IS the chat title**
(`backend/scripts/chat_note.py` summarizer subagent: transcript in the prompt, no
tools). This note is **core continuity** — it exists and is useful even when the
Memory app is not installed. Its consumers:

- **Short-term continuity into new chats.** A fresh chat opens with only the gist and
  bounded Digest from the ~10 most-recently-modified chats
  (`backend/app/memory.py`); the fenced path lets the agent deliberately open a
  relevant full note. Facts and cumulative Summaries are not injected.
- **Knowledge graph (installed Memory system app).** The app requests structurally
  redacted chat text through its declared API permission, writes a complete graph to
  a same-filesystem staging tree, and atomically advances a JSON `.ready` pointer to
  an immutable generation containing `mocs/`, `notes/`, and `graph.json`. Its confined
  reader pins one generation and returns cited snippets on demand. The graph is not
  platform code; base boot provisions only the per-chat summary surface
  (`backend/scripts/init_chat_summaries.py`).
- **Reflection.** Without the Memory app, the per-chat summaries are what Reflection
  reads.
- **Compaction + provider switch.** The cumulative Summary is the source for compacting a
  long chat and for the provider-switch handoff below — preferred over a from-scratch
  default compaction.

### Provider switch (compaction handoff)

Sessions are not portable across providers, so switching provider mid-chat uses
an **incoming-provider handoff**: the composer confirms and POSTs the target
provider, model, effort, and a stable switch id to
`/chats/{id}/provider-switch`. A successful response is explicitly versioned as
`provider-switch-v1` and echoes both the switch id and target provider; a generic
2xx response is not authoritative. The bodyless `/chats/{id}/compact` route
remains as a rolling-upgrade bridge for older clients that compact and then
PATCH the provider.

The incoming provider runs a disposable, tool-free synthesis turn over the complete
running `## Summary` plus the complete current transcript. Large sources are folded
through bounded progressive synthesis turns so no middle interval is silently
omitted. The writer actor then stores that portable brief, changes
provider/settings, clears the outgoing session, and supersedes outgoing
`parked`/`resume_pending` runs in one conditional transaction; sends, settings
PATCHes, app-chat PATCHes, and auto-resume share the same per-chat transition lock,
while a Summary or transcript change invalidates the commit. Provider-switch UI
state is keyed by chat outside the keyed `ChatView`, so navigation cannot unlock a
handoff or lose its idempotent retry id. The brief is replayed into the incoming provider's first
real turn as a `<compacted_chat>` block, so the new agent continues rather than
starting cold. Same-provider model swaps skip the handoff because their session
context is preserved.

### Staying aligned (enforcement)

This section is the **owner-authoritative source of truth** for chat UX; the
gitignored `CLAUDE.md` / `docs/*` copies must not diverge from it (when they do, this
wins). Alignment is currently enforced by the tracked unit and Playwright lock-in
specs above plus `chatContract.js`'s pure geometry predicates. Three additional
harnesses have been designed but are **not present in this repository yet**: a
runtime chat-contract monitor on the live shell (`208`), a deterministic chat-states
gallery with geometry goldens (`209`), and an SSE event-replay harness (`210`). Do
not cite those planned harnesses as current coverage. Changing a rule here means
updating a matching tracked test in the same change.

## Stop-chat contract

Stop is a two-layer contract: the backend interrupts and clears, while `frontend/src/components/ChatView/ChatView.jsx:handleStop` owns the user-visible collapse-and-resend behavior. On entry, `handleStop` synchronously guards against double clicks, snapshots `pendingQueue.pendingMessagesRef.current`, joins queued text with a single `\n`, dedupes attachments by `name`, bumps `fetchGenRef`, and clears the pending queue before awaiting `/api/chat/stop`. The endpoint `backend/app/routes/chat.py:chat_stop` returns `{"stopped": bool, "cleared_pending_cids": [...]}` from `chat.py:stop_chat` (cleared-set identity is the stable `cid`; `ts` is display metadata), and the frontend runs that through `resolveStopResend()` for both clean-stop and timeout branches: `null`/missing `cleared_pending_cids` falls back to the whole snapshot, `[]` resends nothing, exact matches resend only those queued rows, and an unmatched cleared cid falls back to the whole snapshot rather than dropping work.

`backend/app/chat.py:stop_chat_for` is an interrupt primitive, not a queue-drain primitive. It snapshots the generation, calls `bump_run_generation(chat_id)` before killing handles, registers `_clear_after_terminal_generation` when handles exist, clears pending under `chat_queue.get_lock()`, cancels any live `app.questions` pending question, then calls each runner handle's `stop(timeout=2.0)`. If every handle stops it unregisters them and finalizes the broadcast (discarding `_starting`); the stuck run-marker is cleared via the actor only on the no-handles path — active handles hand that clear to `run_chat`'s finally block. If any handle times out it leaves the registry entry and broadcast intact for runner-side teardown and returns `stopped=False` — in that branch the frontend must NOT disconnect or start a second run. Instead, if `resolveStopResend()` returns text, `handleStop` calls `doSend(..., { pin:false })` while `isStreamingRef` is still true, so the message follows the queue path and is re-persisted as pending.

The generation bump is the key invariant. A dying `_run_chat_impl` rechecks ownership in its terminal path; after Stop it must resolve to `STALE_NO_ACTION` (or the Stop-handoff cleanup), never promote pending or schedule a backend continuation behind the frontend's resend. Do not refetch pending from the server after Stop to rebuild the resend — Stop already cleared the durable queue, so the local snapshot is the only source that preserves text + attachments; and do not resend the full snapshot unconditionally on `stopped:false` — the natural turn-end drain may already have consumed some rows, and `cleared_pending_cids` is the only guard against duplicate follow-up work.

## AskUserQuestion interception

AskUserQuestion is a shared pending-future lifecycle plus a shared `question` stream event; Claude and Codex differ only at the SDK boundary. `backend/app/pending_questions.py:PendingQuestion` carries `question_id`, `questions`, `future`, and optional `run_token`; `backend/app/questions.py` owns the module-level `_pending` registry (`get`, `claim_if`, `cancel`). Claude registers the pending question in `claude_sdk_runner.py:can_use_tool` for the `AskUserQuestion` tool, persists the card via `_ChatEventSink.publish_question()`, awaits the future, and returns `PermissionResultAllow(updated_input={questions, answers})`. Codex installs `_install_request_user_input_handler()` on `codex._client._sync._approval_handler`, enables `features.default_mode_request_user_input=true`, handles `item/tool/requestUserInput`, marshals from the SDK worker thread into the loop with `run_coroutine_threadsafe` (a ~420s bridge timeout), and translates Möbius's text-keyed answers into Codex's id-keyed `{answers:{qid:{answers:[...]}}}` shape.

The answer POST is intercepted before normal send handling in `backend/app/routes/chats_stream.py:send_message` whenever `body.answers` is truthy. The route waits ~500ms for a just-broadcast pending entry, checks `question_id` identity when supplied, persists the answer FIRST through the writer actor's `AnswerQuestion`, then `questions.claim_if(chat_id, pending)` before resolving the future. That ordering is load-bearing: a concurrent Stop can cancel and pop the pending entry while the answer write awaits its ack, and resolving a cancelled/superseded future would feed the answer to the wrong SDK call. On success the route publishes `answers_applied` and returns `status:"answer_delivered"` plus `answer_turn:"same"`, which `useStreamConnection.js:sendMessage` treats as terminal for the POST without reconnecting the SSE. Durable-question recovery instead returns `status:"started"` plus `answer_turn:"new"`. The dedicated `answer_turn` field owns frontend row/bridge semantics; the status fallback exists only for rolling compatibility with older backends. A stale/missing pending question returns `410` rather than falling through and sending the answer as a new user turn.
**Question settlement invariant:** live stream items, a persisted partial, and the settled transcript are alternate sources for one active assistant row. An in-process answer resumes that same row; the live-to-durable handoff preserves the question, its answer, and all pre/post-answer thinking, tool, and text blocks in event order without hiding, duplicating, or reordering them. Only a recovered answer with `answer_turn:"new"` creates a separate hidden continuation. Unknown future modes fail closed to a separate boundary so an existing question row is never overwritten.

Three frontend gates must stay aligned. `StreamingMessage.jsx` renders live question events with `QuestionCard` and NO disabled prop (the runner is paused while `sending`/`isStreaming` can still be true); `QuestionCard.jsx` does accept a `disabled` prop, but only `MsgContent.jsx` passes it, for non-answerable persisted cards. `ChatView.jsx:doSendSilent` allows submissions carrying `resolvedAnswers` through both `sendingRef` and `isStreamingRef`, uses `sendSilentInFlightRef` as the synchronous double-submit guard, optimistically patches message + stream question answers, and sends a hidden message with `answers` + `question_id`. Persistence identity lives in `chat_writer.py`: `apply_answers_to_last_question()` writes by exact `question_id` when present, and both the live-snapshot and final-merge paths carry existing answers forward by `events.question_block_key()` so later streaming snapshots don't wipe them. Do not key answer carry by block position, do not resolve the pending future before the writer ack, and do not make live cards inherit global send/stream disabled state.

## Chat persistence — single-writer actor

All chat-domain mutations — transcript writes, run-markers, question rows, answers, finalize, error-persist — route through the single-writer actor in `chat_writer.py` as **domain commands** (`PersistTranscript`, `QuestionCommit`, `Finalize`, `PersistError`, `AnswerQuestion`, `Barrier`, `DrainAndStop`). Every command allocates an ack `Future`, but only the strict paths (`QuestionCommit`, `Finalize`, `AnswerQuestion`, `Barrier`, `DrainAndStop`) *await* it (commit-before-ack); `PersistTranscript` and `PersistError` are submitted fire-and-forget — `PersistTranscript` additionally coalesces rapid streaming snapshots, while `PersistError` does not coalesce. One dedicated thread owns the SQLAlchemy session and a FIFO command queue; async callers submit a command and await its `Future`. The blocking `db.commit()` (which SQLite's `busy_timeout` can stall up to 5s) thus never runs on the event loop, and the actor never touches asyncio or `ChatBroadcast` (those stay loop-owned).

Streaming state is physically bounded: `PersistTranscript`/`PersistError` replace `Chat.live_assistant`, never the historical `Chat.messages` JSON blob. Read routes overlay that current assistant on immutable history. `QuestionCommit` merges the card into history before broadcast, `Finalize` performs the terminal merge and clears the live value, and startup reconciliation performs the same merge after a crash. This keeps one-second crash-resilient snapshots without quadratic transcript rewrites as chats grow.

- **Commit-before-ack (strict paths):** the caller's `await` on `QuestionCommit`/`Finalize`/`AnswerQuestion`/`Barrier`/`DrainAndStop` doesn't unblock until the commit succeeds; `PersistTranscript` and `PersistError` are fire-and-forget (submitted without awaiting the ack).
- **Questions commit-before-broadcast:** a question row is durable before its SSE push fires, so a reconnect's catch-up burst always finds it.
- **Concurrency invariant:** ack `Future`s are NEVER resolved while a producer lock is held — collect `(ack, value)` under the lock, resolve after release — so even a synchronous done-callback that re-enters `submit()`/`stop()` can't deadlock. Do not move an ack resolution back inside a `with` block.

**GUARDRAIL — never write `Chat.messages` / `Chat.live_assistant` / `Chat.pending_messages` directly** from a request handler or SDK runner. SQLite WAL serializes commits but NOT the app-level JSON snapshot READ: two readers both see the pre-write snapshot and one silently overwrites the other (the lost-update race the actor closes). The only justified direct writer is `reconcile_interrupted_chats` (`chat.py`, runs at boot before the actor starts); `recovery_chat_runner.py` is actor-independent by design and appends to its own `/data/recovery/chats/<chat_id>.jsonl`, not the `Chat.messages` column.

## Multi-pane workspace (design only)

The shipped feature is a single tab strip that swaps one on-screen view. The
target is a workspace where tabs can move between tiled panes: a build chat on
the left and its app preview on the right, or several agent chats side by side.
Phones degrade to swap-only tabs; tiling is a web/desktop capability.

### Existing pane seams

`frontend/src/components/Shell/tabModel.js` is the openable-item primitive. A
tab is `{ kind: 'chat' | 'app', id: string }`; the module owns construction,
identity (`tabKey`, `sameTab`), deduplication, capacity, persistence, navigation
mapping, and current single-view active state. Construction, identity,
deduplication, capacity, and `tabNavTarget` carry into panes unchanged. Today
`isTabActive(tab, view)` maps the global `{ view, chatId, appId }` focus to a
tab. A pane will instead store `activeTabKey` and compare it with `tabKey(tab)`.

`Shell.jsx` currently keeps one `openTabs` set, one active-view triple, and the
hidden app-iframe LRU. This is the degenerate one-pane form of the target model.

`frontend/src/components/Shell/workspacePlacement.js` is the placement seam.
Producers issue an `open-item` request with `placement: 'beside-source'` and
`activation: 'background'`; they never name a tab strip, pane id, split
direction, or breakpoint. The flat resolver inserts a built app after its
source chat. A pane resolver should interpret the same request as: use the next
pane when one exists, create one when the viewport supports it, and fall back
to an adjacent background tab on narrow screens.

| Input | Confirmation | Current action |
| --- | --- | --- |
| `app_created {appId, chatId}` | Refetched row matches both ids | Apply one background `beside-source` request |
| `app_created` missing/mismatched ids | No matching live row | Ignore the placement request |
| Fresh app-list row with `chat_id` | App absent from the established session baseline | Apply the same request as reconnect fallback |
| `app_updated` | Live row exists | Refresh CTA/code and warm cache; never place again |
| Store install or app without `chat_id` | No source-chat relationship | Drawer arrival only |
| Replayed/duplicate placement | Target app already open | Strict same-reference no-op |

Every automatic built-preview path passes through
`applyWorkspaceRequestsToFlatTabs`. Direct drawer/user tab opens remain
explicit foreground navigation and bypass automatic placement by design.
When the flat strip is at capacity, automatic placement protects the currently
visible tab as well as the new source-chat/app pair; background work must never
make the user's on-screen tab disappear from the strip.

### Target pane model

- **Workspace** = a `layout` tree plus a set of `panes`.
- **Pane** = `{ id, tabs: Tab[], activeTabKey }`, with its own open set and
  focused tab. The current shell is `panes: [pane0]` with
  `pane0.tabs = openTabs`.
- **Layout** = a binary split tree: `{ dir: 'row' | 'col', a, b, ratio }`, with
  pane leaves. A single pane is the trivial leaf.
- **Focus** = the pane receiving keyboard input and serving as the current back
  target.

`paneModel.js`, beside `tabModel.js`, should own pure layout operations: split
a pane, move a tab, close a pane, and resize a split. Rendering walks the tree
and renders each leaf pane's active tab.

### Localized migration path

1. Introduce `paneModel.js` and a workspace reducer. Seed one pane from today's
   `openTabs`; do not change the UI yet.
2. Render the layout tree instead of one `<main>`. The single-pane output must
   remain identical; this step unlocks two panes.
3. Add drag-to-tile: dropping a tab on a pane edge splits it and moves the tab.
   The strip's drag source already has `tabKey`.
4. Add per-pane chat/app rendering. Today only the active ChatView mounts; a
   workspace mounts one ChatView per visible chat pane and must obey the
   constraints below.

### Hard pane constraints

1. **Never remount ChatView to re-measure.** Pane resizing must imperatively
   reset its grow-only `fullViewHRef` in `useScrollMode` while preserving
   `FOLLOW_BOTTOM`. Folding pane size into a React key freezes live follow
   behavior. Each pane owns
   its own height ref; keyboard resizing is pane-local.
2. **Never reparent keyed app iframes, and keep the global cap.** Visible app
   panes count against `APP_CACHE_MAX` (currently four). Preserve id-sorted
   render order across the workspace; reparenting a sandboxed iframe reloads it
   and can hit the ten-second loading timeout.
3. **App ids remain numeric for navigation.** Route every pane open through
   `tabModel.tabNavTarget`; string/number divergence double-mounts the iframe.
4. **Design Back and pane focus together.** Multiple panes need per-pane
   history or explicitly tagged tab/pane history entries dispatched by type.
   A priority-list guess desynchronizes the Navigation API and sentinel model.
5. **Test positive behavior.** Assert that a message stays pinned and a pane
   continues following its stream across resize/toggle. A bound such as
   `spacer <= client` is insufficient because a broken zero spacer passes it.

`paneModel`, the layout reducer, multi-pane rendering, resizing, and drag/drop
remain deferred until panes are the explicit task. The tab and placement seams
above are useful in the shipped one-pane experience today.

## Navigation back-stack + drawer model

On narrow layouts the drawer is modeled as a *virtual route*: opening it pushes one history entry but keeps the URL at `/` (`openDrawer` → `pushNavEntry('drawer')` + `drawerPushedRef = true`). On desktop, navigation is instead a persistent sidebar whose open preference belongs to `Shell`/`useDesktopSidebar`; it never reads or mutates the mobile sentinel. `Shell` derives the rendered navigation from those independent states. When a viewport widens while the mobile drawer is open, it keeps the modal interaction boundary in place until `closeDrawer()` has consumed the sentinel, then exposes the saved desktop preference. Untagged iframe-created entries encountered during that close are traversed serially before the desktop sidebar becomes interactive.

The mobile design satisfies a few hard desiderata — no "two drawers" artifact during Chrome-Android swipe-back, the 250ms slide stays visible, one back-press exits the PWA from home, and closing the drawer (overlay tap / X) must never navigate. Three load-bearing invariants in `useNavigation.js` enforce this: (1) **`navTo` consumes the existing drawer-sentinel rather than pushing** when the drawer is open (it pushes one `'nav'` entry only if the drawer was closed), so an in-app nav reuses the drawer's history slot instead of growing the stack — keeping history pinned to a pre-drawer snapshot and killing the BFCache artifact; (2) **every close path funnels through `history.back()` → `handleBack`**, whose drawer-first guard (`if (drawerOpenRef && drawerPushedRef) { close; return }`) prevents over-popping `navStackRef`; (3) **`drawerPushedRef` is a ref, not state** (mutated synchronously in the same task as the history call) and is the single source of truth for "is a drawer-sentinel above the current entry." Activating the already-current destination is a close/no-op and must not create a duplicate history edge. Every shell-pushed entry is tagged `{__mobiusNav:true, kind}` via `navHistory.js` and written to *both* the classic History store and the Navigation API entry (`updateCurrentEntry`); both back handlers ignore untagged pops so sandboxed-iframe phantom entries can't over-pop — do not drop the tag from any push site or genuine sentinels read as phantoms and back-nav dies. Mini-apps install their own back-targets via the `moebius:nav-push` postMessage protocol (per-app counts in `appSentinelCountsRef`, capped at 20), consumed before navStack pops; `Shell.deleteChat` must scrub `navStackRef` of the deleted chat's entries or back lands on a 404'd chat. Three architectures were tried and rejected (per-nav pushState, `flushSync`-before-pushState, perpetual single-sentinel) — read `tests/navigation.spec.mjs` before changing anything.

## Service worker + offline

Möbius uses one root-scoped service worker, `frontend/src/sw.js`, to keep shell and mini-app navigations same-origin when offline. The shell route is the Workbox app-shell path: `NavigationRoute(createHandlerBoundToURL('/index.html'))` serves the precached shell, with `/apps/`, `/app-assets/`, `/app-embeds/`, `/recover`, `/shell/embed`, `/sites`, and selected published-style paths denied so backend-owned documents don't become the SPA by accident. Mini-app code is split from that shell path: `/api/apps/{id}/frame` and `/api/apps/{id}/module` match `isAppCodeRoute()` and go through `appCodeHandler(OFFLINE_APPS_CACHE, { gated: false })` — frame/module caching is deliberately NOT gated by `offline_capable`. Standalone `/apps/<slug>/` navigations use the same handler with `gated: true`: only a `200` carrying `X-Mobius-Offline: 1` is stored; a headerless `200` purges the standalone entry. The server sets that header for `offline_capable` apps in `routes/apps.py:get_frame`/`get_module` and `routes/standalone.py:standalone_shell`.

`appCodeHandler()` normalizes the cache key by stripping `token`/`_`/`install` but KEEPING `v`; freshness rides `?v=<app.updated_at>` becoming a new key, not a connectivity probe. Once a versioned entry exists, `shouldServeCacheFirst()` serves it immediately while `event.waitUntil()` refreshes in the background. Cold paths and refreshes use `cache: 'reload'` through `boundedFetch()` so browser HTTP-cache revalidation can't hand the SW a bodyless `304` (`NET_TIMEOUT_MS` is a 3000ms hang guard, not a latency knob). `appCodeStoreAction()` is the storage policy: ungated frame/module stores every `200`, gated standalone stores only `X-Mobius-Offline: 1`, all non-`200` ignored; `applyAppCodeStore()` tolerates quota failures and deletes superseded same-route entries with a different `v`.

Packaged static documents use a separate rule. `/app-embeds/` entry documents
retain their own response-sandboxed cache key. A response-sandboxed opaque child
is not controlled by the shell worker, so its relative JS/CSS/media requests use
normal network/HTTP caching and have no packaged-static offline guarantee. Only
an actual SW-controlled subresource request may normalize to the ordinary
`/app-assets/by-id/…` identity; fetch/XHR and document requests retain the embed
namespace, preventing a sandboxed entry response from aliasing onto the ordinary
protected lane. No recursive crawler is implied: a future offline-capable package
needs an explicit manifest/static-assets warm contract. The controlled-page
regression pins the cached entry as packaged content rather than shell HTML.

Install-time precache includes the Vite shell plus the D3/Pixi classic scripts Memory loads by URL. Package imports are already inside each compiled app artifact and must not be duplicated in the shell precache. Runtime `/vendor/` remains `CacheFirst` for explicit public assets. `setCatchHandler()` returns precached `index.html` outside `/apps/` and `offline.html` for standalone/app-asset failures, avoiding native offline chrome. Two anti-patterns: do NOT reintroduce a `mobius-shell-nav` HTML cache (navigations bind to the precached `index.html` so HTML and hashed bundles advance together), and do NOT gate in-shell frame/module reads on `offline_capable` (that flag gates standalone offline opens + write semantics, while frame/module speed + warmup are universal). There is no hand-edited `VERSION` constant: `activate` deletes stale runtime caches via `isStaleRuntimeCache`, and Workbox handles content-versioned precache cleanup separately.

Shell rebuilds apply on idle: `Shell.jsx` defers `shell_rebuilt` while the chat the
owner is actively viewing is streaming, then performs the controlled SW handoff/reload.
Background chat runs are server-owned and reconnect after the reload; they must not
strand a repaired shell indefinitely when several agents are working. The idle boundary alone
is not a transcript-persistence boundary—terminal promotion updates the in-memory
TanStack cache synchronously while its normal IndexedDB mirror is throttled. Before
the intentional reload, `flushPersistedQueryCache()` writes the current allowlisted
cache directly; this normally guarantees the reloaded ChatView hydrates the terminal
assistant row rather than the previous partial while its authoritative GET revalidates.
The wait is bounded by `awaitCacheFlushBeforeReload()`: IndexedDB can be blocked by
another browser lifecycle transaction, and a best-effort cache write must never strand
a waiting service-worker generation. The write may still finish after the deadline.

## Mini-app manifest (mobius.json)

Every mini-app ships a `mobius.json`; the dependency-free source of truth is `backend/app/manifest_contract.py`, used by both `install._validate_manifest` (`POST /api/apps/install`) and `backend/scripts/validate-app.py`. Five required non-empty string fields are `id`, `name`, `version`, `description`, and `entry`; `entry` must be the canonical `index.jsx` used by the editor/watcher lifecycle. The `id` is the manifest identity and the initial slug (source dir `/data/apps/<slug>/`; `allocate_unique_slug` can diverge it on a collision, and cron registration keys off the resolved `app.slug`), so it uses charset `a-z 0-9 - _`, cannot start with `-`/`_`, and cannot be purely numeric (bare integers are reserved for the numeric-id storage tree). Optional fields the parser recognizes include `previous_id`, `icon`, colors/display, `offline_capable`, `embeds_agent`, `offline`, `permissions`, `storage_seeds`, `static_assets`, `source_files`, `skills`, `system_prompt`, and `schedule`. Decorative-only fields such as `author`, `license`, and `homepage` are not validated or stored. Three gotchas: (1) **`runtime` (`imports`/`esm_deps`) is informational**; dependency resolution is governed by the pinned self-contained compiler in `app_compile_contract.py`. (2) **`storage_seeds` value type is a switch**: a string is a repo-relative file the installer fetches; a non-string is stored inline as JSON. (3) **`schedule.job` has dual semantics** — with an exactly five-field `schedule.default` it installs recurring cron; without it the script is an on-demand build hook. `static_assets` caps at 256 files / 16 MB each / 64 MB total and logical destination `x` is materialized at source path `static/x`.

## Testing — determinism principle

Flaky e2e is a SYMPTOM of app non-determinism, not slow tests. Fix at the source:
(1) eliminate app races — the SW first-install reload (only reload on a genuine
update), and make the SSE stream (event) authoritative over the reconcile poll so
optimistic state is never clobbered mid-turn; (2) mock the clock for genuinely
time-dependent behavior; (3) wait on signals/state, never `setTimeout` durations;
(4) expose a "settled" flag from the app rather than guessing a delay. The two app
fixes above took `handleStop` 0→3/3 and removed the steer/app-canvas deterministic
failures — product improvements, not test hacks.

`backend/memeval/` is the offline evaluation harness for the memory system —
synthetic/real-session corpora (`corpus.py`, `fixtures/`) run through
consolidation and recall metrics (`runner.py`, `systems.py`, `metrics.py`),
including a reflection-in-the-middle stage (`reflection_stage.py`). It is dev
tooling, never imported by the running app; `backend/tests/test_memeval_*.py`
cover it deterministically.

## See also

- **Build / test / run commands and the dev loop:** `CONTRIBUTING.md`. (The #1 deploy gotcha — a stale `/data/platform/frontend/dist` masking a fresh image — is covered under *Frontend serving priority* above.)
- **Subsystem deep-dives are inlined above** as their own sections: *Stop-chat contract*, *AskUserQuestion interception*, *Chat persistence — single-writer actor*, *Navigation back-stack + drawer model*, *Service worker + offline*, and *Mini-app manifest (mobius.json)*. (The chat-persistence v2 design + staged-rollout notes remain internal/gitignored — the as-built contract is the section above.)
