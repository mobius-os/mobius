---
title: Enumerate storage children; never brute-force filenames
type: note
importance: 4
access_count: 0
last_accessed: null
tags: [apps, storage, gotcha]
mocs: [building-mobius-apps]
created: 2026-06-02
updated: 2026-06-02
---
There is no `HEAD` on storage (it 405s). GET-probing guessed paths (e.g.
`reports/<date>.html` for the last 30 days) is the anti-pattern that shipped an app
showing empty in prod.

**Why:** you can't know what an app stored by guessing; you enumerate.

**How to apply:** `await window.mobius.storage.list('prefix/')` (inside an app) or
`GET /api/storage/apps-list/{appId}/{prefix}` / `GET /api/storage/shared-list/{prefix}`
(cron/agent). Returns `{entries:[{name,path,type,size,modified_at,mime_type}], next_cursor}`.
`list()` is offline-capable like `get()`: it falls back to the read-through cache
when the server is unreachable, overlaid with the outbox (read-your-writes).
Offline entries omit `size`/`modified_at` (server-only).
