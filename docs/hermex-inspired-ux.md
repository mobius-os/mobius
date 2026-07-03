# Hermex-inspired UI/UX ideas for Möbius

**Living doc — keep updating it.** Findings and concrete improvement ideas for
Möbius's interface, drawn from a review of [Hermex](https://github.com/uzairansaruzi/hermex),
a native SwiftUI iPhone client for the self-hosted [hermes-webui](https://github.com/nesquena/hermes-webui)
agent. Hermex and Möbius solve the same shape of problem — a mobile control
surface in front of a coding agent that lives on your own machine — so its
choices are unusually transferable.

When you find another idea worth stealing (or ship one of these), **add/append
here** rather than letting it live only in a chat. Newest learnings go in the
Changelog at the bottom; the body stays organized by area.

> **Voice/scope note:** everything here is *inspired by* Hermex but must land in
> Möbius's own visual language — theme tokens (`--surface`/`--accent`/`--muted`/…),
> the `mobius-ui:` component shapes, 44px touch targets, scoped CSS. Port the
> *idea and information architecture*, not the iOS chrome.

Last updated: 2026-07-03.

---

## Status legend

- 🟢 **Shipped** — done, in a Möbius app/shell.
- 🟡 **In progress** — partially built.
- ⚪ **Idea** — not started; effort estimate is rough.

Effort: **S** (a few hours) · **M** (a day) · **L** (multi-day / touches a subsystem).

---

## 0. The one-paragraph aesthetic to internalize

Hermex reads as "dense, calm, operator-grade" because of a single discipline:
**structure is monochrome; color is reserved exclusively for semantic signal.**
Surfaces are a 3–4 step neutral ramp (`bg → surface → surface2`), separators are
0.5px hairlines, cards are 12px-radius rounded rects, and **anything that is a
path, count, branch, or code is monospaced**. Hue (green/amber/red/blue) appears
*only* where it means something (git status, task urgency). That restraint is the
whole trick — Möbius already has the tokens for it (`--green`, `--danger`, `--mono`);
the lever is using them with the same discipline instead of accenting everything
purple.

---

## 1. Chat & streaming rendering — the biggest visual lever

Möbius's chat is the persistent control surface, so upgrades here have the widest
reach. Hermex's transcript is the most sophisticated part of that app.

### 1.1 ⚪ **L** — The three-layer streaming reveal (signature feel)
Hermex's streaming *feels* premium because three mechanisms stack, where Möbius
today does one (a ~3-char/frame typewriter in `useStreamConnection.js`):
1. **Word-cadence drain** — reveal one word per ~48ms, but scale the quota up
   when backlog would exceed a ~1s max-lag so display catches up to the live
   stream. Split on grapheme clusters so emoji never break; guarantee
   `head + tail === text` so pacing can never alter final content.
2. **Per-glyph fade-in** — each new glyph fades 0→1 over ~350ms on a quadratic
   ease, staggered ~12ms apart in reading order, capped ~450ms ahead of the
   clock. Walk the *resolved* layout so words are already at final wrapped
   positions and never reflow mid-fade. Pre-existing text never re-fades.
3. **Markdown block sealing** — once a prefix passes a safe boundary (blank line,
   heading, closed code fence), seal it as static and stop re-parsing; only the
   active tail block re-renders.
- **Möbius today:** `frontend/src/hooks/useStreamConnection.js` (typewriter drain)
  + block-memoized markdown (`BlockRenderer`) already does layer 3's spirit.
- **Suggested:** add layer 1 (word cadence + catch-up quota) and a CSS-only
  version of layer 2 (wrap new glyphs in spans, rAF sets opacity). This is the
  single highest-impact polish for the whole product.
- Hermex refs: `StreamingWordDrain.swift`, `StreamingTextFade*.swift`,
  `StreamingMarkdownSupport.swift`.

### 1.2 ⚪ **M** — Turn-anchored tool grouping (their "subagent" answer)
Hermex has no literal subagent UI. Instead every tool call sharing a
`turn:user:N` key **coalesces into one "Activity: N tools" card per turn**, with
multi-source dedup (stable-id → argument fingerprint → name-ordinal) so the live
SSE stream and the settled transcript reconcile without cards flashing.
- **Möbius today:** `ToolBlock` renders tool calls; grouping is looser.
- **Suggested:** group a turn's tool calls under one collapsible "Activity" card
  with a summarized header (`terminal ×3, patch ×2 +1`). If Möbius ever renders
  nested agent work, this is the pattern to reuse.

### 1.3 ⚪ **S** — Collapsible tri-state cards on one shared surface
Thinking / tool / marker / file-change blocks all share ONE glass "timeline
accessory surface" and ONE disclosure motion (`.easeInOut 0.18s`), with a
tri-state expand (user toggle overrides an app default).
- **Suggested:** unify Möbius's thinking/tool/compaction blocks onto a single
  `mobius-ui:AccessoryCard` shape (tinted `--surface2`, hairline stroke, rotating
  chevron), so they read as a family.

### 1.4 ⚪ **M** — Tolerant tool-result formatting
Hermex parses a tool result as JSON, unwraps **terminal envelopes**
(`stdout`/`stderr`/`exit_code`) for shell tools vs **common envelopes**
(`result`/`content`/`text`/`summary`), unescapes nested JSON-in-strings up to 3
levels, and renders monospaced only when it's terminal/multiline.
- **Suggested:** give `ToolBlock` the same envelope-aware formatter so bash output
  reads as a terminal and structured results read as clean key/values, instead of
  a raw JSON dump.
- Hermex ref: `ToolCallDisplayFormatter`.

### 1.5 ⚪ **M** — Context-window ring + clarification/approval cards
- A 30px **circular progress ring** showing context-window %, tapping to a
  tokens/cost popover (`ContextWindowIndicatorView.swift`).
- **Clarification cards** with choice buttons + a live **expiration countdown**
  (a shrinking capsule bar). Möbius already intercepts `AskUserQuestion`
  (`QuestionCard`) — adding a countdown + the ring are small, high-signal touches.

### 1.6 ✅ note — Reconnection
Hermex uses a full state machine with `Last-Event-ID` **replay-after-seq** and a
generation counter to prevent double-finalize. **Möbius's catch-up-burst +
`isStreamingRef` gate is simpler and already solid** — the only bits worth
borrowing are the *generation guard* (so a concurrent completion can't
double-finalize) and replay-after-seq instead of a full re-burst. Low priority.

---

## 2. Editor / file-browser app

The Möbius **Editor** (catalog app `mobius-os/app-editor`) is already a real code
editor + file tree + git panel + embedded chat. Hermex's read-only Workspace has
a more operator-grade *git* surface worth borrowing from.

### 2.1 🟢 **S** — Semantic git status chips + file cards *(shipped 2026-07-03)*
Replaced the git panel's uniform dot + path + "M" with Hermex's **GitFileCard**:
bold basename over a dimmed monospaced parent dir, plus a **semantic status chip**
(green = staged/added, amber = modified, blue = new/renamed, red = deleted), and
matching colors on the summary-bar counts (previously everything was the purple
accent). `theme.js` + `ui/GitPanel.jsx`, `+67/−28`. Pushed to `app-editor`.

### 2.2 ⚪ **L** — Inline diff viewer (the single best Hermex component)
Today tapping a changed file just opens it. Hermex renders a real unified diff:
tinted rows (`add rgba(51,199,89,.16)`, `del rgba(242,64,64,.16)`), a **slightly
darker line-number gutter** (.24 vs .16 — a subtle depth cue), 48px right-aligned
numbers, ~11px mono, **dimmed context / solid changed lines** so the eye lands on
changes, and **collapsible hunks** with humanized labels ("Lines 42–58") + a
per-hunk `+N −M`. Full-width rows pinned to viewport width under horizontal
scroll so short and long lines both look right.
- **Needs:** a git-diff endpoint in `app-editor`'s storage/git API.
- Hermex ref: `GitDiffView.swift` (highest-value file in that repo).

### 2.3 ⚪ **M** — Git status in the file tree
Hermex shows a file's git state inline in the browser. Möbius's tree doesn't
cross-reference the open repo's status. **Suggested:** a subtle status color /
left-edge tint on tree rows that are modified/new, so you see what changed without
opening the panel. (Watch nested-repo boundaries — `/data`, `/data/shell`,
`/data/platform` are separate repos.)

### 2.4 ⚪ **S** — The `+N −M` counter as a shared component
A green `+`, red `−`, monospaced tabular-digit counter (with a real `−`, U+2212)
reused in the tree, cards, headers, and diffs. Cheap, high-signal, very "operator."

### 2.5 ⚪ **M** — Breadcrumb path bar
Hermex's browser is a **single-level re-navigating list** with a horizontal-scroll
breadcrumb (`>` separators, Home/Up buttons, the current crumb inert as a free
"you are here") and a **middle-truncated monospaced current-path label**. Möbius
uses a nested tree (fine for an editor) but could add the breadcrumb + middle
truncation for deep paths. The "distinct empty vs no-search-match states" (icon +
copy both change) is a nice thoughtful touch to copy.

---

## 3. Tasks & Skills apps — shipped, with room to grow

### 3.1 🟢 **M** — Tasks app *(shipped 2026-07-03; catalog app `mobius-os/app-tasks`)*
Viewer for the agent's scheduled check-ins (`/data/shared/self-reminders.jsonl`).
Amber **"Needs attention"** summary pill (Hermex's derived "silently-failed job"
status), status badges, due/created meta grid, smart sort (attention → soonest →
done). Reschedule/done/cancel route to the agent chat (scheduling is owner-only).
- **Future ⚪:** when a cron/self-reminders read surface is exposed to app tokens,
  add recurring-cron jobs (Hermex shows `schedule_display`, next/last run, deliver
  target, model). A "Running now" live pill needs a status endpoint an app can hit.

### 3.2 🟢 **S** — Skills app *(shipped 2026-07-03; catalog app `mobius-os/app-skills`)*
Searchable read-only browser for `/data/shared/skills/*.md`; tap → SKILL.md as
markdown (`marked` + `dompurify`). Create/edit routes to the agent chat.
- **Future ⚪:** Hermex exposes skills as `/slash` commands in chat too. A Möbius
  command-palette entry that lists/searches skills and drops `/<skill>` into the
  composer would mirror that dual surface. Category grouping is a no-op today
  (skill files have no frontmatter categories) — add it if skills ever gain them.

---

## 4. Cross-cutting design language

- ⚪ **S — Monochrome + semantic-color discipline (§0).** Audit the shell/apps for
  places that use `--accent` decoratively where a neutral would read calmer, and
  reserve hue for meaning. Biggest "operator-grade" upgrade for the least code.
- ⚪ **S — Mono for identifiers.** Ensure every path/count/branch/id/code token
  uses `--mono` (Hermex's `AppFont` discipline). Many feel "off" only because a
  filename is set in the prose font.
- ⚪ **S — Glass with a real fallback.** For any translucent surface, pair
  `backdrop-filter: blur()` with a solid neutral fallback under
  `prefers-reduced-transparency`/low-contrast, always with a 1px hairline. Keep
  transitions ~0.18s ease-in-out.
- ⚪ **S — Three-state empties everywhere.** loading / error-with-retry /
  empty-vs-no-match, each with its own icon + copy. Already the norm in the new
  Tasks/Skills apps; make it the shared `mobius-ui:Empty` default.
- ⚪ **S — Semantic haptics** on mobile (message sent → light, done → success,
  destructive → warning), gated by a setting. Web analog: subtle press/transition
  feedback.

---

## 5. Platform findings & bugs (surfaced while building)

### 5.1 🟡 **S** — Watcher recompile doesn't bump `updated_at` → warm PWAs serve stale modules
`app_watcher._recompile` → `compiler.recompile_app_bundle` writes the new
`compiled_path` + `jsx_source` and commits, but **never sets `app.updated_at`**.
Since `/api/apps/{id}/module` (and `/frame`) ETags are derived from `updated_at`,
a warm browser that already has the module sends `If-None-Match: <old-etag>`, the
server computes the same ETag, returns **304**, and the browser serves its cached
*old* bundle. The `app_updated` event remounts the iframe, but the refetch 304s to
the stale copy. Masked in dev because a fresh headless browser has no prior ETag.
- **Repro:** edit a mini-app source → confirm `/data/compiled/app-<id>.js` mtime
  advanced but `apps.updated_at` did not → a warm client keeps the old UI.
- **Fix:** set `app.updated_at = timeutil.now_naive_utc()` before the `db.commit()`
  in `recompile_app_bundle` (safe: iframe remounts are already driven by the
  `app_updated` event, not by `updated_at`, so this only corrects the ETag). Same
  class as the `three`/frame-ETag staleness already in memory.
- **Status:** ✅ fixed on `main` (d38d758), regression test in `test_frame_etag.py`.

### 5.2 🟢 **M** — Dynamic web app-store registry *(shipped 2026-07-03)*
The App Store's catalog was a hardcoded `CATALOG` array in `app-store/constants.js`
— adding an app meant editing + bumping + republishing the store app, and every
instance had to *update the store* before the new app appeared. Now the store
fetches `mobius-os/app-store/main/catalog.json` at mount (via the server proxy),
validates it, and uses it as the catalog source, **falling back to the baked
`CATALOG` only if the fetch fails**. So publishing an app is: create
`mobius-os/app-<name>` (manifest + `index.jsx` + `icon.png`) → append an entry to
`catalog.json` → it appears in every instance's store on next open, no store
redeploy. `api.js:fetchCatalog` does the shape/https validation; the trusted-host
warning + backend SSRF defenses remain the security boundary. Store 1.8.0.

---

## Changelog

- **2026-07-03** — Doc created. Reviewed Hermex chat rendering, Tasks, Skills, and
  Workspace. Shipped: Tasks + Skills as **catalog apps** (`mobius-os/app-tasks`,
  `app-skills`), Editor git-chip polish (§2.1, to `app-editor`), the watcher
  `updated_at`/ETag fix (§5.1, on `main`), and a **dynamic web app-store registry**
  (§5.2, `catalog.json`). Everything else here is a logged idea to pick up later.
