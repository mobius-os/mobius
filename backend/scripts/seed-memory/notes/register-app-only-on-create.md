---
title: Run register_app.py only on first create — edits auto-recompile
type: note
importance: 4
access_count: 0
last_accessed: null
tags: [apps, lifecycle, gotcha]
mocs: [building-mobius-apps]
created: 2026-06-02
updated: 2026-06-02
---
A file watcher recompiles `/data/apps/<slug>/index.jsx` ~1s after you save, so
editing an existing app needs no `register_app.py`. Re-running it creates a DUPLICATE
every time the name differs by a character (slug vs. title is the common slip).

**Why:** duplicate apps + wasted tool calls.

**How to apply:** `register_app.py` only for the initial create (to mint the id + DB
row). For edits, just write the file. If the partner says it didn't change, check
that `/data/compiled/app-<id>.js` mtime advanced and look for `compile failed for` in
`/data/logs/chat.log` — a JSX syntax error blocks the recompile. If a duplicate
appears, `DELETE /api/apps/<dup-id>`.
