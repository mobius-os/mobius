---
title: window.mobius.storage is the default persistence for every app
type: note
importance: 4
access_count: 0
last_accessed: null
tags: [apps, storage]
mocs: [building-mobius-apps]
created: 2026-06-02
updated: 2026-06-02
---
`window.mobius.storage` is injected into every mini-app before its module loads —
make it the default, not raw `fetch`. Reads are instant (read-through cache,
revalidated in background) and work offline (last-known value + pending writes);
writes queue and auto-sync when offline. `subscribe(path, cb)` gives reactive reads.

**Why:** raw `fetch('/api/storage/...')` inside an app has no offline queue/cache and
silently drops offline writes.

**How to apply:** `get/set/remove/subscribe/list`. Pass objects directly to `set`
(do NOT pre-stringify — see [[storage-json-no-envelope]]). Use raw `fetch` only
OUTSIDE an app (cron, agent) or for cross-app `shared/` files. To enumerate, use
[[storage-enumerate-dont-probe]].
