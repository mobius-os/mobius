# Möbius architecture

Read this first if you just cloned the repo. It maps the system so you can find the file that owns a behavior in your first hour. Every row was verified against the source on `main`; if you find a row that no longer matches the code, fix the row.

## What Möbius is

Möbius is a self-hosted PWA where one owner chats with an in-product AI agent to build mini-apps and modify the platform itself. The "agent" is a coding-agent (Claude Code or Codex) running as a subprocess inside the container; a chat message spawns a turn, the backend streams the agent's output back over SSE, and the agent can compile JSX into mini-apps, edit the shell UI, manage files, and schedule tasks. The whole platform runs in a single Docker container and installs on Android/iOS as a PWA.

The design has one line behind it: **low floor, high ceiling, no walls.** The agent is the product; everything else is substrate it operates on. Concretely: **code empowers the agent, it does not police it.** Subsystems the agent touches (themes, mini-apps, memory/skills, `/data/shared/`, `/data/apps/`) prefer well-named variables and inline contract comments over server-side rewriting; prevention lives in the agent's instruction layer and learned memory, not in code-level validators. Breaking is allowed because broken states are recoverable (the agent that broke it, or a sibling, fixes it — see `routes/recover.py`). The flip side: infrastructure the agent never sees — provider plumbing, the chat persistence actor, the streaming protocol, the navigation back-stack — gets whatever complexity makes it correct. Maximal expressive surface for the agent, ironclad substrate underneath.

This split is why a section can read either "this is intentionally hackable, don't add a guardrail" or "this is load-bearing, don't touch it without reading the full reference." Both are true; which one applies depends on whether the agent sees the surface.

## Deployment — single container

```
Dockerfile (root)     Single-container image: frontend build + backend + CLI tools
docker-compose.yml    Self-hosted: Caddy (TLS) + app container
├── caddy             HTTPS reverse proxy — forwards everything to app:8000
└── app               FastAPI serves the API + the frontend static files
```

The image bundles everything the agent needs at runtime (the Claude CLI, esbuild, Node) so the platform works out of the box. To join an existing Caddy setup instead of the bundled one, use `docker-compose.override.example.yml`.

### Frontend serving priority (the #1 deploy gotcha)

At startup `backend/app/main.py:764` picks one static directory **at module load time**, not per request:

```
/data/shell/dist/  ← preferred (the agent's live rebuild; persists across image rebuilds)
/app/static/       ← fallback (baked into the image, current with git HEAD)
```

The `/data` volume persists across `docker compose build && up -d`, so a new image's `/app/static/` is masked by an old `/data/shell/dist/`. After a frontend deploy, refresh both source and dist and verify the bundle hash changed in `/data/shell/dist/assets/index-*.js`. Because the choice is made at module load, an in-container shell rebuild does not take effect until the uvicorn process restarts. Never delete `/app/static/` — it is the only recovery fallback and is root-owned.

## Backend (`backend/app/`)

FastAPI app. `main.py` is the factory (CORS, rate limiting, routers, static serving). `routes/__init__.py` is a crash-tolerant import scaffold: every router is loaded through `_load(name)`, and an import failure returns a 503 stub instead of killing uvicorn, so `/recover/chat` stays reachable. To add a route, write the module under `routes/`, expose a `router`, and register it in `routes/__init__.py` (both the `_load(...)` line and `__all__`), then mount it in `main.py`.

### Core app + chat runtime

