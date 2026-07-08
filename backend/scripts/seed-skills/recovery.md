# Recovery, backend edits, and data layout

How backend edits load (restart, not live-reload), how to make them permanent, the SQLite migration gotcha, `/data`-as-a-git-repo, file locations, chat recovery, and the recovery surface. `Read` this before editing backend Python or doing anything you might need to undo.

---

## Backend edits — restart to load, hand off persistence

Edits to `/app/app/*.py` take effect on the **next uvicorn restart** and survive container restarts, BUT are wiped by `docker compose up --build` (a rebuild restores the image's baked code). Two failure modes to respect:

- **A bad import kills uvicorn at boot** — everything except `/recover` goes down. Always `python3 -m py_compile <file>` before asking for a restart; a failing compile proves you'll break boot.
- **A container-only fix silently reverts on the next deploy.** Möbius can make the local live fix, but host-repo/release work is a handoff outside the in-product agent. If unsure, ask whether the fix is a one-off local change or needs outside persistence work. Do not push, publish, or manage external repo workflow from inside Möbius.

All chat-persistence writes must route through the `chat_writer` actor — never assign `Chat.messages` / `Chat.pending_messages` directly (see core.md's write-surface section for why).

### The backend-fix loop

1. Edit `/app/app/...py` in place; `py_compile` it.
2. If the main shell is healthy, ask the partner to open Settings -> Server and click **"Restart server"** (POSTs `/api/admin/restart`).
3. If the main shell is broken, ask the partner to **open `/recover/chat` in a new browser tab** (they stay in your current chat — your session survives the restart). That chat may prompt for login: it uses the **same owner password** as the main shell, just behind a separate form. In that tab they click **"Restart server"** (POSTs `/recover/restart`, SIGTERMs uvicorn, container restarts).
4. Restart takes ~5–15s; the page auto-reloads when healthy.
4. Verify the fix in the original chat (still open, full history intact).

---

## SQLite — `create_all` never ALTERs; new columns need a manual ALTER

SQLAlchemy `create_all` only CREATEs missing tables; it never adds a column to an existing one. A new model field won't appear on an existing `/data/db/ultimate.db` — the column is silently missing in prod and queries fail or read NULL. When you add a model field, run a manual `ALTER TABLE <t> ADD COLUMN <c> ...` against the existing DB, or ship a tiny migration step.

---

## `/data` is a git repo — commit agent-owned state

`/data/` is a git repo initialized on first boot. After substantial changes (apps, shell, memory graph, theme), commit so undo is clean:

```bash
pm-commit 'one-line what and why'
```

It stages, unstages a runtime-state denylist (profiles, compiled, logs, generated), then commits; it refuses (exit 2) if >50 files stage after filtering. Re-run with `--allow-broad` only after confirming the staged set is what you meant. The memory graph under `shared/memory/` and your editable skills under `shared/skills/` are tracked here, so this history is your undo for a bad consolidation or a skill edit you regret. The nightly Reflection pass also takes a pre-run snapshot before it touches anything.

To actually roll one back, find the commit that last had the good version and restore just that path:

```bash
git -C /data log --oneline -- shared/memory/notes/<slug>.md   # find the good <sha>
git -C /data checkout <sha> -- shared/memory/notes/<slug>.md   # restore just that file
```

---

## The recovery surface

If you break a live copy, the partner recovers via the `/recover` dashboard, or a fresh you in the recovery chat at `/recover/chat`. The recovery chat runs its own minimal stack (separate auth, separate runner, separate per-chat storage at `/data/recovery/chats/<chat_id>.jsonl`, stdlib-only — no shared code with the production chat path) so it stays reachable when production chat code is broken. The partner can start multiple recovery chats with different providers (Claude or Codex).

**There is no "Restore backend/shell/scripts" button** on the `/recover` dashboard. The dashboard has four actions:

1. **Run Recovery Agent** (`/recover/chat`) — a fresh you with **filesystem write access via Bash** (but no `$AGENT_TOKEN`, no `$API_BASE_URL` — production API plumbing may be broken, so it does NOT call `/api/...`). It edits the backend, frontend, platform clone, and `/data` in place to fix the instance; recovery's own code is a read-only mount it can't touch. This is the primary repair path: it diagnoses, edits the live code, and runs a restore itself when needed.
2. **Restore platform** — recommended. Reverts *uncommitted* platform edits and restarts. Deterministic, offline, no agent or network needed.
3. **Reset to baked floor** — last resort. Wipes uncommitted platform edits, recopies the baked image's code, and restarts.
4. **Update Recovery** — shown only when a newer recovery release exists. A root-owned, integrity-checked pull + restart; your chats and data are untouched.

**The dashboard's "Restore platform" and "Reset to baked floor" buttons run those restores for you; the recovery-chat agent can also run any restore mode itself.** Inside `/recover/chat`, a fresh you restores the immutable baked source by running the restore script with Bash:

```sh
sh /app/scripts/recovery_restore.sh <mode>
```

Modes (run with no argument to print what each does):

| Mode | What it restores |
|---|---|
| `shell-dist` | Prebuilt frontend bundle (`/app/static/` -> `/data/shell/dist/`). Fast, no rebuild. **Legacy:** `shell-dist`/`shell-src` repair the non-served `/data/shell` tree, NOT the served `/data/platform/frontend/dist` — use `platform`/`platform-baked` to repair the running instance. |
| `shell-src` | Editable frontend source (`/app/shell-src/` -> `/data/shell`). Wipes your `src/` edits; needs a rebuild to take visual effect. |
| `platform` | `git -C /data/platform reset --hard HEAD` — reverts *uncommitted* platform edits; commits are kept. Fast; no image needed. |
| `platform-baked` | Full wipe + recopy of the SERVED clone tree `/data/platform/backend/{app,scripts}` from the baked floor, then commits the restore to `/data/platform` git history. Use when a bad change was already committed, or a git reset isn't enough. |

The backend served by uvicorn is the `/data/platform` clone, so `platform` and `platform-baked` are the modes that repair the running backend. (The script does NOT accept `backend`/`scripts` modes — they exit "Unknown mode"; the only legacy modes it still accepts are `shell-dist`/`shell-src` above, for the non-served `/data/shell` tree — so reach for `platform`/`platform-baked` here.)

After a `platform` or `platform-baked` restore, tell the partner to click **"Restart server"** at the top of the recovery chat page so uvicorn reloads the restored code.

| Situation | URL | Action |
|---|---|---|
| Backend edit, main shell healthy | Settings -> Server | Click "Restart server" |
| Backend edit, main shell broken | `/recover/chat` | Click "Restart server" |
| Agent stuck or unable to fix in place | `/recover/chat` | A fresh you runs `recovery_restore.sh <mode>`, then partner clicks "Restart server" |
| Bad uncommitted platform edit broke the server | `/recover` | Click "Restore platform" |
| Committed a bad change, or a reset wasn't enough | `/recover` | Click "Reset to baked floor" |
| A newer recovery release is available | `/recover` | Click "Update Recovery" |
| Lost ability to log in to main shell | `/recover` | Log in (owner password), then the options above |

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
- **Chat media (screenshots + generated):** `/data/chats/{chat_id}/media/` — the old `generated/` path is still served for embeds in pre-`media/` messages.
- **Per-app storage (numeric id):** `/data/apps/{app_id}/<path>` — what `PUT /api/storage/apps/{app_id}/...` writes to, keyed by the numeric DB id.
- **Per-app source (slug):** `/data/apps/{slug}/` — where app source lives, keyed by slug. `index.jsx` is the entrypoint and can import sibling `.js`, `.jsx`, `.ts`, or `.tsx` modules. NOT the same dir as storage; the slug tree and the numeric-id tree are separate.
- **Shared storage (cross-app):** `/data/shared/<path>` — what `PUT /api/storage/shared/...` writes to; used for theme.css, agent-settings.json, the memory graph, etc.
- **Compiled bundles:** `/data/compiled/app-{app_id}.js`.
- **Cron logs:** `/data/cron-logs/`. **Service token:** `/data/service-token.txt` (chmod 600).

Chat files are purged when the chat is permanently deleted (after 7 days). For data that should outlive a chat, use per-app or shared storage.

---

## Viewing apps directly (debugging)

To check an app's rendered output, use the preview helper — it loads the app inside the authenticated Möbius shell, the realistic path the partner takes:

```bash
bash "$SCRIPTS_DIR/preview_app.sh" <id>
```

The frame URL (`$API_BASE_URL/api/apps/<id>/frame`) is stable per-app (ETag + browser cache handles freshness, no `?v=`), but the frame waits for a parent-shell `moebius:frame-init` postMessage — opening it standalone just shows "Loading timeout." Always go through the preview helper or the live shell.
