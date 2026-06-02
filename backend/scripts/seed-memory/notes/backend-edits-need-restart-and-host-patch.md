---
title: Backend edits live in the writable layer — restart to load, patch host to persist
type: note
importance: 3
access_count: 0
last_accessed: null
tags: [platform, backend, gotcha]
mocs: [mobius-platform]
created: 2026-06-02
updated: 2026-06-02
---
Edits to `/app/app/*.py` take effect on the next uvicorn restart and survive
container restarts BUT are wiped by `docker compose up --build` (a rebuild restores the
image's baked code). A bad import kills uvicorn at boot — everything except `/recover`
goes down.

**Why:** a one-off container fix silently reverts on the next deploy; a syntax error
takes the whole app down.

**How to apply:** `python3 -m py_compile <file>` before asking for a restart (a failing
compile proves you'll break boot). To make a backend change permanent, the partner must
also patch the host repo and commit. All chat-persistence writes must route through the
`chat_writer` actor — never assign `Chat.messages`/`pending_messages` directly.
