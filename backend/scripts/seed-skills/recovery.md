# Recovery, backend edits, and data layout

How backend edits load, how to make them permanent, the SQLite migration gotcha, `/data`-as-a-git-repo, file locations, and soft-delete recovery. `Read` this before editing backend Python or doing anything you might need to undo.

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
| A package needed by the current task | Install it into the running container first when safe. A new process can use it immediately; restart the server only when the already-running backend must load it. |
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
   Require it now only when the change genuinely cannot activate live (for
   example a base-image/ABI change or an artifact produced only by an image
   build), or when the partner explicitly asks to validate the image.

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
`/app/platform-baked` tree is a read-only recovery floor, not the normal edit
surface. Two failure modes to respect:

- **A bad import keeps the edited tree from serving.** Boot import-probes the
  persistent clone and falls back to the baked backend when that probe fails,
  leaving the local tree intact for repair. Always `python3 -m py_compile
  <file>` before asking for a restart;
  a failing compile proves the edited tree cannot pass that gate.
- **A local fix is persistent but not upstream.** Boot preserves committed local
  changes and reconciles them over newer `origin/main`, so an edit can survive
  container and image deploys; a future reconcile can still conflict, and the
  fix is not reusable by another installation. Host-repo/PR/release work is a
  handoff outside the in-product agent. If unsure, ask whether the fix is a
  one-off local overlay or needs an upstream handoff. Do not push, publish, or
  manage external repo workflow from inside Möbius.

