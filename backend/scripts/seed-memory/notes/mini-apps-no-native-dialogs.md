---
title: Mini-apps can't use window.confirm/alert/prompt
type: note
importance: 3
access_count: 0
last_accessed: null
tags: [apps, ui, gotcha]
mocs: [building-mobius-apps]
created: 2026-06-02
updated: 2026-06-02
---
The mini-app sandbox excludes `allow-modals`, so native `confirm/alert/prompt`
silently no-op and `confirm` returns `false`.

**Why:** a delete confirm that always returns false (or a prompt that returns null)
looks like a broken feature.

**How to apply:** build in-app modal components for confirmations and inputs (see the
app-store `ConfirmModal` pattern). Theme them with [[theme-aware-css-vars]].