| File | Role |
|------|------|
| `main.py` | App factory: CORS, rate limiting, router mounting, static file serving; resolves `_static_dir` at load (`main.py:764`) |
| `config.py` | `Settings` via pydantic-settings; reads `.env` |
| `database.py` | SQLAlchemy engine, `SessionLocal`, `Base`, `get_db` |
| `models.py` | ORM tables: `Owner`, `Chat`, `ChatRun`, `App`, `PushSubscription`, `Notification` |
| `schemas.py` | Pydantic request/response models |
| `auth.py` | bcrypt hashing, JWT creation/decoding, Fernet encryption |
| `deps.py` | FastAPI auth dependencies: `get_current_owner` (owner-only), `get_current_owner_or_app` (owner + app token), `get_principal`, `require_app_permission`, and `reject_cross_site` (CSRF) |
| `compiler.py` | `compile_jsx()` — calls the esbuild CLI to compile a JSX string into an ES module |
| `providers.py` | `BaseProvider` adapters (`ClaudeProvider`, `CodexProvider`) + the `PROVIDERS` registry; identity/auth/env shaping for the SDK runners, and `get_skill_path()`. (`CodexProvider.build`/`parse_line` — the app-server subprocess path — are retained but not on the live chat path; see `codex_appserver.py`.) |
| `claude_sdk_runner.py` | Claude SDK turn runner; passes `cli_path="/usr/local/bin/claude"` so the SDK drives the same pinned binary recovery + cron use |
| `codex_sdk_runner.py` | Codex SDK turn runner (Thread/TurnHandle + steer) |
| `codex_appserver.py` | Pure-function translator for the Codex `app-server` JSON-RPC protocol. The subprocess streaming path it served is legacy (live Codex chat runs through `codex_sdk_runner.py`), but this module stays live — the SDK runner reuses its event-classification + bash-extraction helpers |
| `chat.py` | `run_chat()` background task: spawns the turn, publishes events, routes persistence through the actor |
| `chat_writer.py` | Single-writer chat-persistence actor — one thread owns the DB session + a FIFO command queue; ALL `Chat.messages` / `Chat.pending_messages` mutations route through it (do not write those columns directly) |
| `chat_queue.py` | Per-chat queue lock + turn-end `drain_and_release` / `promote_pending_messages_locked` + the `TerminalDisposition` state machine; the awaited bridge between `chat.py` and the writer actor |
| `broadcast.py` | `ChatBroadcast` per-chat in-memory event bus; decouples the turn runner from SSE clients |
| `events.py` | Pure data transforms accumulating streaming events into the persisted message structure |
| `compaction.py` | Cross-provider chat compaction (portable plain-text summary; native SDK compaction is within-provider only) |
| `runner_registry.py` | Runner lifecycle registry shared across chat backends |
| `pending_questions.py` | Shared `PendingQuestion` registry for AskUserQuestion interception |
| `tool_summaries.py` | Tool-input summary strings (shared by SDK + subprocess paths) |
| `sdk_emit.py` | Helpers for emitting "unknown" SDK events on the SSE wire |

### Mini-apps, storage, files

| File | Role |
|------|------|
| `install.py` | Atomic install + update lifecycle for mini-apps from a manifest |
| `app_git.py` | Per-app git repo (`/data/apps/<slug>/.git`): pristine `upstream` history + a local working branch |
| `app_watcher.py` | File watcher that auto-recompiles a mini-app's source on edit |
| `storage_io.py` | Filesystem helpers for per-app + shared storage; lives apart from `routes/storage.py` so `install.py` can reuse it |
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

These bootstrap an agent into a broken instance and are deliberately isolated from the SDK/chat stack so a broken SDK install cannot take recovery down. Most are chmod 444/555 root-owned (`protected-files.txt`); the agent cannot edit them.

| File | Role |
|------|------|
| `recover_chat.py` | Recovery chat: vanilla HTML page + send/stream/reset endpoints |
| `recover_chat_runner.py` | Minimal CLI runner for the recovery chat; shares no code with `chat`/`providers`/SDK (runs the standalone `claude` binary as its own subprocess) |
| `recover_auth.py` | Recovery-page auth |
| `recover_oauth.py` | OAuth handling for the recovery flow |
| `bootstrap.py` | First-boot bootstrap that auto-installs the curated app-store mini-app |
| `chat_log_redaction.py` | Server-side structural redaction for the gated chat-log read API |
| `http_caching.py` | Range/206 hardening for revalidating `FileResponse`s |
| `timeutil.py` | `now_naive_utc()` + `SOFT_DELETE_TTL`; SQLite stores naive datetimes (mixing aware/naive `TypeError`s on compare) |
| `presence.py` | Owner presence tracking |
| `questions.py` | Question helpers |

