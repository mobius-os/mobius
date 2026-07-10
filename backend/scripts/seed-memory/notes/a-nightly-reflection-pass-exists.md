---
title: A nightly Reflection pass exists
type: note
importance: 4
access_count: 0
last_accessed: null
tags: [platform]
mocs: [mobius-platform, maintaining-memory]
created: 2026-06-10
updated: 2026-06-10
---
Every night (default 06:00, configurable in the Reflection app) an unattended run
reviews the day's agent work, looks for system-improvement opportunities, fixes
small app issues when safe, and writes a morning brief.

Memory is separate: the **Memory** app owns scheduled graph consolidation and
writes compact maintenance records under `/data/shared/memory/update-log/`.
Reflection may read those records and propose improvements, but it does not own
the graph.

**Why it matters by day:** overnight changes to apps, skills, briefs, or the
Memory maintenance process are normal, not an intruder. Keep this chat's note
current; Memory's scheduled pass handles heavier graph work.

**How to apply:** if the partner mentions the brief or something that "changed
overnight," check `/data/cron-logs/reflection.log`, `/data/cron-logs/memory.log`,
the Memory update log, and the cron_outcome events before assuming a bug;
`git -C /data log` shows exactly what the passes changed.
