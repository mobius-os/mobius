---
title: Maintaining memory
type: moc
tags: [meta]
---
# Maintaining memory

How this knowledge graph works and how you grow it. The full mechanical rules
live in the **knowledge-graph skill** at `/app/skill/knowledge-graph-skill.md`
— `Read` it before you split, merge, or reorganize notes.

## The system

- [[how-the-memory-graph-works]] — the format, the inbox, and the daily vs.
  nightly split in one note.

## The short version

- **Record** durable, future-useful facts (user preferences, hard-won bugs,
  platform contracts) — not everything. Default to recording nothing.
- **Day-to-day**, append a quick line to `inbox.md`; the nightly dreaming pass
  consolidates it into proper notes. When you already know the clean fact,
  write the note directly under `notes/` and link it into a map.
- **One idea per note.** Title it as the specific claim. Link every note into
  at least one map ([[index]] → maps → notes). No orphans.
- Files live in git; `describe-tree`-style discovery beats hardcoded lists, so
  [[describe-tree-over-hardcoded-lists]] applies to the graph too.
