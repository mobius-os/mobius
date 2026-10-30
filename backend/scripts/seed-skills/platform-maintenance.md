# Platform maintenance

How backend edits load, how to make them durable, and how to repair a broken
shell. Read this before editing backend Python, changing the image userspace, or
asking for a server restart. Call this instance's API with `mapi` as the
constitution describes. Rarer topics (the container's Docker boundary, external
Recovery, host container replacement, file locations, and viewing apps directly)
live in `platform-reference`. Changing Möbius's own source also needs
`mobius-development`, which owns its code invariants, tests, and upstream
validation.

---

## Root authority

The normal Möbius agent has passwordless full root inside its own container by
default. Check the operator-controlled capability before root work:

```bash
sudo -n true
```

Use `sudo` deliberately when the task needs it. If that check fails, stop: the
operator disabled root with `MOBIUS_AGENT_SUDO=0`, and bypassing that choice is
not part of the agent's authority. Package changes made only in a running
container are lost when the container is replaced; see the dependency steps
below for making them durable.

---

## Choose the smallest activation action

Do not infer activation from the fact that a task touched platform source.
Review the exact changed paths and use the smallest matching action:

| Changed surface | Smallest activation |
|---|---|
| `/data/shared/theme.css` | Hot-reloads after the theme notification. No build or restart. |
| Mini-app source under `/data/apps/<slug>/` | Call `apply_app`; the compiled app live-swaps. No shell rebuild or server restart. |
| `frontend/src/` and other frontend build inputs | The watcher rebuilds the served shell, then `shell_apply_now` applies it. A normal save triggers this automatically; source arriving through Git needs a changed frontend file touched. No server restart. |
| `backend/app/*.py` | After compile checks, tests, and commit, one server restart loads the settled backend revision. |
| `skill/core.md` | A server restart refreshes the cached constitution for new agent sessions only; existing sessions keep their immutable prompt snapshot. Unless new sessions need the rule immediately, leave it pending for the next separately approved restart. |
| `backend/scripts/entrypoint.sh`, the exact `/app/scripts/*` bootstrap files it invokes, or `backend/runtime/` | Image-owned. Container replacement installs the official image for the release, which carries no local edit to these files: the edit stays in the checkout, inactive, and the update reports it without waiting on it. Batch and test the change, then prepare it as an upstream contribution; it takes effect with the release that contains it. |
| `backend/runtime/identity_broker.py` | The one served privileged runtime file: one server restart activates a valid edit; an invalid one falls back to the baked platform for that boot. |
| `backend/scripts/pm-commit` | One server restart refreshes the installed launcher from the served checkout; no image rebuild. |
| `backend/scripts/seed-skills/` | One server restart applies the served templates to installed skills; untouched copies advance and edited ones stay for review. No image rebuild once the container runs an image that hands this job to the server. To use an edit immediately, write identical bytes to `/data/shared/skills/<name>.md`. |
| Other `backend/scripts/`, tests, docs, and shared skill content | Takes effect on its next invocation or read. No server restart or image rebuild. An agent that already read old instructions cannot be rewritten in place. |
| A package needed by the current task | Install it into the running container first when safe. A new process can use it immediately; restart only when the already-running backend must load it. |
| `backend/requirements.txt`, lockfiles, `frontend/package.json`, or `Dockerfile` | These declarations do not activate a live install and do not require an immediate rebuild. They survive container replacement only through the release that contains them; see Dependencies below. |

### Verify activation without confusing image and source versions

`mapi /api/platform/status` names the remaining activation in
`activation.required_actions`, `needs_restart`, and `unfinished_update`.
Its `current_build_sha` is the immutable image revision, not the source the
running server imported. `served_sha` identifies that served source; local
reconciliation normally makes these revisions different. A server restart
cannot change the image revision, so never request one just to make these
values match. `mapi /api/version` also reports `serving_source`, `served_sha`,
and frontend freshness. Use those owning facts to verify a ready boot.

### Dependencies — live first, durable second

1. Run `sudo -n true` once. Use `sudo` for apt or other system/global install
   locations, not for ordinary writes under `/data`, which must remain
   partner-owned.
