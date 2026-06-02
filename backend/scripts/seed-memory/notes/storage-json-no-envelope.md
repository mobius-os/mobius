---
title: Save the bare object to .json storage paths — no envelope
type: note
importance: 5
access_count: 0
last_accessed: null
tags: [apps, storage, gotcha]
mocs: [building-mobius-apps]
created: 2026-06-02
updated: 2026-06-02
---
For `.json` storage paths the body IS the document. `PUT /api/storage/apps/{id}/notes.json`
with `{"items":[1,2,3]}` stores exactly that. The envelope form
`{content: JSON.stringify(data)}` is NOT unwrapped for `.json` — the server stores
the envelope literally, the app loads back `{content:"..."}` instead of its data,
falls through to empty state, and the next save overwrites real data with empty.

**Why:** silent data loss that looks like "the app forgot everything".

**How to apply:** `.json` → write `body: JSON.stringify(data)`, read `await res.json()`.
Non-`.json` (markdown, css, html) → use the `{content: "..."}` envelope. Prefer
[[window-mobius-storage-is-default]], which handles this for you (pass the object,
it stringifies).
