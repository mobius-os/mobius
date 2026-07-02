# Möbius architecture

Read this first if you just cloned the repo. It maps the system so you can find the file that owns a behavior in your first hour. Every row was verified against the source on `main`; if you find a row that no longer matches the code, fix the row.

## What Möbius is

Möbius is a self-hosted PWA where one owner chats with an in-product AI agent to build mini-apps and modify the platform itself. The "agent" is a coding-agent (Claude Code or Codex) running as a subprocess inside the container; a chat message spawns a turn, the backend streams the agent's output back over SSE, and the agent can compile JSX into mini-apps, edit the shell UI, manage files, and schedule tasks. The whole platform runs in a single Docker container and installs on Android/iOS as a PWA.

The design has one line behind it: **low floor, high ceiling, no walls.** The agent is the product; everything else is substrate it operates on. Concretely: **code empowers the agent, it does not police it.** Subsystems the agent touches (themes, mini-apps, memory/skills, `/data/shared/`, `/data/apps/`) prefer well-named variables and inline contract comments over server-side rewriting; prevention lives in the agent's instruction layer and learned memory, not in code-level validators. Breaking is allowed because broken states are recoverable (the agent that broke it, or a sibling, fixes it — see `routes/recover.py`). The flip side: infrastructure the agent never sees — provider plumbing, the chat persistence actor, the streaming protocol, the navigation back-stack — gets whatever complexity makes it correct. Maximal expressive surface for the agent, ironclad substrate underneath.

This split is why a section can read either "this is intentionally hackable, don't add a guardrail" or "this is load-bearing, don't touch it without reading the full reference." Both are true; which one applies depends on whether the agent sees the surface.

## Deployment — single container

```
Dockerfile (root)     Single-container image: frontend build + backend + CLI tools
docker-compose.yml    Self-hosted: Caddy (TLS) + app + recoveryd
├── caddy             HTTPS reverse proxy — forwards everything to app:8000
├── app               FastAPI serves the API + the frontend static files
└── recoveryd         frozen recovery floor — same image, own container, serves :8001 (/recover* routing deliberately not wired in the bundled Caddyfile; see the self-heal section)
```

The image bundles everything the agent needs at runtime (the Claude and Codex CLIs, esbuild, Node) so the platform works out of the box. To join an existing Caddy setup instead of the bundled one, use `docker-compose.override.example.yml`.

### Frontend serving priority (the #1 deploy gotcha)

At startup `backend/app/main.py:890` picks one static directory **at module load time**, not per request (though a request for a file missing from the chosen `/data/shell/dist` still falls back per-request to the baked `/app/static`):

```
/data/shell/dist/  ← preferred (the agent's live rebuild; persists across image rebuilds)
/app/static/       ← fallback (baked into the image, current with git HEAD)
```

The `/data` volume persists across `docker compose build && up -d`, so a new image's `/app/static/` is masked by an old `/data/shell/dist/`. After a frontend deploy, refresh both source and dist and verify the bundle hash changed in `/data/shell/dist/assets/index-*.js`. Because the choice is made at module load, an in-container shell rebuild does not take effect until the uvicorn process restarts. Never delete `/app/static/` — it is the only recovery fallback and is root-owned.

### Security updates — who patches what

Möbius is meant to be self-hosted on a user-provisioned host — a managed platform (Railway/Render/Fly/PikaPods) or a raw VPS — so "apply a security update" splits into three tiers by who can even act:

- **Image userspace** — the Python wheels, npm globals, apt packages, and vendored mini-app libs baked into the image. The agent owns these end-to-end: bump the pin (`Dockerfile` / `backend/requirements.txt` / `frontend/package.json`), rebuild, recreate. Never `apt upgrade` / `pip install -U` a *running* container — that mutation is ephemeral and drifts the live container away from the reproducible image. `deploy-prod.sh` is the apply path. One deliberate runtime exception: the `mobius` user (the in-product agent) has scoped NOPASSWD sudo for `apt-get`/`apt`/`dpkg` only (`/etc/sudoers.d/mobius-apt`, baked and visudo-validated in the `Dockerfile`), so it can install a genuinely needed OS package at runtime without full root — such installs stay ephemeral until pinned into the image, and the recovery floor deliberately depends on zero apt-installed packages, so a bad package can never take recovery down.
- **Host OS userspace + the Docker engine** — outside every container; patched on the host (`unattended-upgrades` covers the OS packages; the engine is a separate host upgrade).
- **Host kernel** — *not in the container*; it shares the host's and cannot be patched from inside. On a managed platform the operator patches+reboots the kernel underneath you (the safe default for non-devops owners); on a raw VPS it's the owner's job, via `unattended-upgrades` + livepatch + a scheduled reboot window.

Two invariants follow. (1) **Möbius never patches the kernel from inside the container** — it only *surfaces* "host reboot pending / kernel CVE outstanding" to the owner; the platform/OS applies it. (2) **The in-container agent cannot recreate its own container** (the swap would kill its own process), so the shape is *propose-in* (agent scans → bumps → tests → commits) / *dispose-out* (a host-driven `deploy-prod.sh`, or blue-green, does the rebuild+recreate). Detection is the agent's leverage on every tier: `pip-audit` + `npm audit` + an image scanner (Trivy / `docker scout`) over the built image → triage → bump → test → deploy (tier 1) or surface a reboot window (tiers 2/3).

**lodash is pinned to 4.18.1 via `overrides`.** `@openai/apps-sdk-ui` pulls lodash transitively — only through its `Slider` component, which the shell does not import. The 4.17.x line sat unfixed against several advisories for a long stretch; 4.18.x restored maintenance and patched them, so `frontend/package.json` `overrides` forces the transitive lodash to 4.18.1 (`npm audit` is clean). As defense-in-depth, `frontend/src/lib/__tests__/appsSdkLodash.test.js` also fails if the shell ever imports `Slider`, which keeps lodash tree-shaken out of the shipped bundle regardless of the pin.

## Self-update model — `upstream` / `main`, replay on update

