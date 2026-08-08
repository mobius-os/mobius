# Recovery, backend edits, and data layout

How backend edits load (restart, not live-reload), how to make them permanent, the SQLite migration gotcha, `/data`-as-a-git-repo, file locations, soft-delete recovery, and the external recovery boundary. `Read` this before editing backend Python or doing anything you might need to undo.

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
2. If the main shell is healthy, ask the partner to open Settings -> Server and click **"Restart server"** (POSTs `/api/admin/restart`).
3. If the main shell is broken, ask the partner to open Recovery from their managed deployment. A self-hosted operator runs `scripts/mobiusctl recovery` to open a root shell in the live app container and repairs `/data/platform` in place. Do not invent or link to an in-app `/recover` route; recovery is deliberately outside this container.
4. Restart takes ~5–15s; the page auto-reloads when healthy.
5. Verify the fix in the original chat (still open, full history intact).

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

## The external recovery boundary

Recovery does not run beside this agent.

**Managed deployments** create or wake a separate Serverless recovery service
inside the same deployment project — a fresh reasoning agent that inspects and
repairs all of `/data` through a private root target, or quarantines and reseeds
the platform clone when that is the correct diagnosis. This running agent never
receives that target's one-time token and cannot start, update, or modify it.

**Self-hosted operators** already have host and Docker root, so recovery is just
a root shell in the *live* app container — no isolated worker, credentials, or
downtime:

```sh
scripts/mobiusctl recovery
```

It runs `docker exec -u 0` in the running container. Repair `/data/platform` (or
anything under `/data`) in place, then restart in place (Settings -> Server, or
`docker restart`); the app is never stopped or recreated and the container
overlay is preserved. If the container is not running, start it first — boot
falls back to the baked floor when `/data/platform` is broken, so the normal
in-container agent returns to repair it — then re-run the command.

| Situation | Action |
|---|---|
| Backend edit, main shell healthy | Settings -> Server -> Restart |
| Main shell or backend broken (managed) | Partner opens the deployment's Recovery action |
| Self-hosted instance broken | Operator runs `scripts/mobiusctl recovery`, repairs `/data/platform` in place, restarts |

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