### Routes (`backend/app/routes/`)

Each module exposes a `router`; registration is in `routes/__init__.py`.

| File | Role |
|------|------|
| `auth.py` | Setup, login, CLI provider PKCE OAuth (`/api/auth/provider/*`) |
| `apps.py` | Mini-app registry CRUD + `/module` and `/frame` serving (ETag revalidation) |
| `chat.py` | `POST /api/chat/stop` — interrupts the agent turn |
| `chats.py` | Chat CRUD + reversible soft-delete with recovery |
| `chats_stream.py` | `POST /messages` (starts a turn, returns 202) + `GET /stream` (SSE) |
| `chat_logs.py` | Gated, redacted chat-log read API for mini-apps |
| `storage.py` | Per-app and shared file storage |
| `fs.py` | Owner-facing filesystem + git oversight API |
| `uploads.py` | Per-chat file upload management |
| `generate.py` | Gemini image-generation endpoint (image-only; not a chat provider) |
| `proxy.py` | Server-side CORS-bypass proxy for mini-apps |
| `standalone.py` | Top-level routes that make a mini-app installable as its own PWA (own importmap) |
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
| `ChatEmbed/` | In-app embedded chat surface (agent chat inside a mini-app) |
| `SettingsView/` | Theme, provider auth, owner config |
| `SetupWizard/` | First-boot: account + provider auth |
| `LoginForm/` | Subsequent logins |
| `ProviderAuth/` | Reusable Claude OAuth flow |
| `ProviderModelPicker/` | Provider + model selection UI |
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
| `ComposerPopover.jsx` | The `+` popover: attach files + model/effort/provider picker (rendered by `ChatSettingsPanel.jsx`) |
| `MsgContent.jsx` | Per-message rendering: markdown, tool blocks, attachments |
| `ToolBlock.jsx` | Collapsible tool-execution block with status |
| `StreamingMessage.jsx` | The live, in-progress assistant message |
| `QuestionCard.jsx` | AskUserQuestion UI (gates the turn) |
| `QueuedMessages.jsx` | Tray of messages queued while a turn streams |
| `CompactionCard.jsx` | Compaction summary affordance |
| `Attachments.jsx` | File/image attachment previews |
| `ConnectionStatus.jsx` | SSE reconnection indicator |
| `ManageModelsModal.jsx` | Model management modal |
| `streamReducers.js` | Stream-event reducers |
| `resolveStopResend.js` | Stop → collapse-queue → re-send logic |
| `useStreamConnection.js` | SSE connection, text buffering, typewriter drain, sleep/wake reconnect |
| `useScrollMode.js` | Scroll-mode state machine |
| `useVoiceInput.js` | Web Speech API with Android-Chrome workarounds |
| `useFileUpload.js` | File-upload state + API calls |
| `markdown/` | `BlockRenderer.jsx`, `blocks.jsx`, `InlineContent.jsx`, `ImageLightbox.jsx`, `highlight.js` (lazy highlight.js), `math.js` (KaTeX) |

### Hooks (`frontend/src/hooks/`)

| Hook | Role |
|------|------|
| `useNavigation.js` | Navigation stack, pushState/popstate, the Navigation API (back-stack model in `docs/navigation.md`) |
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
| `frontend/src/sw-cache-policy.js` | Authoritative cache-route policy (see `docs/offline.md`) |
| `frontend/src/lib/` | Cross-cutting helpers: `appToken.js`, `chatEmbed.js`, `themeService.js`, `onlineStatus.js`, `navHistory.js`, `errorLog.js`, etc. |

## Where do I make a change?