Möbius is the rare app whose own agent edits its live code: the in-product agent customizes its mini-apps (`/data/apps/<slug>`), its backend overlay (`/data/platform`), and its shell (`/data/shell`) while the platform runs. A deploy then ships a *new pristine version* of that same code. One small model keeps every such surface up to date without clobbering the owner's customizations and without a deploy ever silently dropping them.

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
- **Conflict** (the release and the local edits touched the same lines) → an **agent chat** resolves it. Apps materialize standard conflict markers up front (`start_conflict_merge`, a `git merge --no-commit --no-ff upstream`) for the agent to edit; platform/shell leave the live tree untouched and the resolver chat runs the merge itself. Either way the resolution is finalized as the *same* single-parent replay — `--no-ff` points `MERGE_HEAD` at the upstream tip and the commit takes only that one parent, so even a resolved conflict stays linear (`A → B → X`), never the 2-parent commit a plain `git merge` would leave. Auto-spawn of the chat differs by surface: platform and shell spawn it automatically, but for apps the auto-spawn was retired (a0a828b) — the update returns `mode=conflict` + `conflict_paths` and the owner opts in via the store's click-gated "Resolve in chat" button. The owner never hand-merges; back out with `git merge --abort`.

**"Update available" is an ancestry question, not a version-string compare:** an update is available iff `upstream`'s tip is **not yet an ancestor of `main`** (a new release hasn't been rebased in). This is the content question — "does my working tree already contain this release" — that a `image_sha != recorded_sha` proxy can't answer on a customized instance, and it's what eliminates phantom "update available" rows after a deploy that changed nothing the owner hadn't already.

### The recovery/auth files are NOT in the model — they're gitignored

The one thing that makes a self-editing platform tricky is the recovery/core island (`protected-files.txt`: the `recover_*` modules, `main.py`/`routes/__init__.py`/`auth.py`/`database.py`/`config.py`/`models.py`, `entrypoint.sh`/`recovery_restore.sh`, and the auth-handling shell components `LoginForm`/`SetupWizard`/`ProviderAuth`). These are root-owned `chmod 444`; the `mobius` user that runs the updater genuinely cannot write them.

