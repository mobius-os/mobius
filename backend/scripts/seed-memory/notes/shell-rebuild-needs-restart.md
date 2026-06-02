---
title: rebuild_shell.sh doesn't live-reload — uvicorn serves the old bundle until restart
type: note
importance: 3
access_count: 0
last_accessed: null
tags: [platform, deploy, gotcha]
mocs: [mobius-platform]
created: 2026-06-02
updated: 2026-06-02
---
`main.py` picks the static dir at module load, not per request. After
`bash /app/scripts/rebuild_shell.sh` writes a fresh `/data/shell/dist/`, the running
uvicorn keeps serving the old bundle until the process restarts.

**Why:** you'll claim "shell rebuilt" while the partner still sees the old UI.

**How to apply:** tell the partner the shell updates after the next container restart,
or have them click Restart in the recovery chat. For CSS-only changes, prefer
`/data/shared/theme.css` (hot-reloaded, no rebuild).
