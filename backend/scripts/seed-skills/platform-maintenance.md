# Platform maintenance

How backend edits load, how to make them durable, where platform files live,
and how to repair a broken shell. Read this before editing backend Python,
changing the image userspace, or asking for a server restart.

---

## Authority and the external repair boundary

The normal Möbius agent has passwordless full root inside its own container by
default. Check the operator-controlled capability before root work:

```bash
sudo -n true
```

Use `sudo` deliberately when the task needs it. If that check fails, stop: the
operator disabled root with `MOBIUS_AGENT_SUDO=0`, and bypassing that choice is
not part of the agent's authority. Package changes made only in a running
container are ephemeral; declare them in the Dockerfile or lockfile and ship a
new image when they must survive recreation.

Recovery is not a daemon, listener, alternate boot mode, or second process
inside Möbius. If the interface is unavailable, ask the partner to open
**Recovery** from the deployment card in Möbius Launch. The launcher creates a
separate temporary worker on demand and pins Railway SSH to the exact live
Möbius service instance. Commands reach that container as root, while the
worker itself remains outside the container and is deleted when the session
finishes or expires. Never try to start or repair an in-container Recovery
service; none should exist.

Self-hosted operators use the authority they already own:

```bash
docker compose exec -u 0 app bash
```

That also attaches to the normal live container; it does not select a Recovery
boot profile.

---

## Choose the smallest activation action

Do not infer activation from the fact that a task touched platform source.
Review the exact changed paths and use the smallest matching action:

| Changed surface | Smallest activation |
|---|---|
| `/data/shared/theme.css` | Hot-reloads after the theme notification. No build or restart. |
| Mini-app source under `/data/apps/<slug>/` | Run `apply_app.py`; the compiled app live-swaps. No shell rebuild or server restart. |
| `frontend/src/` and other frontend build inputs | The watcher rebuilds the served shell, then `shell_apply_now` applies it. A normal save triggers this automatically; source arriving through Git needs a changed frontend file touched. No server restart. |
| `backend/app/*.py` | After compile checks, tests, and commit, one server restart loads the settled backend revision. |
| `skill/core.md` | A server restart refreshes the cached constitution for new agent sessions only; existing sessions keep their immutable prompt snapshot. Unless new sessions need the rule immediately, leave it pending for the next separately approved restart. |
| `backend/scripts/`, tests, docs, and shared skill content | Takes effect on its next invocation or read. No server restart. An agent that already read old instructions cannot be rewritten in place. |
| A package needed by the current task | Install it into the running container first when safe. A new process can use it immediately; restart only when the already-running backend must load it. |
| `backend/requirements.txt`, lockfiles, `frontend/package.json`, or `Dockerfile` | These declarations make a live install reproducible after container replacement; they do not activate it and do not require an immediate rebuild. |

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
3. If shipped behavior depends on the package, record the same resolution in
   the owning manifest and lockfile, plus the Dockerfile only when image wiring
   is needed. These declarations are durability metadata, not an activation
   action: they let a future image/container replacement restore the live
   install.
4. Treat a container rebuild as a last resort, not an ordinary closeout step.
   Require it now only when the change genuinely cannot activate live, or when
   the partner explicitly asks to validate the image.

### Activation preflight — before any restart question

1. List the exact paths changed for the current task and whether a later owner
   action has already activated them.
2. Map every path through the table above. If none still requires a server
   restart, do not offer one. Never substitute a restart for hot reload, app
   apply, shell rebuild, live dependency install, or container rebuild.
3. Batch every restart-requiring edit, test it, and commit it before asking.
   Do not restart between iterations or request a speculative restart.
4. In the question, name the exact change that remains inactive and why only a
   server restart can activate it. For a constitution-only change, default to
   leaving it pending unless the partner needs the rule in new sessions now.

---

## Backend edits — restart to load, hand off persistence