2. Use the owning package manager to install only the named dependency, pinned
   to the intended version when possible. Do not run blanket upgrades or
   ad-hoc remote installers. Put Python packages in the active interpreter and
   Node packages in the active runtime dependency tree; a global Node install
   does not satisfy a project's imports. Verify the import, executable, or
   version. New processes can use the install immediately. A long-running
   backend needs one approved server restart only when it must load the new
   package itself.
3. If shipped platform behavior depends on the package, record the same
   resolution in the owning manifest and lockfile, plus the Dockerfile only
   when image wiring is needed, and contribute it upstream. These declarations
   are durability metadata, not an activation action: they reach installations
   through the release that contains them. Committed only locally, they never
   reach an image and never block updates.
4. To keep a local or app install across container replacements, declare a
   setup step: `{"setup":{"steps":["restore.sh"],"apt":["poppler-utils (>= 25)"]}}`
   in the app's accepted `mobius.json` (ship scripts through `source_files`,
   then Apply) or in `/data/customizations/mobius.json` for the instance. A
   step script gets `check` (exit 0 ready, 1 needs apply, 2 conflict) or an
   idempotent `apply`, runs as `mobius` from its manifest's directory (sudo as needed),
   and writes persistent output under `/data`. Version syntax belongs to the
   underlying manager; all `apt` entries are solved together by
   `apt-get satisfy`, never forcing removals. Restoration runs after readiness
   and update settlement on every boot and accepted declaration, and never
   holds up boot or updates. `GET /api/setup` shows each step's state and the
   running one; `POST /api/setup/rerun` cancels it and starts a fresh pass.
   A root script can still disrupt the platform: this is restoration, not a
   sandbox.
5. Treat a container rebuild as a last resort, not an ordinary closeout step.
   Require it now only when the change genuinely cannot activate live, or when
   the partner explicitly asks to validate the image.

### Activation preflight — before any restart question

1. List the exact paths changed for the current task and whether a later owner
   action has already activated them.
2. Map every path through the table above. Do not propose an unnecessary
   restart for routine activation or substitute one for hot reload, app apply,
   shell rebuild, live dependency install, or container rebuild. An explicit
   partner request may still create a Restart card without pending changes.
3. Batch every restart-requiring edit, test it, and commit it before asking.
   Do not restart between iterations or request a speculative restart.
4. For activation, name the exact change that remains inactive and why only a
   restart can activate it. If no change is pending, say so. For a
   constitution-only change, default to leaving it pending unless the partner
   needs the rule in new sessions now.

---

### Debugging the platform runtime

Use the existing authenticated diagnostics instead of adding temporary routes:

```bash
mapi /api/debug/status | python3 -m json.tool
mapi "/api/debug/memory?process_limit=20&allocation_limit=25" | python3 -m json.tool
mapi "/api/debug/logs?lines=50&chat_id=$CHAT_ID" | python3 -m json.tool
mapi "/api/debug/profile?seconds=15" | python3 -m json.tool
```

(`mapi` fills in auth + base URL.)

`status` is the cheap health view and deliberately omits variable-sized runtime
payload totals; its `runtime_memory.payload_sizing` field points to the detailed
`memory` report. Query flags on `status` do not enable payload sizing. Use
`memory` for processes, maps, runtime-owner payloads, GC diagnostics, and
optional allocation tracing. Add `deep=true` only when a GC object-type walk is
actually needed.

When the backend is slow or CPU-bound, `profile` samples every thread in-process
(py-spy cannot attach inside the container) and ranks the busy app frames and
stacks. It occupies one worker for the window, and one profile runs at a time.

Exercising owner-input cards programmatically (for a test, never to answer for
the owner): while a card is open, posting a chat message returns 409
`pending_question_open` — that is the designed gate, not a bug. The two-step is
to answer the card at `POST /api/chats/<id>/question-answers` with its
`question_id`, which records the answer but does **not** resume the paused
turn, and then `POST` a chat message to start the next turn.

---

## Backend edits — restart to load, hand off persistence

