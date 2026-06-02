---
title: List directories live with describe-tree; docstring every new file
type: note
importance: 3
access_count: 0
last_accessed: null
tags: [platform, convention]
mocs: [mobius-platform, maintaining-memory]
created: 2026-06-02
updated: 2026-06-02
---
Hand-written file tables go stale the moment a file is renamed and send you on
dead-end searches. `python3 /app/scripts/describe-tree.py <dir> --depth 1 --quiet`
prints `filename — first-sentence-of-docstring` for each file, always matching reality.

**Why:** a stale hardcoded list caused a real downstream bug (a claimed file that no
longer existed).

**How to apply:** use describe-tree instead of trusting any hardcoded list (including
ones in your memory). Start every NEW file with a one-sentence docstring/top-comment so
the next reader sees what it does without opening it — Python `"""..."""`, JSX
`/* ... */`, shell `#`, CSS `/* ... */`.