| Task | Start here |
|------|------------|
| New API route | New module in `backend/app/routes/` exposing `router` → register in `routes/__init__.py` (`_load(...)` line + `__all__`) → mount in `main.py` |
| New ORM table / column | `backend/app/models.py` (and a manual `ALTER TABLE` — `create_all` never ALTERs an existing table) |
| Change request/response shape | `backend/app/schemas.py` + the owning route |
| Add an auth dependency / change CSRF | `backend/app/deps.py` |
| Persist anything chat-domain | A domain command in `backend/app/chat_writer.py` — never write `Chat.messages`/`Chat.pending_messages` directly |
| Add an AI provider | New `BaseProvider` subclass + a row in `PROVIDERS` (`backend/app/providers.py`) plus the matching SDK runner |
| Change chat streaming UI | `ChatView/ChatView.jsx` + `ChatView/useStreamConnection.js` (+ `streamReducers.js`) |
| Change chat scroll/spacer/keyboard | `ChatView/ChatView.jsx` + `ChatView.css`; run the spacer/send-pin tests in repo-root `tests/` |
| Change drawer / back-stack nav | `frontend/src/hooks/useNavigation.js` + `Shell/Shell.jsx` (read `docs/navigation.md` first) |
| Change the mini-app iframe / cache | `AppCanvas/AppCanvas.jsx` + `Shell/Shell.jsx` (`appCache`); ETag logic in `routes/apps.py` |
| Add an app-runtime capability | `frontend/public/mobius-runtime.js` + both importmaps (`frontend/public/app-frame.html` and `backend/app/routes/standalone.py`) |
| Add a runtime vendor library | `backend/app/runtime_libs.py` + both importmaps + the Dockerfile vendor copy |
| Change offline / SW behavior | `frontend/src/sw.js` + `frontend/src/sw-cache-policy.js` (read `docs/offline.md` first) |
| Change the in-product agent's instructions | `skill/core.md` (constitution) or `backend/scripts/seed-skills/*.md` (per-task skills) — see below |
| Theme CSS / tokens | `backend/app/theme.py` + `routes/theme.py` + `frontend/src/hooks/useTheme.js` |

## In-product agent context — three layers

The in-product agent is a first-class reader of this code, and its behavior is governed by three layers, not one. (1) **Constitution** — `skill/core.md`, the owner-curated system prompt, baked to `/app/skill/core.md`; `chat.py` reads it into `system_prompt` (the Claude SDK receives it on every turn; the Codex SDK uses it as base instructions only when starting a new thread, i.e. `session_id is None`). `providers.get_skill_path()` resolves `core.md` only (there is no `agent-skill.md` fallback). (2) **Skills** — `/data/shared/skills/*.md` (building-apps, theming, cron, notifications, recovery, memory, reflection, …), seeded create-if-absent by `backend/scripts/init_skills.py` from `backend/scripts/seed-skills/`; the agent `Read`s the relevant one on demand and may edit them. (3) **Memory** — the knowledge graph under `/data/shared/memory/` (`index.md` + `mocs/` + `notes/` + `graph.json` + `.ready`), injected progressive-disclosure by `backend/app/memory.py` into the first user message as an `<agent_experience>` block (not the system prompt, so static content caches), indexed by `memory_graph.py`, and viewed through the Memory mini-app. To change agent behavior, edit `skill/core.md` and the seeds — not code-level validators (see the design philosophy above).

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
└── service-token.txt       long-lived JWT for cron scripts (chmod 600)
```

`/data` is itself a git repo owned by the `mobius` user, tracking `shared/memory/` and `shared/skills/` with a nightly safety-net commit, so a bad memory consolidation or skill overwrite is recoverable. Inspect it as that user (`docker exec -u mobius ... git -C /data ...`) — as root it dies with "dubious ownership," which reads misleadingly as an empty/non-repo tree.

## See also

- **Build / test / run commands and the dev loop:** `CONTRIBUTING.md`. (The #1 deploy gotcha — a stale `/data/shell/dist` masking a fresh image — is covered under *Frontend serving priority* above.)
- **Chat persistence (the hard subsystem):** `docs/persistence/` — start at `docs/persistence/README.md`; `redesign.md` is the full design, with `activation-design.md`, `terminal-completion-design.md`, `chat-writer-actor-plan.md`, and `behavioral-tests.md` alongside.
- **Other load-bearing subsystems:** `docs/navigation.md` (back-stack), `docs/offline.md` (service worker), `docs/stop-chat-contract.md`, `docs/ask-user-question.md`, `docs/mobius-json.md` (app manifest format).
