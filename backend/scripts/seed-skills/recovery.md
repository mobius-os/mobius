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

It stages, unstages a runtime-state denylist (profiles, compiled, logs, generated), then commits; it refuses (exit 2) if >50 files stage after filtering. Re-run with `--allow-broad` only after confirming the staged set is what you meant. The memory graph under `shared/memory/` is tracked here, so its history is your undo for a bad consolidation.

---

## The recovery surface

If you break a live copy, the partner recovers via `/recover` or a fresh you in the recovery chat at `/recover/chat`. The recovery chat runs its own minimal stack (separate auth, separate runner, separate per-chat storage at `/data/recovery/chats/<chat_id>.jsonl`) so it stays reachable when production chat code is broken. The partner can start multiple recovery chats with different providers (Claude or Codex). From the `/recover` dashboard they can click "Restore backend" / "Restore shell" / "Restore scripts" to copy the immutable baked source (`/app/app-baked/`, `/app/shell-src/`, `/app/scripts-baked/`) back over the live copy.

| Situation | URL | Action |
|---|---|---|
| Backend edit, main shell healthy | Settings -> Server | Click "Restart server" |
| Backend edit, main shell broken | `/recover/chat` | Click "Restart server" |
| Agent stuck or unable to fix | `/recover` | Click "Restore backend" / "Restore shell" / "Restore scripts" |
| Lost ability to log in to main shell | `/recover` | Log in (owner password), then options above |

---

## Chat recovery

Deleted chats remain in the system for **7 days** and can be recovered:

```bash
curl -s -X POST "$API_BASE_URL/api/chats/{chat_id}/recover" -H "Authorization: Bearer $AGENT_TOKEN"
```

Tell the partner about this safety net if they accidentally delete a chat. **Apps cannot be recovered after deletion** — always confirm before deleting one (see `building-apps.md`).

---

## File locations

- **Uploaded files:** `/data/chats/{chat_id}/uploads/`
- **Generated images:** `/data/chats/{chat_id}/generated/`
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
