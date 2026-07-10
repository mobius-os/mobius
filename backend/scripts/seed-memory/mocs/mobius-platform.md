---
title: The Möbius platform
type: moc
tags: [platform]
---
# The Möbius platform

A map for operational facts about **this instance** — quirks of this
deployment, a recurring failure mode you hit here, an environment detail worth
remembering between sessions.

**Generic platform how-to does NOT live here.** The reusable operational
knowledge (shell rebuild needs a restart, backend edits need a host patch, cron
survives a rebuild via init-cron, SQLite needs a manual ALTER, `/data` is a git
repo, list dirs live with describe-tree) is now a **skill** under
`/data/shared/skills/` — that's where procedure that helps *any* instance
belongs. Add a note here only when the fact is specific to *this* deployment.

## Notes

- [[a-nightly-reflection-pass-exists]] — the overnight pass writes a morning
  brief, reviews Memory's maintenance log, and fixes safe app issues; overnight
  changes are normal.