The simple answer is that **they are not part of the git model at all** — each surface's `.gitignore` excludes them. They live on disk, managed wholly by the image (the root entrypoint re-enforces root-owned 444/555 on them every boot; their contents refresh from the baked floor only on first boot, a crash-loop restore, or a `recovery_restore.sh` run — the deploy/recovery path, not every reboot). Because they're untracked, neither `record upstream` nor the merge/replay ever touches them, so there is no special "protected-file" machinery in the update engine — the replay only ever moves agent-editable files, and the recovery island updates the one way it should: via an image deploy. (Recovery therefore stays agent-proof, and becomes current with the image once the deploy's restore/reconcile step runs.)

### Where each surface stands

| Surface | Repo | On the model | Engine |
|---------|------|------------------|--------|
| **Mini-apps** (`/data/apps/<slug>`) | `.git` per app (installed apps; agent-built bespoke apps have no upstream to track) | yes — whole source tree on `upstream`, single-parent replay, so **multi-file apps update cleanly** | `backend/app/app_git.py` + `install.py` |
| **Platform backend** (`/data/platform`) | `.git`, recovery files gitignored | yes — thin caller of `app_git`; ancestry availability (sha compare only as the one-time pre-seed fallback); an idempotent migration untracks recovery files | `backend/app/platform_update.py` |
| **Shell** (`/data/shell`) | `.git`, auth components gitignored | yes — thin caller of `app_git`, seeded at boot so existing agent edits become the local delta | `backend/app/shell_update.py` |

All three converge on **one** small tree-aware engine (`app_git.py`): `record_upstream` commits the *whole source tree* on `upstream`, `merge_upstream` verdicts a clean-vs-conflict via `git merge-tree`, and a clean apply replays the merged tree as a **single-parent** commit on top of `upstream` (linear `A→B→X`). The platform and shell are thin callers of that primitive — they pass their own source tree and let their `.gitignore` keep recovery/auth files out of the model. Availability for the platform and shell is the ancestry check (`upstream` not an ancestor of the working branch); the one surviving sha-string compare is the platform's one-time pre-seed fallback for instances that predate the `upstream` branch (`platform_status`'s `seed_required` path). Mini-app update discovery is different: the store compares the catalog manifest version against the installed `App.version` (the new release lives in the remote catalog, so a local ancestry check can't see it). There is no per-surface protected-file scaffolding.

## Backend (`backend/app/`)

FastAPI app. `main.py` is the factory (CORS, rate limiting, routers, static serving). `routes/__init__.py` is a crash-tolerant import scaffold: every router is loaded through `_load(name)`, and an import failure returns a 503 stub instead of killing uvicorn, so `/recover/chat` stays reachable. To add a route, write the module under `routes/`, expose a `router`, and register it in `routes/__init__.py` (both the `_load(...)` line and `__all__`), then mount it in `main.py`. (One documented exception: `routes/chats.py` exposes a *second* router, `app_chat_router` (`/api/app-chats`), which `main.py` imports and mounts directly because `_load` returns only each module's primary `router`.)

### Core app + chat runtime

| File | Role |
|------|------|
| `main.py` | App factory: CORS, rate limiting, security headers (`_SecurityHeadersMiddleware` — authoritative on every response, strips-and-replaces same-named route headers; deliberately no CSP, see SECURITY.md), router mounting, static file serving; resolves `_static_dir` at load (`main.py:890`); serves `GET /api/version` (image + served-platform identity) |
| `config.py` | `Settings` via pydantic-settings; reads `.env` |
| `database.py` | SQLAlchemy engine, `SessionLocal`, `Base`, `get_db`, and `run_migrations()` (idempotent boot-time additive `ALTER TABLE`s) |
| `models.py` | ORM tables: `Owner`, `Chat`, `ChatRun`, `App`, `PushSubscription`, `Notification` |
| `schemas.py` | Pydantic request/response models |
| `auth.py` | bcrypt hashing, JWT creation/decoding, Fernet encryption |
| `deps.py` | FastAPI auth dependencies: `get_current_owner` (owner-only), `get_current_owner_or_app` (owner + app token), `get_principal`, `require_app_permission`, and `reject_cross_site` (CSRF) |
| `compiler.py` | `compile_jsx()` — calls the esbuild CLI to compile a JSX string into an ES module |
| `providers.py` | `BaseProvider` adapters (`ClaudeProvider`, `CodexProvider`) + the `PROVIDERS` registry; identity/auth/env shaping for the SDK runners, and `get_skill_path()`. (`CodexProvider.build`/`parse_line` — the app-server subprocess path — are retained but not on the live chat path; see `codex_appserver.py`.) |
| `claude_sdk_runner.py` | Claude SDK turn runner; passes `cli_path="/usr/local/bin/claude"` so the SDK drives the same pinned binary recovery + cron use |
| `codex_sdk_runner.py` | Codex SDK turn runner (Thread/TurnHandle + steer) |
| `codex_appserver.py` | Pure-function translator for the Codex `app-server` JSON-RPC protocol. The subprocess streaming path it served is legacy (live Codex chat runs through `codex_sdk_runner.py`), but this module stays live — the SDK runner reuses one helper from it, `_extract_bash_command` (it implements its own event/tool classification locally) |
| `chat.py` | `run_chat()` background task: spawns the turn, publishes events, routes persistence through the actor |
| `chat_writer.py` | Single-writer chat-persistence actor — one thread owns the DB session + a FIFO command queue; ALL `Chat.messages` / `Chat.pending_messages` mutations route through it (do not write those columns directly) |
| `chat_queue.py` | Per-chat queue lock + turn-end `drain_and_release` / `promote_pending_messages_locked` + the `TerminalDisposition` state machine; the awaited bridge between `chat.py` and the writer actor |
| `broadcast.py` | `ChatBroadcast` per-chat in-memory event bus; decouples the turn runner from SSE clients |
| `events.py` | Pure data transforms accumulating streaming events into the persisted message structure |
| `compaction.py` | Cross-provider chat compaction (portable plain-text summary; native SDK compaction is within-provider only) |
| `runner_registry.py` | Runner lifecycle registry shared across chat backends |
| `pending_questions.py` | Shared `PendingQuestion` dataclass for AskUserQuestion interception (split out to break the `questions`↔runner import cycle; the registry itself lives in `questions.py`) |
| `tool_summaries.py` | Tool-input summary strings (shared by SDK + subprocess paths) |
| `tool_sources.py` | `normalize_tool_sources()` — normalizes provider web-search results into `{title, url, snippet}` source chips for the WebSearch `ToolBlock`; drops non-`http(s)` URLs so a `javascript:`/`data:` source never reaches an `<a href>` |
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
| `runtime_libs.py` | Canonical list of mini-app runtime libraries externalized by esbuild |
| `runtime_types.py` | Shared runtime type definitions |
| `net_utils.py` | SSRF-safe URL validation shared by the install fetcher and the proxy |
| `resource_access.py` | Resource-access helpers, incl. `live_app` / `live_app_or_404` (tombstone-aware app resolution) |
| `path_utils.py` | Path-safety helpers |

### Memory, skills, activity, scheduling

| File | Role |
|------|------|
| `memory.py` | `build_memory_block()` — assembles the agent's injected memory block from the knowledge graph (graph mode, ~25KB budget) |
| `memory_graph.py` | Builds + lints the knowledge-graph index (`graph.json`) for the Memory viewer |
| `memory_trace.py` | Persists per-chat read traces of the memory graph for the nightly reflection pass |
| `reflection_checkpoint.py` | Reflection's last-run marker (what to review tonight) |
| `activity.py` | Append-only JSONL platform-activity log (app_open, app_install, storage_write, …) |
| `self_reminders.py` | Agent self-scheduling: append-only store of relational check-ins |
| `theme.py` | Theme CSS management and HTML injection |
| `push.py` | VAPID key management and Web Push delivery |

### Recovery (the frozen island)

The `recover_*` modules bootstrap an agent into a broken instance and are deliberately isolated from the SDK/chat stack so a broken SDK install cannot take recovery down. They are chmod 444 root-owned via `protected-files.txt` (together with the boot-chain files `main.py`, `routes/__init__.py`, `auth.py`, `database.py`, `config.py`, `models.py`); the agent cannot edit them.

| File | Role |
|------|------|
| `recover_chat.py` | Recovery chat: vanilla HTML page + send/stream/reset endpoints |
| `recover_chat_runner.py` | Minimal CLI runner for the recovery chat; shares no code with `chat`/`providers`/SDK (runs the standalone `claude` binary as its own subprocess) |
| `recover_auth.py` | Recovery-page auth |
| `recover_oauth.py` | OAuth handling for the recovery flow |

The separate `recoveryd` container's code is a distinct package, `backend/recovery/` (baked root-owned to `/app/recovery/`, outside `backend/app/`): `recoveryd.py` (stdlib `http.server`, zero `app.*` imports; also owns the Sec-Fetch-Site/Origin cross-site reject), `recovery_auth.py` (HMAC-cookie auth — a deliberately frozen *verbatim* copy of `recover_auth.py` above, so edits to the platform copy do NOT propagate), `recovery_db.py` (raw-`sqlite3` owner-row read, no ORM, with a DB-independent `/data/.recovery-owner.json` fallback for a wiped DB — see the self-heal section), `recovery_pages.py` (dependency-free HTML). See the self-heal section below for the container itself.

### Misc shared helpers

Agent-editable general-purpose modules — several sit on live chat paths, so despite living near `recover_*` they are NOT part of the frozen island.

| File | Role |
|------|------|
| `bootstrap.py` | First-boot bootstrap (`ensure_store_installed`) that auto-installs the curated app-store mini-app; called idempotently from the FastAPI lifespan |
| `chat_log_redaction.py` | Server-side structural redaction for the gated chat-log read API |
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
| `storage.py` | Per-app and shared file storage |
| `fs.py` | Owner-facing filesystem + git oversight API |
| `uploads.py` | Per-chat file upload management |
| `generate.py` | Gemini image-generation endpoint (image-only; not a chat provider) |
| `proxy.py` | Server-side CORS-bypass proxy for mini-apps |
| `standalone.py` | Top-level routes that make a mini-app installable as its own PWA (own importmap) |
| `published.py` | Serves published site snapshots at `/sites/<token>/` — token-validated, traversal-confined static files from `/data/published/<token>/` (created by `POST /api/apps/{id}/publish` in `apps.py`; token stable per project) |
| `platform.py` | Owner-gated platform self-update: `GET /api/platform/status`, `POST /apply`, `POST /restart` (drives Settings → Updates; thin caller of `platform_update.py`) |
| `shell.py` | Owner-gated shell self-update: `GET /api/shell/status` + `POST /apply` (thin caller of `shell_update.py`) |
| `notify.py` | System-event notifications to active broadcasts |
| `notifications.py` | Push notification sending + history |
| `push.py` | Web Push subscription management |
| `theme.py` | `GET /api/theme` — effective theme CSS + bg with default fallback |
| `settings.py` | Owner-level configuration |
| `self_reminders.py` | Agent self-scheduling endpoints |
| `admin.py` | Admin / introspection endpoints (service-token gated) |
| `debug.py` | Observability: active SDK clients/sessions, broadcasts, chat logs |
| `client_error.py` | `POST /api/client-error` — record an uncaught client/app JS error |
| `recover.py` | Recovery page at `/recover` (reset/backup/rebuild) — frozen island |
| `recover_html.py` | HTML templates for the recovery page (no `router`; used by `recover.py`) |

Note: there is no `routes/ai.py` and no `POST /api/ai`. An older mini-app AI proxy lived there and was removed; mini-apps reach the agent via `window.mobius.chat`, `POST /api/apps/{id}/run-job`, or cron — not a synchronous AI endpoint.

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
| `MenuButton/` | Hamburger icon |
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
| `frontend/public/mobius-runtime.js` | The `window.mobius` runtime injected into mini-apps; same code for the in-shell iframe (`app-frame.html`) and the standalone PWA (`routes/standalone.py`). Offline outbox + read-through cache live here |
| `frontend/public/app-frame.html` | The mini-app frame: importmap (React/recharts/date-fns/three from `/vendor/...`), error UI, postMessage init |
| `frontend/src/sw.js` | Service worker: precache + cache strategy, incl. the offline-capable-app handler |
| `frontend/src/sw-cache-policy.js` | Authoritative cache-route policy (see *Service worker + offline* below) |
| `frontend/src/lib/` | Cross-cutting helpers: `appToken.js`, `chatEmbed.js`, `themeService.js`, `onlineStatus.js`, `navHistory.js`, `errorLog.js`, etc. |

**Vendored mini-app libs are version-pinned in lockstep across several places.** Each lib (React, three, pdf.js, CodeMirror, KaTeX, recharts, date-fns, d3-geo, marked, DOMPurify) is served same-origin under `/vendor/<name>@<version>/` so mini-apps resolve it offline. Bumping a version means changing it in *all* of: the `Dockerfile` vendor step (installs + builds the lib), the `app-frame.html` importmap (resolves the bare specifier), `sw.js`'s precache list (offline-guarantees the libs it precaches — but three/pdf.js/KaTeX are NOT precached; they ride the runtime `/vendor` `CacheFirst` route instead, so they only warm online), and `runtime_libs.py`'s `RUNTIME_LIBS` (marks it external so esbuild doesn't try to bundle the bare specifier). `routes/standalone.py` derives its importmap from `app-frame.html` via `runtime_libs.importmap_block()`, and `backend/tests/test_runtime_libs.py` locks the importmap ↔ `RUNTIME_LIBS` pair so a desync fails CI. These are security-relevant: the vendored DOMPurify/marked are on the mini-app sanitize/render path, so they must be kept current with the shell's own npm copies.

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
| Add an app-runtime capability | `frontend/public/mobius-runtime.js` + both importmaps (`frontend/public/app-frame.html` and `backend/app/routes/standalone.py`) |
| Add a runtime vendor library | `backend/app/runtime_libs.py` + both importmaps + the Dockerfile vendor copy |
| Change offline / SW behavior | `frontend/src/sw.js` + `frontend/src/sw-cache-policy.js` (read *Service worker + offline* below first) |
| Change the in-product agent's instructions | `skill/core.md` (constitution) or `backend/scripts/seed-skills/*.md` (per-task skills) — see below |
| Change a built-in core app (Memory / Reflection) | The catalog repo (`mobius-os/app-<slug>`) is the source of truth — `core-apps/` is a committed snapshot, never hand-edited. Bump the pinned commit in `core-apps/SOURCES`, run `scripts/sync-core-apps.sh`, commit the diff; CI (`scripts/check-core-apps-sync.sh`) fails on drift. The snapshot is baked to `/app/core-apps` (Dockerfile) and installed at boot by `backend/scripts/install-core-apps.sh` |
| Theme CSS / tokens | `backend/app/theme.py` + `routes/theme.py` + `frontend/src/hooks/useTheme.js` |

## In-product agent context — three layers

The in-product agent is a first-class reader of this code, and its behavior is governed by three layers, not one. (1) **Constitution** — `skill/core.md`, the owner-curated system prompt, baked to `/app/skill/core.md`; `chat.py` reads it into `system_prompt` (the Claude SDK receives it on every turn; the Codex SDK uses it as base instructions only when starting a new thread, i.e. `session_id is None`). `providers.get_skill_path()` resolves `core.md` only (there is no `agent-skill.md` fallback). (2) **Skills** — `/data/shared/skills/*.md` (building-apps, theming, cron, notifications, recovery, memory, reflection, …), seeded create-if-absent by `backend/scripts/init_skills.py` from `backend/scripts/seed-skills/`; the agent `Read`s the relevant one on demand and may edit them. (3) **Memory** — the knowledge graph under `/data/shared/memory/` (`index.md` + per-chat notes `chats/<id>/index.md` — the primary day-time memory carrier — + `mocs/` + `notes/` + `graph.json` + `read-trace/` + `.ready`), injected progressive-disclosure by `backend/app/memory.py` into the first user message as an `<agent_experience>` block (not the system prompt, so static content caches), indexed by `memory_graph.py`, and viewed through the Memory mini-app. The injected block is `index.md` plus the ~10 most-recently-modified chat notes; the deeper graph (`mocs/`, `notes/`) is read on demand. Two platform scripts maintain/retrieve it: `backend/scripts/chat_note.py` (tool-free turn-end summarizer `chat.py` runs when a settled chat's note is missing or stale; it also syncs the chat title from the note's gist) and `backend/scripts/memory_search.py` (the memory-search recall subagent, auto-run on substantive first messages when `auto_memory_search` is enabled). To change agent behavior, edit `skill/core.md` and the seeds — not code-level validators (see the design philosophy above).

## Data layout (`/data/` volume)

```
/data/
├── db/ultimate.db          SQLite database
├── compiled/app-*.js       esbuild output (one per app, keyed by numeric id)
├── apps/<slug>/index.jsx   agent-editable JSX source (keyed by app slug)
├── apps/<slug>/...          per-app runtime data + per-app git repo
├── shared/                 cross-app shared files (theme.css, skills/, memory/)
├── shell/                  agent's editable shell copy (src/ + dist/)
├── cli-auth/claude/        CLI credentials
├── cron-logs/              output from scheduled task scripts
├── published/<token>/      published site snapshots (shareable /sites/<token>/ URLs)
└── service-token.txt       long-lived JWT for cron scripts (chmod 600)
```

`/data` is itself a git repo owned by the `mobius` user, tracking `shared/memory/` and `shared/skills/` with a nightly safety-net commit, so a bad memory consolidation or skill overwrite is recoverable. Inspect it as that user (`docker exec -u mobius ... git -C /data ...`) — as root it dies with "dubious ownership," which reads misleadingly as an empty/non-repo tree.

## Boot, self-heal, and how each layer updates

**Layers + where they live:** core platform = backend (`/data/platform`, a git
repo) + shell (`/data/shell` src+dist); mini-apps (`/data/apps/<slug>`, each a
git repo); recovery (the frozen island, below). Runtime trees are gitignored
(db, compiled, cli-auth).

**Updates** flow through git. `backend/app/platform_update.py` is a thin caller
of the shared `app_git.py` engine (see "Where each surface stands" above): it
picks the baked image floor to record as the `upstream` branch (today the
upstream IS the baked floor; a signed-GitHub origin is the planned direction,
tracked as card 147 on the maintainers' local backlog — `.pm/` is gitignored, so
these card refs here and below are not present in a fresh clone), the engine
computes the merge tree, and on conflict the platform path spawns an agent
conflict-chat to resolve. Shell rebuilds via `rebuild_shell.sh`; the served
bundle is `/data/shell/dist` if a complete build exists else baked `/app/static`
(the #1 deploy gotcha — the volume masks a new baked dist; always byte-check the
served hash). Mini-apps update through each app's git repo + the store; freshness
rides ETags (`/module` = `updated_at` µs; `/frame` = compound
`updated_at`+content-hash). The backend has the same served-vs-baked gotcha as
the shell: a new image's `sha` advances on every deploy even while
`/data/platform` keeps serving the previous deploy's Python. `GET /api/version`
(`backend/app/main.py`) therefore reports the image identity (`sha`, `shell_sha`)
plus the SERVED-platform identity — `serving_source` (from the `/tmp/serving-source`
stamp `entrypoint.sh` writes at boot), `platform_sha`, `platform_dirty`,
`baked_sha` — and `scripts/deploy-prod.sh`'s verify step asserts these match its
sync decision (it also hard-blocks deploying a checkout strictly BEHIND
`origin/main`).

**Built-in apps (Memory, Reflection)** come from the tracked top-level
`core-apps/<slug>/` trees — committed SNAPSHOTS of their catalog repos
(`mobius-os/app-*`), pinned by commit in `core-apps/SOURCES`, baked to
`/app/core-apps` (Dockerfile), and registered/re-synced at boot by
`backend/scripts/install-core-apps.sh` (backgrounded post-launch by the
entrypoint; registration goes through the API with the service token). Never edit
`core-apps/` directly: update the catalog repo, bump `SOURCES`, and run
`scripts/sync-core-apps.sh`; CI (`scripts/check-core-apps-sync.sh`) fails the
build on drift.

**Self-heal — LANDING (owner-signed-off 2026-06-30; the `recoveryd` container is
BUILT — `backend/recovery/recoveryd.py`, its own service in `docker-compose.yml`
with `restart: unless-stopped` on the same `/data` — though `/recover*` routing
is NOT yet wired in the bundled `Caddyfile` (which still only proxies
`app:8000`; recovery routing is left to the external `deploy-caddy` and marked
"NOT wired here" in `docker-compose.yml`) — per the adversarially-reviewed plan in
cards 148 + its recoveryd-hardened addendum. Some hardening is still in flight
under an active session, so treat the finer status of the bullets below —
especially the pre-flight gate (card 154) and the listed removals — as
"intended end-state, confirm against the recovery rework" rather than settled).**
The *end-state* model is git + a minimal pre-flight gate + a separate always-up
recovery container, with **no baked duplicate, no `/app/app` symlink-swap, no
auto-heal probe, no magic** — but note several of those removals are not done at
HEAD yet (see the last bullet); today boot still symlink-swaps `/app/app` →
`/data/platform/app` with a baked fallback:
- The platform is served directly as a git repo.
- **Pre-flight gate (card 154):** a change only takes effect on the restart that
  applies it, so the running server keeps serving the working code while the agent
  edits. Before applying, a minimal check — does the new tree import (`python -c
  "import app.main"`) and answer `/api/health`? Green → apply; red → don't apply,
  hand the traceback back to the agent's turn. A break never reaches the live server.
- **Recovery is a SEPARATE CONTAINER, not a process inside the platform.**
  `recoveryd` runs from the same image with its own command + `restart:
  unless-stopped`, mounts the same `/data`, and is routed by the external
  `deploy-caddy` at `/recover*` *independently of platform health* (with an
  auto-surfaced broken-state page when `:8000` is down). It must be a separate
  container — the adversarial review confirmed prod pid1 `exec`s uvicorn, so a
  backgrounded supervisor inside the platform container dies with it. **Two tiers:**
  *Tier 1* is a deterministic "Restore platform" button (git-reset `/data/platform`
  via the existing `.recover-pending` + restart) needing **zero CLI / OAuth /
  network / agent** — this is THE floor and the ONLY tier built today; *Tier 2*
  (the lifted `recover_chat_runner` spawning a rescue agent, best-effort when
  creds exist) is an explicitly DEFERRED follow-on, NOT in the built recoveryd
  (per `backend/recovery/recoveryd.py`'s docstring) — today the rescue chat still
  lives only inside the platform uvicorn at `/recover`, so it dies with the
  platform. recoveryd restarts the platform via a sentinel file + a baked
  `kill -TERM 1` poller (no Docker socket — O1). Its auth bcrypt-checks the
  owner password against the SQLite owner row read via raw sqlite3
  (`backend/recovery/recovery_db.py`), with an HMAC session cookie keyed off
  `/data/.recovery-secret`. It also survives a **wiped or corrupt DB** (O2): the
  platform mirrors the owner's username + bcrypt hash into a DB-independent seed
  at `/data/.recovery-owner.json` (`backend/app/recovery_seed.py`, written at
  `setup()` and idempotently re-synced at boot), and `recovery_db` falls back to
  it **only when the DB is unreadable** — never when it is readable-but-owner-less,
  so a fresh install and a completed factory reset (both `DELETE FROM owner`,
  leaving a readable empty table) still read as "no owner" and a transient
  busy/locked DB fails closed. The seed is only ever written after an owner row
  commits (the first-boot-takeover guard), deleted on factory reset, and
  gitignored + FS-deny-listed so it never leaks through `pm-commit` or
  `/api/fs/read`.
- Of the planned removals, only the `.platform-serve-baked` probe/flag is gone
  today. The crash-loop `cp`-restore (`backend/scripts/entrypoint.sh`, boot-counter
  ≥ 3) and the destructive `platform-baked` `recovery_restore.sh` mode are STILL
  PRESENT at HEAD — slated for removal with the recovery rework — and boot still
  symlink-swaps `/app/app` → `/data/platform/app` (falling back to the baked floor
  when the platform tree fails its sanity check; the entrypoint stamps the decision
  to `/tmp/serving-source`). End-state intent: real image/disk corruption is an
  operator redeploy, not something to auto-heal around.

## Chat scroll + steer contract

How a chat scrolls/steers, owner-authoritative. The Playwright lock-in specs
(`tests/send-rule`, `spacer`, `second-send-pin`, `steer-queued`, `stream-reconnect`,
`backend/tests/test_chats_stream_steer`) encode this:

- **R1** First message always pins to the viewport top, reserving bottom room for
  the streaming reply.
- **R2** Subsequent messages always reserve enough space, but only MOVE the scroll
  if the user is at the bottom (the autoscroll zone). Scrolled-up/reading → the
  viewport must NOT jump (the load-bearing guarantee is "don't move a reading
  user", not "spacer is zero"). At-bottom is read from `scrollTop` before the
  append (`shouldPinSend`/`isNearScrollBottom` in `useScrollMode.js`), not a
  sentinel.
- **R3** Steered messages obey R1/R2 (pin only if they'd have pinned as a fresh send).
- **R4** Leave-and-return restores the same scroll position, even mid-stream
  (hide-then-reveal + the versioned snapshot cache).
- **Steer = separate rows, one turn.** Steered queued messages render as separate
  transcript rows in send order (`insertMessageBatchByTs`), never one stranded
  after the reply. The agent receives them joined by `\n\n` (clean paragraphs,
  not a `\n` blob) — matched byte-for-byte FE (`handleSteer`) + BE
  (`_selected_force_steer_pending`). The fast-forward button shows only when every
  queued row is server-confirmed (`canFastForwardQueue`); confirm on a numeric ts.
  A steer landing before any renderable assistant output seals nothing — the
  empty/whitespace pre-steer segment is dropped symmetrically on the live path
  (`streamPromotion.streamItemsHaveRenderableContent`) and the persisted path
  (`events.blocks_have_renderable_content`, gating the seal in `chat.py`), so no
  stray empty assistant bubble precedes the steered row; a single real token still
  seals, correctly placed before it (card 166). Keep the two predicates aligned.
- **Regression guards (owner-observed prod bugs):** an at-bottom send must not land
  mid-viewport; a steered row must not render after the agent's reply.

## Stop-chat contract

Stop is a two-layer contract: the backend interrupts and clears, while `frontend/src/components/ChatView/ChatView.jsx:handleStop` owns the user-visible collapse-and-resend behavior. On entry, `handleStop` synchronously guards against double clicks, snapshots `pendingQueue.pendingMessagesRef.current`, joins queued text with a single `\n`, dedupes attachments by `name`, bumps `fetchGenRef`, and clears the pending queue before awaiting `/api/chat/stop`. The endpoint `backend/app/routes/chat.py:chat_stop` returns `{"stopped": bool, "cleared_pending_ts": [...]}` from `chat.py:stop_chat`, and the frontend runs that through `resolveStopResend()` for both clean-stop and timeout branches: `null`/missing `cleared_pending_ts` falls back to the whole snapshot, `[]` resends nothing, exact matches resend only those queued rows, and unmatched optimistic timestamps fall back to the whole snapshot rather than dropping work.

`backend/app/chat.py:stop_chat_for` is an interrupt primitive, not a queue-drain primitive. It snapshots the generation, calls `bump_run_generation(chat_id)` before killing handles, registers `_clear_after_terminal_generation` when handles exist, clears pending under `chat_queue.get_lock()`, cancels any live `app.questions` pending question, then calls each runner handle's `stop(timeout=2.0)`. If every handle stops it unregisters them and finalizes the broadcast (discarding `_starting`); the stuck run-marker is cleared via the actor only on the no-handles path — active handles hand that clear to `run_chat`'s finally block. If any handle times out it leaves the registry entry and broadcast intact for runner-side teardown and returns `stopped=False` — in that branch the frontend must NOT disconnect or start a second run. Instead, if `resolveStopResend()` returns text, `handleStop` calls `doSend(..., { pin:false })` while `isStreamingRef` is still true, so the message follows the queue path and is re-persisted as pending.

The generation bump is the key invariant. A dying `_run_chat_impl` rechecks ownership in its terminal path; after Stop it must resolve to `STALE_NO_ACTION` (or the Stop-handoff cleanup), never promote pending or schedule a backend continuation behind the frontend's resend. Do not refetch pending from the server after Stop to rebuild the resend — Stop already cleared the durable queue, so the local snapshot is the only source that preserves text + attachments; and do not resend the full snapshot unconditionally on `stopped:false` — the natural turn-end drain may already have consumed some rows, and `cleared_pending_ts` is the only guard against duplicate follow-up work.

## AskUserQuestion interception

AskUserQuestion is a shared pending-future lifecycle plus a shared `question` stream event; Claude and Codex differ only at the SDK boundary. `backend/app/pending_questions.py:PendingQuestion` carries `question_id`, `questions`, `future`, and optional `run_token`; `backend/app/questions.py` owns the module-level `_pending` registry (`get`, `claim_if`, `cancel`). Claude registers the pending question in `claude_sdk_runner.py:can_use_tool` for the `AskUserQuestion` tool, persists the card via `_ChatEventSink.publish_question()`, awaits the future, and returns `PermissionResultAllow(updated_input={questions, answers})`. Codex installs `_install_request_user_input_handler()` on `codex._client._sync._approval_handler`, enables `features.default_mode_request_user_input=true`, handles `item/tool/requestUserInput`, marshals from the SDK worker thread into the loop with `run_coroutine_threadsafe` (a ~420s bridge timeout), and translates Möbius's text-keyed answers into Codex's id-keyed `{answers:{qid:{answers:[...]}}}` shape.

The answer POST is intercepted before normal send handling in `backend/app/routes/chats_stream.py:send_message` whenever `body.answers` is truthy. The route waits ~500ms for a just-broadcast pending entry, checks `question_id` identity when supplied, persists the answer FIRST through the writer actor's `AnswerQuestion`, then `questions.claim_if(chat_id, pending)` before resolving the future. That ordering is load-bearing: a concurrent Stop can cancel and pop the pending entry while the answer write awaits its ack, and resolving a cancelled/superseded future would feed the answer to the wrong SDK call. On success the route publishes `answers_applied` and returns `status:"answer_delivered"`, which `useStreamConnection.js:sendMessage` treats as terminal for the POST without reconnecting the SSE. A stale/missing pending question returns `410` rather than falling through and sending the answer as a new user turn.

Three frontend gates must stay aligned. `StreamingMessage.jsx` renders live question events with `QuestionCard` and NO disabled prop (the runner is paused while `sending`/`isStreaming` can still be true); `QuestionCard.jsx` does accept a `disabled` prop, but only `MsgContent.jsx` passes it, for non-answerable persisted cards. `ChatView.jsx:doSendSilent` allows submissions carrying `resolvedAnswers` through both `sendingRef` and `isStreamingRef`, uses `sendSilentInFlightRef` as the synchronous double-submit guard, optimistically patches message + stream question answers, and sends a hidden message with `answers` + `question_id`. Persistence identity lives in `chat_writer.py`: `apply_answers_to_last_question()` writes by exact `question_id` when present, and `update_last_assistant_message()` carries existing answers forward by `events.question_block_key()` so later streaming snapshots don't wipe them. Do not key answer carry by block position, do not resolve the pending future before the writer ack, and do not make live cards inherit global send/stream disabled state.

## Chat persistence — single-writer actor

All chat-domain mutations — transcript writes, run-markers, question rows, answers, finalize, error-persist — route through the single-writer actor in `chat_writer.py` as **domain commands** (`PersistTranscript`, `Finalize`, `PersistError`, `AnswerQuestion`, `Barrier`, `DrainAndStop`). Every command allocates an ack `Future`, but only the strict paths (`Finalize`, `AnswerQuestion`, `Barrier`, `DrainAndStop`) *await* it (commit-before-ack); `PersistTranscript` and `PersistError` are submitted fire-and-forget — `PersistTranscript` additionally coalesces rapid streaming snapshots, while `PersistError` does not coalesce (an unwritten error state is repaired by a later `Finalize`/snapshot). One dedicated thread owns the SQLAlchemy session and a FIFO command queue; async callers submit a command and await its `Future`. The blocking `db.commit()` (which SQLite's `busy_timeout` can stall up to 5s) thus never runs on the event loop, and the actor never touches asyncio or `ChatBroadcast` (those stay loop-owned). Commands are DOMAIN-level, not row-level, so a later milestone can swap their dispatch for normalized-row writes without rewriting the actor.

- **Commit-before-ack (strict paths):** the caller's `await` on `Finalize`/`AnswerQuestion`/`Barrier`/`DrainAndStop` doesn't unblock until the commit succeeds; `PersistTranscript` and `PersistError` are fire-and-forget (submitted without awaiting the ack).
- **Questions commit-before-broadcast:** a question row is durable before its SSE push fires, so a reconnect's catch-up burst always finds it.
- **Concurrency invariant:** ack `Future`s are NEVER resolved while a producer lock is held — collect `(ack, value)` under the lock, resolve after release — so even a synchronous done-callback that re-enters `submit()`/`stop()` can't deadlock. Do not move an ack resolution back inside a `with` block.

**GUARDRAIL — never write `Chat.messages` / `Chat.pending_messages` directly** from a request handler or SDK runner. SQLite WAL serializes commits but NOT the app-level JSON snapshot READ: two readers both see the pre-write snapshot and one silently overwrites the other (the lost-update race the actor closes). The only justified direct writer is `reconcile_interrupted_chats` (`chat.py`, runs at boot before the actor starts); `recover_chat_runner.py` is actor-independent by design and appends to its own `/data/recovery_chat.jsonl`, not the `Chat.messages` column.

## Navigation back-stack + drawer model

The drawer is modeled as a *virtual route*: opening it pushes one history entry but keeps the URL at `/` (`openDrawer` → `pushNavEntry('drawer')` + `drawerPushedRef = true`). The design satisfies a few hard desiderata — no "two drawers" artifact during Chrome-Android swipe-back, the 250ms slide stays visible, one back-press exits the PWA from home, and closing the drawer (overlay tap / X) must never navigate. Three load-bearing invariants in `useNavigation.js` enforce this: (1) **`navTo` consumes the existing drawer-sentinel rather than pushing** when the drawer is open (it pushes one `'nav'` entry only if the drawer was closed), so an in-app nav reuses the drawer's history slot instead of growing the stack — keeping history pinned to a pre-drawer snapshot and killing the BFCache artifact; (2) **every close path funnels through `history.back()` → `handleBack`**, whose drawer-first guard (`if (drawerOpenRef && drawerPushedRef) { close; return }`) prevents over-popping `navStackRef`; (3) **`drawerPushedRef` is a ref, not state** (mutated synchronously in the same task as the history call) and is the single source of truth for "is a drawer-sentinel above the current entry." Every shell-pushed entry is tagged `{__mobiusNav:true, kind}` via `navHistory.js` and written to *both* the classic History store and the Navigation API entry (`updateCurrentEntry`); both back handlers ignore untagged pops so sandboxed-iframe phantom entries can't over-pop — do not drop the tag from any push site or genuine sentinels read as phantoms and back-nav dies. Mini-apps install their own back-targets via the `moebius:nav-push` postMessage protocol (per-app counts in `appSentinelCountsRef`, capped at 20), consumed before navStack pops; `Shell.deleteChat` must scrub `navStackRef` of the deleted chat's entries or back lands on a 404'd chat. Three architectures were tried and rejected (per-nav pushState, `flushSync`-before-pushState, perpetual single-sentinel) — read `tests/navigation.spec.mjs` (22 invariants) before changing anything.

## Service worker + offline

Möbius uses one root-scoped service worker, `frontend/src/sw.js`, to keep shell and mini-app navigations same-origin when offline. The shell route is the Workbox app-shell path: `NavigationRoute(createHandlerBoundToURL('/index.html'))` serves the precached shell, with `/apps/`, `/recover`, `/shell/embed`, `/sites`, and selected published-style paths denied so backend-owned documents don't become the SPA by accident. Mini-app code is split from that shell path: `/api/apps/{id}/frame` and `/api/apps/{id}/module` match `isAppCodeRoute()` and go through `appCodeHandler(OFFLINE_APPS_CACHE, { gated: false })` — frame/module caching is deliberately NOT gated by `offline_capable`. Standalone `/apps/<slug>/` navigations use the same handler with `gated: true`: only a `200` carrying `X-Mobius-Offline: 1` is stored; a headerless `200` purges the standalone entry. The server sets that header for `offline_capable` apps in `routes/apps.py:get_frame`/`get_module` and `routes/standalone.py:standalone_shell`.

`appCodeHandler()` normalizes the cache key by stripping `token`/`_`/`install` but KEEPING `v`; freshness rides `?v=<app.updated_at>` becoming a new key, not a connectivity probe. Once a versioned entry exists, `shouldServeCacheFirst()` serves it immediately while `event.waitUntil()` refreshes in the background. Cold paths and refreshes use `cache: 'reload'` through `boundedFetch()` so browser HTTP-cache revalidation can't hand the SW a bodyless `304` (`NET_TIMEOUT_MS` is a 3000ms hang guard, not a latency knob). `appCodeStoreAction()` is the storage policy: ungated frame/module stores every `200`, gated standalone stores only `X-Mobius-Offline: 1`, all non-`200` ignored; `applyAppCodeStore()` tolerates quota failures and deletes superseded same-route entries with a different `v`.

Install-time precache includes the Vite shell plus a large same-origin vendor set (React 19.2.6, CodeMirror, Recharts, date-fns, d3-geo, marked, DOMPurify, d3, PixiJS) appended to `self.__WB_MANIFEST` — runtime `/vendor/` stays `CacheFirst`, but any lib an app can statically import before its error UI renders must be promoted into the precache list. `setCatchHandler()` returns precached `index.html` outside `/apps/` and `offline.html` for standalone/app-asset failures, avoiding native offline chrome. Two anti-patterns: do NOT reintroduce a `mobius-shell-nav` HTML cache (navigations bind to the precached `index.html` so HTML and hashed bundles advance together), and do NOT gate in-shell frame/module reads on `offline_capable` (that flag gates standalone offline opens + write semantics, while frame/module speed + warmup are universal). There is no hand-edited `VERSION` constant: `activate` deletes stale runtime caches via `isStaleRuntimeCache`, and Workbox handles content-versioned precache cleanup separately.

## Mini-app manifest (mobius.json)

Every mini-app ships a `mobius.json`; the enforcing source of truth is `install._validate_manifest` (`backend/app/install.py`), reached via `POST /api/apps/install`. **Five required fields** (`_REQUIRED_FIELDS`), all present-and-truthy or install 400s: `id`, `name`, `version`, `description`, `entry`. The `id` is the manifest identity and the initial slug (source dir `/data/apps/<slug>/`; `allocate_unique_slug` can diverge it on a collision, and cron registration keys off the resolved `app.slug`), so it has strict slug rules (`_validate_slug_field`): charset `a-z 0-9 - _`, not starting with `-`/`_`, not purely numeric (bare integers are reserved for the numeric-id storage tree). Optional fields the parser actually recognizes: `previous_id` (rename migration; same slug rules, must differ from `id`), `icon` (repo-relative path, 12 MB cap), `theme_color`/`background_color` (coerced to `#RRGGBB` by `_manifest_color`; non-hex silently → None, background falls back to theme), `offline_capable` + `embeds_agent` (bools, on the App row), `display` (web-manifest mode standalone/fullscreen/minimal-ui/browser via `_manifest_display`, else None), `offline` (object — `reads:bool`, `writes:queued|none`, `execution:full|partial|none`, `precache:[paths]` — validated by `_validate_manifest_offline`, stored as `App.offline_contract` JSON), `permissions`, `storage_seeds`, `static_assets`, `schedule`. Decorative-only (not validated, not stored): `author`, `license`, `homepage`. Three gotchas: (1) **`runtime` (`imports`/`esm_deps`) is informational — the installer never reads it**; dependency resolution is governed solely by `runtime_libs.py` + the two importmaps, so a dep resolves only if wired there. (2) **`storage_seeds` value type is a switch**: a string is a repo-relative path the installer *fetches* (`_validate_repo_relative_path` — inlining literal markup trips on the first `://` or `#`); a non-string is stored *inline* as a JSON literal. (3) **`schedule.job` has dual semantics** — with `schedule.default` (a `_validate_cron_expr` 5-field expression) it installs a recurring cron job; without it the script lands on disk as an on-demand build hook run only via the run-job endpoint. `static_assets` caps at 256 files / 16 MB each / 64 MB total.

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

- **Build / test / run commands and the dev loop:** `CONTRIBUTING.md`. (The #1 deploy gotcha — a stale `/data/shell/dist` masking a fresh image — is covered under *Frontend serving priority* above.)
- **Subsystem deep-dives are inlined above** as their own sections: *Stop-chat contract*, *AskUserQuestion interception*, *Chat persistence — single-writer actor*, *Navigation back-stack + drawer model*, *Service worker + offline*, and *Mini-app manifest (mobius.json)*. (The chat-persistence v2 design + staged-rollout notes remain internal/gitignored — the as-built contract is the section above.)
