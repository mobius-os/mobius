---
title: /data is a git repo — commit agent-owned state with pm-commit
type: note
importance: 3
access_count: 0
last_accessed: null
tags: [platform, git]
mocs: [mobius-platform]
created: 2026-06-02
updated: 2026-06-02
---
`/data/` is a git repo initialized on first boot. After substantial changes, commit
so undo is clean: `pm-commit 'one-line what and why'` — it stages, unstages a
runtime-state denylist (profiles, compiled, logs, generated), then commits; it refuses
(exit 2) if >50 files stage after filtering.

**Why:** agent-owned state (apps, shell, memory graph, theme) is recoverable via git
when something breaks.

**How to apply:** `pm-commit 'msg'`; re-run with `--allow-broad` only after confirming
the staged set is what you meant. The memory graph under `shared/memory/` is tracked
here, so its history is your undo for a bad consolidation.