The live backend is `/data/platform/backend/app/*.py`. Edits there take effect
on the **next uvicorn restart** and survive container restarts and image rebuilds
because `/data/platform` is the persistent served clone. The baked
`/app/platform-baked` tree is a read-only fallback, not the normal edit surface.

- A bad import keeps the edited tree from serving. Boot import-probes the
  persistent clone and falls back to the baked backend, leaving the local tree
  intact for repair. Always run `python3 -m py_compile <file>` before a restart.
- A local fix is persistent but not upstream. Boot preserves committed local
  changes and reconciles them over newer `origin/main`; a future reconcile can
  still conflict, and another installation will not receive the fix. Ask
  whether it is a local overlay or needs a separate upstream handoff. Do not
  push or manage external repository workflow from inside Möbius.

All chat-persistence writes must route through the `chat_writer` actor. Never
assign `Chat.messages` or `Chat.pending_messages` directly; see `core.md` for
the write-surface contract.

### Backend-fix loop

1. Record the starting revision, edit `/data/platform/backend/app/...py`,
   compile every changed Python file, and run focused tests.
2. Commit only the exact paths you own with `PM_COMMIT_ROOT=/data/platform
   pm-commit --from <starting-sha> '<what and why>' -- <paths>`.
3. Run the activation preflight. Only if it proves that the settled backend
   change is not live, stop and ask through the clarifying-question tool
   immediately before restarting. Explain that the restart interrupts every
   active agent turn, name the current number of running turns when known, and
   warn that service may be unavailable for tens of seconds. Offer **Restart
   now** and **Not now**. Approval of the task, a broad “go ahead” or “fix it,”
   or delegation of the complete backend-fix loop does not approve a restart.

   A **Restart now** answer authorizes exactly one safe restart call:

   ```bash
   curl -fsS -X POST "$API_BASE_URL/api/admin/restart" \
     -H "Authorization: Bearer $AGENT_TOKEN" \
     -H "Content-Type: application/json"
   ```

   The current tool call ends as the worker exits and Möbius resumes the turn.
   A second restart, or an ambiguous outcome where you cannot prove whether the
   call reached the server, requires a new question. A scheduled/background
   agent cannot ask live, so it leaves the restart pending for the partner.
4. If the edited tree fails to import, the baked shell stays available. Refresh
   and repair `/data/platform` there, or use external Recovery if the interface
   itself is unavailable.
5. Restart time varies with active work and boot time; the page reloads when
   healthy. Verify the fix in the original chat.

## SQLite migrations

SQLAlchemy `create_all` creates missing tables; it never adds a column to an
existing table. A new model field therefore needs an explicit migration for the
existing `/data/db/ultimate.db`. Test both a fresh database and an upgraded
database rather than assuming model metadata altered the latter.

---

## File locations

- Uploaded files: `/data/chats/{chat_id}/uploads/`
- Chat media: `/data/chats/{chat_id}/media/`
- Encrypted app credentials: `/data/app-secrets/{app_id}/` — use the app-secret
  API, never edit ciphertext files.
- Per-app storage (numeric id): `/data/apps/{app_id}/<path>`
- Per-app source (slug): `/data/apps/{slug}/`
- Shared storage: `/data/shared/<path>`
- Compiled bundles: read the exact `compiled_path` from `GET /api/apps/{id}`
- Cron logs: `/data/cron-logs/`
- Owner service token: `/data/service-token.txt` (mode 0600)

Chat files are purged when their chat is permanently deleted after the
retention window. Put data that must outlive a chat in per-app or shared
storage.

## Viewing apps directly

Capture an app through the authenticated shell, which supplies the frame-init
message a standalone frame does not receive:

```bash
bash "$SCRIPTS_DIR/agent-screenshot.sh" --content-only /app/<id>
```

The frame URL is stable and cache-revalidated, but opening it alone normally
ends at “Loading timeout.” Use the authenticated capture helper or the live
shell.