The live backend is `/data/platform/backend/app/*.py`. Edits there take effect
on the **next uvicorn restart** and survive container restarts and image rebuilds
because `/data/platform` is the persistent served clone. The baked
`/app/platform-baked` tree is a read-only fallback, not the normal edit surface.

- A bad import keeps the edited tree from serving. Boot import-probes the
  persistent clone and falls back to the baked backend, leaving the local tree
  intact for repair. Always run `python3 -m py_compile <file>` before a restart.
  A commit that imports but crashes or hangs while the server starts is set
  aside the same way after two such boots in a row; `/data/logs/platform-boot.jsonl`
  records an `admit` refusal. A new commit or an approved restart tries it again.
- A local fix is persistent but not upstream. Startup preserves installed source and local changes without fetching newer
  code. Explicit updates reconcile local changes onto the reviewed release;
  they can still conflict, and another installation will not receive a local fix. Ask
  whether it is a local overlay or needs a separate upstream handoff. Do not
  push or manage external repository workflow from inside Möbius.
- `/data/platform` is its own Git repository. The separate `/data` safety-net
  repository ignores `platform/`, so a bare `/data` commit never records
  platform source; never sweep platform source with `git add -A`.

### Backend-fix loop

1. Record the starting revision, edit `/data/platform/backend/app/...py`,
   compile every changed Python file, and run focused tests.
2. Commit only the exact paths you own with `PM_COMMIT_ROOT=/data/platform
   pm-commit --from <starting-sha> '<what and why>' -- <paths>`.
3. Run the activation preflight. For routine activation, ask only when the
   settled backend change is not live; an explicit partner request may create
   the card without a pending change. Explain that the restart interrupts active
   turns and may make service unavailable for tens of seconds, then call
   `request_restart` as the final action.

   `request_restart` takes no action arguments. The platform derives the exact
   committed, restart-loadable source and saves its own card with one exact
   **Restart now** action plus a written-response path. Its receipt confirms
   only that the card was saved: end the turn with no further text or tools.
   Any authenticated participant that can read the card may answer it, just as
   with ordinary Q&A. A **Restart now** selection is dispatched by the platform;
   the answering agent never issues or replays a shell command. A written
   response continues the conversation without restarting. Do not use
   `request_approval` or Codex's
   `request_user_input` for platform restart permission.

   Every chat that still owes activation and verification calls
   `request_restart` itself, even if another chat already has a Restart card.
   Each card saves that chat's visible owner decision and post-restart
   continuation. These are separate chat handoffs, not duplicate restart
   executors: selecting any card requests one platform-owned restart, and a
   later ready boot resumes every still-registered chat independently. Each
   agent then verifies its own changes before completing its Goal. Do not
   substitute a peer note, shared work claim, or monitor of the owner's
   decision for your own card. Stop or a written response cancels only that
   chat's activation wait; unrelated owner-input and recovery holds still apply.

   If the tool is absent, the same saved-card operation is available through:

   ```bash
   python3 /data/platform/backend/scripts/mobius_control_mcp.py call request_restart
   ```

   A failed save is not a waiting card and not consent. Retry only the
   identical request to recover its receipt. If the running backend predates
   this operation, ask plainly and leave activation pending; never fabricate
   a card or park a process waiting for an answer.

   Never replay a restart after a lost response or uncertain outcome; that
   needs a fresh, specific selection. After the restart, the chat resumes on
   its own: verify that your changes loaded. A scheduled/background agent
   cannot open a live card, but may answer an existing one it can access.
4. If the edited tree fails to import, the baked shell stays available. Refresh
   and repair `/data/platform` there, or use external Recovery (see
   `platform-reference`) if the interface itself is unavailable.
5. Restart time varies with active work and boot time; the page reloads when
   healthy. Verify the fix in the original chat.

## Database readiness

Schema changes follow `mobius-development`. If `/api/ready` reports `reason: schema_mismatch` or
`database_initialization_failed`, the process has deliberately skipped its
writer, reconciliation, cron, and database supervisors. Recovery repairs the
database externally, then the partner approves one normal restart so boot can
verify the database and start those owners coherently. Do not hand-edit the
in-memory readiness verdict or try to start skipped owners piecemeal.
