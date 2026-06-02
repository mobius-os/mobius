---
title: offline_capable caches the app's CODE, not its data
type: note
importance: 3
access_count: 0
last_accessed: null
tags: [apps, offline, gotcha]
mocs: [building-mobius-apps]
created: 2026-06-02
updated: 2026-06-02
---
Storage already works offline for every app via [[window-mobius-storage-is-default]].
`offline_capable: true` is a SEPARATE flag that caches the app's frame + module so the
app itself RUNS with no network.

**Why:** setting it on a network-dependent app caches empty/stale state and the app
looks broken offline.

**How to apply:** set `offline_capable` only for apps that genuinely work offline
(notes, a tracker, a game). Leave network-dependent apps at the default — they still
get a branded offline screen, never the browser error page.