All chat-persistence writes must route through the `chat_writer` actor — never assign `Chat.messages` / `Chat.pending_messages` directly (see core.md's write-surface section for why).

### The backend-fix loop

1. Edit `/data/platform/backend/app/...py` in place; `py_compile` it.
2. Test first, then commit only the intended platform paths.
3. Run the activation preflight above. Only if it proves that the settled
   backend change is not live, stop and ask through the clarifying-question
   tool immediately before restarting. Explain that the restart interrupts
   every active agent turn, name the current number of running turns when
   known, and warn that service may be unavailable for tens of seconds. Then
   offer **Restart now** and **Not now**. Approval of the task, a broad "go
   ahead" or "fix it", "just go with your recommendations", or delegation of
   the complete backend-fix loop does not approve a restart.

   A **Restart now** answer authorizes exactly one safe restart call:

   ```bash
   curl -fsS -X POST "$API_BASE_URL/api/admin/restart" \
     -H "Authorization: Bearer $AGENT_TOKEN" \
     -H "Content-Type: application/json"
   ```

   The current tool call ends as the worker exits and Möbius resumes the turn.
   A second restart, or an ambiguous outcome where you cannot prove whether the
   call reached the server, requires a new question. A background or scheduled
   agent cannot use the live question tool, so it must leave the restart pending
   for the partner rather than calling this route.
4. If the edited tree fails to import, the baked shell stays available. Ask the partner to refresh and diagnose `/data/platform` in place.
5. Restart time varies with active work and boot time; the page auto-reloads when healthy.
6. Verify the fix in the original chat (still open, full history intact).

---

## SQLite — `create_all` never ALTERs; new columns need a manual ALTER

SQLAlchemy `create_all` only CREATEs missing tables; it never adds a column to an existing one. A new model field won't appear on an existing `/data/db/ultimate.db` — the column is silently missing in prod and queries fail or read NULL. When you add a model field, run a manual `ALTER TABLE <t> ADD COLUMN <c> ...` against the existing DB, or ship a tiny migration step.

---

## `/data` is a git repo — commit agent-owned state

For platform source, record the starting revision before editing, then use the
path-owned helper against the platform clone:

```bash
git -C /data/platform rev-parse HEAD
PM_COMMIT_ROOT=/data/platform pm-commit --from <sha-before-edit> \
  'one-line what and why' -- <exact changed paths>
```

The helper commits only those paths, preserves unrelated staged and unstaged
work, and stops when another commit touched one of them since the task began.
Never use `git add -A` for platform work.

`/data/` is a separate git repo. After substantial changes to agent-owned state,
commit so undo is clean:

```bash
git -C /data rev-parse HEAD                 # record this before editing
pm-commit --from <sha-before-edit> 'one-line what and why' -- <exact paths>
```

It commits only the declared paths and leaves every unrelated staged or
unstaged change alone. If one of those paths changed in another commit since
the recorded starting revision, it stops for reconciliation instead of
guessing who owns the newest `HEAD`. Shared user data and editable skills are
tracked here, so this history is your undo for a bad app-owned data rewrite or
a skill edit you regret. Scheduled background agents follow the same exact-path
contract; they never snapshot another task's working tree.

To actually roll one back, find the commit that last had the good version and restore just that path:

```bash
git -C /data log --oneline -- shared/<path>   # find the good <sha>
git -C /data checkout <sha> -- shared/<path>  # restore just that file
```

---

| Situation | Action |
|---|---|
| Backend edit, main shell healthy | Settings -> Server -> Restart |
| Edited backend fails to import | Refresh, then use the repair chat from the baked shell |
| Host-level maintenance | Operator runs `docker compose exec -u 0 app bash` |

---

## Chat recovery

Deleted chats remain in the system for **7 days** and can be recovered:

```bash
curl -s -X POST "$API_BASE_URL/api/chats/{chat_id}/recover" -H "Authorization: Bearer $AGENT_TOKEN"
```

Tell the partner about this safety net if they accidentally delete a chat.

## App recovery

Deleted apps are **tombstoned, not destroyed** — they stay for **7 days** with
their source and saved data intact, and can be recovered:

```bash
curl -s -X POST "$API_BASE_URL/api/apps/{app_id}/recover" -H "Authorization: Bearer $AGENT_TOKEN"
```

For a **store-installed** app you can equivalently just reinstall it (same
`manifest_url`) — the install reattaches to the tombstoned row, so it comes back
with the SAME id and all its data. The app id is in this chat's note from when
you logged the deletion. After 7 days a tombstoned app is purged for good. So
uninstall is reversible within the window — still confirm before deleting (see
`building-apps.md`), but reassure the partner it's recoverable if they change
their mind.

---

## File locations

- **Uploaded files:** `/data/chats/{chat_id}/uploads/`
- **Chat media (screenshots + generated):** `/data/chats/{chat_id}/media/`. Startup moves files and stored URLs off the retired `generated/` path; the live API serves only `media/`.
- **Encrypted app credentials:** `/data/app-secrets/{app_id}/` — manage them through `/api/apps/{app_id}/secrets/{name}`, never by editing the encrypted files.
- **Per-app storage (numeric id):** `/data/apps/{app_id}/<path>` — what `PUT /api/storage/apps/{app_id}/...` writes to, keyed by the numeric DB id.
- **Per-app source (slug):** `/data/apps/{slug}/` — where app source lives, keyed by slug. `index.jsx` is the entrypoint and can import sibling `.js`, `.jsx`, `.ts`, or `.tsx` modules. NOT the same dir as storage; the slug tree and the numeric-id tree are separate.
- **Shared storage (cross-app):** `/data/shared/<path>` — what `PUT /api/storage/shared/...` writes to; used for theme.css, agent-settings.json, chat summaries, and app-owned shared data.
- **Compiled bundles:** the App row's `compiled_path`, normally `/data/compiled/app-{app_id}-{sha256}.js`. Read the exact path from `GET /api/apps/{app_id}` instead of guessing a mutable filename.
- **Cron logs:** `/data/cron-logs/`. **Service token:** `/data/service-token.txt` (chmod 600).

Chat files are purged when the chat is permanently deleted (after 7 days). For data that should outlive a chat, use per-app or shared storage.

---

## Viewing apps directly (debugging)

To check an app's rendered output, use the canonical capture helper — it loads the app inside the authenticated Möbius shell, the realistic path the partner takes:

```bash
bash "$SCRIPTS_DIR/agent-screenshot.sh" --content-only /app/<id>
```

The frame URL (`$API_BASE_URL/api/apps/<id>/frame`) is stable per-app (ETag + browser cache handles freshness, no `?v=`), but the frame waits for a parent-shell `moebius:frame-init` postMessage — opening it standalone just shows "Loading timeout." Always go through the authenticated capture helper or the live shell.
