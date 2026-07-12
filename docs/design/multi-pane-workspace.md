# Multi-pane workspace (design note — NOT built)

Status: **design only.** The shipped feature is a single tab strip that swaps
one on-screen view (card 220). This note captures the target the tabs were
shaped toward — build several apps at once, chat with several agents, in tiled
panes — so the next agent inherits the plan and the constraints instead of
re-deriving them. Build it when it is the actual task, not before (Möbius:
duplicate now, harvest later).

## Vision

Tabs move out of a single strip into a **layout** the owner arranges. Drag a
tab out of the strip to tile it into a second pane; keep a chat pane on the
left and an app preview on the right; run two build chats side by side. On a
phone it degrades to swap-only tabs; the tiling pays off on web/desktop.

## What already exists (the seam)

`frontend/src/components/Shell/tabModel.js` is the openable-item primitive: a
tab is `{ kind: 'chat' | 'app', id: string }`, and the module owns dedup+cap,
persistence, the nav mapping (`tabNavTarget`), identity (`tabKey`, `sameTab`),
and active-ness (`isTabActive`). It is dependency-free. Its construction,
dedup, cap, `tabNavTarget`, `tabKey`, and `sameTab` carry over to panes
verbatim. The one today-shaped helper is `isTabActive(tab, view)`: it maps the
single global `{ view, chatId, appId }` nav focus to a tab. A pane instead
stores its focus as `activeTabKey` (below), so per-pane active-ness is just
`tabKey(tab) === pane.activeTabKey` — collapse `isTabActive` into that (a
`sameTab`/`tabKey` compare) when the pane model lands.

Today `Shell.jsx` keeps ONE `openTabs` set and renders ONE active view (the
`activeView`/`activeChatId`/`activeAppId` triple) plus the hidden app-iframe
LRU. That is the degenerate 1-pane case of the model below.

## Target model

- **Workspace** = a `layout` tree + a set of `panes`.
- **Pane** = `{ id, tabs: Tab[], activeTabKey }` — its own open set (from
  tabModel) and its own focused tab. The current shell is `panes: [pane0]` with
  `pane0.tabs = openTabs`.
- **Layout** = a small binary tree of splits: `{ dir: 'row' | 'col', a, b,
  ratio }` with panes at the leaves. A single pane is the trivial leaf.
- **Focus** = which pane is active (receives keyboard, the "current" back
  target).

`paneModel.js` (a sibling of tabModel) owns pane/layout operations: split a
pane, move a tab between panes, close a pane, resize a split. Rendering walks
the layout tree and renders each leaf pane's active tab into its own box.

## Migration path (each step localized)

1. Introduce `paneModel.js` + a `workspace` reducer in Shell. Seed it as one
   pane whose tabs are today's `openTabs`. No visual change yet.
2. Render the layout tree instead of the single `<main>`. With one pane it is
   the same output. This is the step that unlocks two panes.
3. Drag-to-tile: dropping a tab on a pane edge splits that pane and moves the
   tab (paneModel op). The strip's drag source already has `tabKey`.
4. Per-pane chat/app rendering: today only the active chat's ChatView mounts;
   a workspace mounts one ChatView per chat-pane. Honor the constraints below.

## Hard constraints (learned the expensive way — 2026-07-12 review)

Multiple live chat panes re-hit the exact machinery that sank the first split
view. Do not rediscover these:

1. **Never remount a ChatView to re-measure.** The spacer's grow-only
   `fullViewHRef` must re-measure when a pane's height changes, but a remount
   (folding size into the React key) also resets `spacerActive` — collapsing
   the send-reservation (Chat-UX #1) — and freezes a live `FOLLOW_BOTTOM`,
   stopping the pane from following its stream. Instead: drive pane size by CSS
   and add an **imperative `fullViewHRef` reset** signal into `useScrollMode`
   (a sanctioned re-measure that keeps mode + spacerActive intact). Each chat
   pane owns its own grow-only `fullViewHRef`; the keyboard shrinks panes
   independently.
2. **App-iframe LRU: never reparent keyed children; cap is real.** N visible
   app panes = N live iframes; the LRU cap (`APP_CACHE_MAX`, currently 4)
   bounds total mounted apps, and the render order must stay id-sorted so a
   sandboxed iframe is never reparented (reparent = reload → 10s loading
   timeout). A workspace that pins apps in several panes needs the cap and the
   stable-order invariant to span the whole workspace, not one strip.
3. **App ids stay numeric for nav.** `tabModel.tabNavTarget` already coerces —
   route every pane's open-app through it so the string/number double-mount
   (the review's HIGH finding) can't recur.
4. **Back button becomes pane-aware.** Today a tab switch is a plain `navTo`
   riding the single `navStack`; that is why tabs are safe now. Multiple panes
   need either per-pane nav history or explicit tagged `tab`/`pane` history
   entries dispatched by type — NOT a priority-list guess, which desyncs the
   Navigation-API/sentinel model (a Codex finding). Design pane focus + back
   together before building.
5. **Test the positive invariant.** The first split view's tests passed while
   the feature was broken because they asserted only `spacer <= client` (a zero
   spacer passes). A pane test must assert the message stays pinned and the
   pane keeps following its stream through a resize/toggle.

## Out of scope until the workspace is the task

paneModel, the layout tree, the workspace reducer, multi-pane rendering, and
drag-and-drop are all deferred. tabModel is the only piece built ahead — it is
useful today (it powers the strip) and is the primitive every pane reuses.
