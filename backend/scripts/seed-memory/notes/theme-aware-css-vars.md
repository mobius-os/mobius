---
title: Use theme CSS variables, never hardcode structural colors
type: note
importance: 3
access_count: 0
last_accessed: null
tags: [apps, ui]
mocs: [building-mobius-apps]
created: 2026-06-02
updated: 2026-06-02
---
Structural colors (bg, text, borders, cards, inputs) must use CSS variables so the
app works in light and dark mode. Hardcoding `#0c0f14` instead of `var(--bg)` breaks
the app when the partner toggles modes.

**Why:** half the users' devices are in the mode you didn't test.

**How to apply:** `var(--bg)`, `--surface`, `--surface2`, `--text`, `--muted`,
`--accent`, `--accent-hover`, `--border`, `--danger`, `--green`, `--font`, `--mono`.
Hardcoded colors are fine only for app-specific accents (a brand color, a chart
series). Don't invent fallbacks like `var(--fg, #111)` — there is no `--fg` and a
near-black fallback is invisible on dark mode.
