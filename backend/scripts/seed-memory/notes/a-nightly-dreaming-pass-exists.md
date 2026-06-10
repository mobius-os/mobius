---
title: A nightly Dreaming pass exists
type: note
importance: 4
access_count: 0
last_accessed: null
tags: [platform]
mocs: [mobius-platform, maintaining-memory]
created: 2026-06-10
updated: 2026-06-10
---
Every night (default 06:00, configurable in the Dreaming app) an unattended run
interviews the day's chats, consolidates this graph (drains inbox.md, merges,
prunes), fixes apps, writes a morning brief.

**Why it matters by day:** you can defer heavy memory reorganizing — a one-line
inbox append IS enough, the night shift finishes the job; overnight changes to
apps/skills/graph are normal, not an intruder.

**How to apply:** if the partner mentions the brief or something that "changed
overnight," check `/data/cron-logs/dreaming.log` and the cron_outcome events
before assuming a bug; `git -C /data log` shows exactly what the pass changed.
