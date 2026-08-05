// Pane model — the tiled workspace behind the shell.
//
// tabModel.js owns a single flat open set and computes active-ness against one
// global nav focus. This module generalizes that to a binary split tree of
// panes: each pane holds its own set of tabModel tabs plus its own active tab,
// and the workspace tracks which pane has focus. A single pane is the trivial
// leaf. The state shape, normalize() invariants, reducer contract, and rendering
// constraints are documented in ARCHITECTURE.md.
//
// Everything here is pure and dependency-free except tabModel.js. Tab identity,
// construction, the numeric-app-id posture, and nav mapping all stay in
// tabModel; this module only arranges those tabs into panes. Every op returns a
// NEW normalized workspace, or the SAME reference when it changes nothing (the
// workspacePlacement convention, so React can bail on an unchanged tree).

import * as tabModel from './tabModel.js'
// The render-time px clamp is shared with the divider drag state machine
// (splitHelper.makeSplit uses the same function). projectLayout below clamps
// every split's ratio against 280/200 minimums so a stored ratio can never
// project a pane below the usable floor (design §2, §4). Importing one pure
// helper keeps paneModel dependency-free of the DOM while reusing the exact
// clamp semantics the drag layer commits with.
import { clampRatio as clampRatioPx } from '../../lib/splitHelper.js'

// At most four leaf panes, nested at most two splits deep (a balanced binary
// tree of depth 2 has four leaves). moveTab refuses to cross either bound.
export const MAX_PANES = 4
export const MAX_DEPTH = 2

// Responsive breakpoints — capability derives from USABLE CONTENT SIZE, never
// the user agent (design §4). A mode needs BOTH dimensions to qualify; the
// otherwise-clause is the phone stack. modeForRect is the single authority the
// renderer, canSplit, and the (later) resolver all read.
export const WIDE_MIN_W = 960
export const WIDE_MIN_H = 600
export const COMPACT_MIN_W = 700
export const COMPACT_MIN_H = 520

// Render geometry (pixels are a RENDER concern — the model never stores them).
// PANE_GAP sits between two sibling panes and is where a divider is drawn.
// Tiled and focused panes both use the full content rect edge-to-edge.
export const PANE_GAP = 7

// Height of every workspace tab strip, including the flow strip used by a
// one-leaf workspace. A pane's CONTENT rect is its pane rect minus this strip
// row (design §2). Shell publishes it as --shell-tabstrip-height so the flow
// and positioned renderers cannot drift.
export const STRIP_H = 34

// The smallest a pane may be. canSplit refuses a split whose either resulting
// child would fall below this within the pane's current projected rect — the
// shared feasibility predicate drag/menu/resolver all consult (design §3.2,
// §6.2). projectLayout clamps ratios against the same minimums at render time.
export const MIN_PANE_W = 280
export const MIN_PANE_H = 200

// localStorage key for the sole serialized workspace state. The workspace must
// survive a fully closed/relaunched PWA, not only an in-tab reload.
export const STORAGE_KEY = 'mobius-workspace'

// localStorage key for the maximized ("focus one pane full-screen") presentation.
// Deliberately SEPARATE from STORAGE_KEY: focusing a pane must never rewrite the
// persisted split tree or ratios (design §2), so the maximize is a thin presentation
// overlay stored beside the blob and re-seeded on mount. Without this, the reload
// that apply-on-idle fires while the tab is backgrounded dropped the maximize, so a
// full-screen pane came back tiled ("minimized") on return. Colon-style like the flags.
export const FOCUSED_PANE_VIEW_KEY = 'mobius:workspace-focused-pane'

// The stable synthetic pane id the single-world SLOT (chat or app) mounts + owns
// its history under when the item is ABSENT from the builder pane tree (two-worlds
// design: a stable single-world owner rather than assuming paneOf() succeeds). It
// can never collide with a generated `pN` pane id, so a FOCUS/paneOf on it is a
// harmless miss. Shared by Shell (mount owner) and useNavigation (history owner).
export const SINGLE_SLOT_PANE = '__single__'

// A layout node is either a leaf (a pane-id string) or a split object.
function isSplit(node) {
  return node != null && typeof node === 'object'
}

// In-order walk of the layout tree, collecting leaf pane ids left-to-right.
function leafIds(node, out = []) {
  if (isSplit(node)) {
    leafIds(node.a, out)
    leafIds(node.b, out)
  } else if (typeof node === 'string') {
    out.push(node)
  }
  return out
}

// Nesting depth measured in splits: a lone pane is 0, one split is 1, a balanced
// four-pane tree is 2. This is the quantity MAX_DEPTH bounds.
function depthOf(node) {
  if (!isSplit(node)) return 0
  return 1 + Math.max(depthOf(node.a), depthOf(node.b))
}

// Every split id in the tree, in-order.
function splitIdsOf(node, out = []) {
  if (isSplit(node)) {
    out.push(node.id)
    splitIdsOf(node.a, out)
    splitIdsOf(node.b, out)
  }
  return out
}

// The trailing integer of a generated id ('p12' -> 12, 's3' -> 3), else 0.
function idSuffix(id) {
  const m = /(\d+)$/.exec(typeof id === 'string' ? id : '')
  return m ? parseInt(m[1], 10) : 0
}

// The valid drop edges; anything else is not a split direction and no-ops.
const EDGES = new Set(['left', 'right', 'top', 'bottom'])

// Ratio is a's fraction, stored clamped to [0.1, 0.9]; a non-finite ratio is a
// degenerate split and resets to an even 0.5 (px minimums are a render concern).
function clampRatio(ratio) {
  const n = Number(ratio)
  if (!Number.isFinite(n)) return 0.5
  return Math.min(0.9, Math.max(0.1, n))
}

// Coerce one raw tab to a canonical tabModel tab, or null if it can't be a live
// tab: drop unknown kinds, missing ids, and app ids that aren't finite numbers
// (they would become NaN in
// tabNavTarget and never resolve). The Settings tab is the one non-chat/app kind
// accepted, and only under two conditions that make single-instance and rollback
// safety structural rather than guarded-around:
//   - the builder flag is ON — a flag-off (or older) shell drops it like any
//     unknown kind, scrubbing a persisted Settings tab before first render;
//   - the id is exactly the canonical 'settings' — a foreign settings id is NOT
//     coerced to the canonical one (that would silently merge two distinct
//     stored tabs into one key), it is dropped.
function sanitizeTab(raw) {
  if (!raw || raw.id == null) return null
  if (raw.kind === 'apps') {
    return String(raw.id) === tabModel.APPS_ID ? tabModel.appsTab() : null
  }
  if (raw.kind === 'settings') {
    return String(raw.id) === tabModel.SETTINGS_ID
      ? tabModel.settingsTab()
      : null
  }
  if (raw.kind !== 'chat' && raw.kind !== 'app') return null
  if (raw.kind === 'app' && !Number.isFinite(Number(raw.id))) return null
  return tabModel.makeTab(raw.kind, raw.id)
}

function sanitizeTabs(tabs) {
  const out = []
  for (const raw of tabs || []) {
    const tab = sanitizeTab(raw)
    if (tab) out.push(tab)
  }
  return out
}

// The single-screen SLOT is the two-worlds design's persisted "last item opened
// IN single mode" (codex-modecontext-design.md §Recommended state). It is a
// `{ kind:'chat'|'app', id }` on the blob, DISTINCT from the builder pane tree —
// opens in single mode set it without touching the tree, and builder work never
// changes it. Sanitize forgivingly (design: forgiving parse, forward-compat with
// older shells): an unknown/corrupt/settings value normalizes to `null` (explicit
// empty/home), NEVER to the builder's focused item — a deleted or garbled slot
// degrades to the empty single screen, it does not silently resurrect builder
// focus. App ids must be finite numbers (the tab posture) or they would never
// resolve. Property ABSENCE is preserved by the caller (normalize) as the
// migration marker; only a PRESENT-but-invalid value collapses to null here.
function sanitizeSingleScreen(raw) {
  if (raw == null || typeof raw !== 'object') return null
  if (raw.kind === 'apps' && String(raw.id) === tabModel.APPS_ID) {
    return tabModel.appsTab()
  }
  if (raw.kind === 'chat' && raw.id != null && String(raw.id).trim() !== '') {
    return { kind: 'chat', id: String(raw.id) }
  }
  if (raw.kind === 'app' && Number.isFinite(Number(raw.id))) {
    return { kind: 'app', id: String(raw.id) }
  }
  return null
}

// Keep the first occurrence of each tab key; later duplicates are dropped.
function dedupTabs(tabs) {
  const seen = new Set()
  const out = []
  for (const tab of tabs) {
    const key = tabModel.tabKey(tab)
    if (seen.has(key)) continue
    seen.add(key)
    out.push(tab)
  }
  return out
}

// Structural equality that ignores object key order, so a normalized rebuild can
// be compared against its input to preserve reference identity on a no-op.
function deepEqual(a, b) {
  if (a === b) return true
  if (typeof a !== typeof b) return false
  if (a === null || b === null) return a === b
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false
    for (let i = 0; i < a.length; i += 1) if (!deepEqual(a[i], b[i])) return false
    return true
  }
  if (typeof a === 'object') {
    const ka = Object.keys(a)
    const kb = Object.keys(b)
    if (ka.length !== kb.length) return false
    for (const k of ka) if (!deepEqual(a[k], b[k])) return false
    return true
  }
  return false
}

// Replace a leaf pane id anywhere in the tree with a replacement subtree.
function replaceLeaf(node, leafId, replacement) {
  if (isSplit(node)) {
    return {
      ...node,
      a: replaceLeaf(node.a, leafId, replacement),
      b: replaceLeaf(node.b, leafId, replacement),
    }
  }
  return node === leafId ? replacement : node
}

// Prune the tree to the panes that are alive (non-empty), collapsing a split
// that loses one child to the surviving child and dropping a leaf whose pane is
// gone or already placed elsewhere (a duplicate leaf reference). Returns null
// when the whole subtree is gone.
function pruneTree(node, alive, placed) {
  if (isSplit(node)) {
    const a = pruneTree(node.a, alive, placed)
    const b = pruneTree(node.b, alive, placed)
    if (a && b) return { ...node, a, b, ratio: clampRatio(node.ratio) }
    return a || b || null
  }
  if (typeof node !== 'string' || !alive.has(node) || placed.has(node)) return null
  placed.add(node)
  return node
}

// The nearest surviving leaf to a removed focus target, searched outward in the
// original in-order sequence, so focus lands on a neighbour rather than jumping.
function nearestSurviving(orderedIds, survivors, targetId) {
  const idx = orderedIds.indexOf(targetId)
  if (idx !== -1) {
    for (let d = 0; d < orderedIds.length; d += 1) {
      const right = orderedIds[idx + d]
      if (right && survivors.has(right)) return right
      const left = orderedIds[idx - d]
      if (left && survivors.has(left)) return left
    }
  }
  for (const id of survivors) return id
  return null
}

function panesAreEmpty(panes) {
  return Object.values(panes).every(pane => pane.tabs.length === 0)
}

// Enforce every workspace invariant, idempotently, and return the SAME reference
// when the input already satisfies them (so normalize(normalize(ws)) is
// reference-stable and callers can bail on an unchanged tree). Repairs — never
// rebalances: a tree that is too deep or too wide survives unchanged for parse
// to reject, because ops guarantee the bounds and only a corrupt blob can breach
// them.
export function normalize(ws) {
  if (!ws || typeof ws !== 'object') return seedFromFlatTabs([])
  const panesIn = (ws.panes && typeof ws.panes === 'object') ? ws.panes : {}

  // Pane ids in in-order leaf order, first occurrence winning.
  const orderedIds = []
  const seenId = new Set()
  for (const id of leafIds(ws.layout)) {
    if (typeof id !== 'string' || seenId.has(id)) continue
    seenId.add(id)
    orderedIds.push(id)
  }

  // Clean each referenced pane's tabs; a referenced-but-missing pane is empty.
  const cleaned = new Map()
  for (const id of orderedIds) {
    const src = panesIn[id]
    cleaned.set(id, {
      id,
      tabs: sanitizeTabs(src && Array.isArray(src.tabs) ? src.tabs : []),
      activeTabKey: src ? src.activeTabKey : null,
    })
  }

  // A tab is unique workspace-wide: the first pane (in leaf order) to hold it
  // keeps it, later panes lose it (drop = move, never copy).
  const seenTab = new Set()
  for (const id of orderedIds) {
    const pane = cleaned.get(id)
    pane.tabs = pane.tabs.filter(tab => {
      const key = tabModel.tabKey(tab)
      if (seenTab.has(key)) return false
      seenTab.add(key)
      return true
    })
  }

  const alive = new Set()
  for (const id of orderedIds) {
    if (cleaned.get(id).tabs.length > 0) alive.add(id)
  }

  const placed = new Set()
  let layout = pruneTree(ws.layout, alive, placed)

  // Everything collapsed away: keep exactly one empty root pane so the shell
  // still has a home surface, preserving the focused pane's identity if we can.
  if (layout == null) {
    const rootId = (typeof ws.focusedPaneId === 'string' && cleaned.has(ws.focusedPaneId))
      ? ws.focusedPaneId
      : (orderedIds[0] || 'p0')
    cleaned.set(rootId, { id: rootId, tabs: [], activeTabKey: null })
    placed.clear()
    placed.add(rootId)
    layout = rootId
  }

  // Rebuild the panes map from the surviving leaves only, coercing each active
  // tab to a real member (else the last tab, else null for an empty root).
  const panes = {}
  for (const id of placed) {
    const pane = cleaned.get(id)
    const keys = pane.tabs.map(tabModel.tabKey)
    let active = pane.activeTabKey
    if (active == null || !keys.includes(active)) {
      active = keys.length ? keys[keys.length - 1] : null
    }
    panes[id] = { id, tabs: pane.tabs, activeTabKey: active }
  }

  // Focus must name a live pane; else the nearest surviving leaf.
  let focused = ws.focusedPaneId
  if (typeof focused !== 'string' || !panes[focused]) {
    focused = nearestSurviving(orderedIds, placed, ws.focusedPaneId)
  }

  // The id generator is a MONOTONIC high-water mark. It must clear the largest
  // live pane/split suffix (repairing a corrupt or rolled-back blob whose stored
  // nextId LAGS — that would mint a colliding `pN`/`sN`, overwrite the node, and
  // lose its tab when the duplicate leaf collapses), AND it must never REGRESS
  // below the stored value: collapsing a split frees its `pN`/`sN`, and recomputing
  // purely from the smaller live tree would reissue those exact ids so a stale
  // physical-history hint for the dead pane suddenly matches a live one and
  // restoreRoute targets the wrong pane (finding: id reuse). max() of both
  // satisfies both directions.
  let maxId = 0
  for (const id of Object.keys(panes)) maxId = Math.max(maxId, idSuffix(id))
  for (const id of splitIdsOf(layout)) maxId = Math.max(maxId, idSuffix(id))
  const storedNext = (Number.isInteger(ws.nextId) && ws.nextId > 0) ? ws.nextId : 0
  const nextId = Math.max(maxId + 1, storedNext)
  // Builder is a presentation of actual pane content, never an empty world. A
  // stale/corrupt persisted blob can still say `panes` after every tab is gone;
  // repair that at the model boundary so boot cannot strand the owner in a blank
  // Builder. Non-empty trees preserve their mode.
  const requestedViewMode = coerceViewMode(ws.viewMode)
  const viewMode = requestedViewMode === 'panes'
    && panesAreEmpty(panes)
    ? 'single'
    : requestedViewMode
  const result = { v: 1, viewMode, layout, panes, focusedPaneId: focused, nextId }
  // The single-screen slot rides through normalize the same way viewMode does:
  // a preserved field, forgivingly sanitized, that never affects the tree. ABSENCE
  // is preserved (the migration marker: an older/uninitialized blob seeds the slot
  // on its first builder→single switch); a PRESENT value is sanitized so a corrupt
  // slot becomes explicit-empty (null) rather than absent. Keeping blob version 1
  // (design: no v2 bump) means an older shell that ignores this key still parses
  // the tree — forward-compat by construction.
  if ('singleScreen' in ws) result.singleScreen = sanitizeSingleScreen(ws.singleScreen)
  return deepEqual(result, ws) ? ws : result
}

// Normalize a candidate, but return the ORIGINAL workspace when the net effect
// is nothing, so every op honors the same-reference-on-no-op contract even when
// the mutation cancelled out (e.g. a reorder to the same slot).
function commit(ws, candidate) {
  const result = normalize(candidate)
  return deepEqual(result, ws) ? ws : result
}

// commit plus the geometry bound: a split that would exceed MAX_PANES leaves or
// MAX_DEPTH is refused as a same-reference no-op (§1 enforces depth/width at the
// model level, since reducer/parse/undo all bypass the drag layer's guards).
function commitBounded(ws, candidate) {
  const result = normalize(candidate)
  if (leafIds(result.layout).length > MAX_PANES || depthOf(result.layout) > MAX_DEPTH) return ws
  return deepEqual(result, ws) ? ws : result
}

// Seed a single-pane workspace from today's flat open set. Tabs are sanitized
// and deduped; the last tab is active (the on-screen one under the flat model).
// A fresh or fallback workspace starts in the standard single-screen world.
// Persisted valid workspaces keep their chosen mode through normalize(); this
// seed is only used when there is no usable workspace blob. parseWorkspace returns it DIRECTLY on
// the fresh/fallback/invalid paths, so the seed itself must carry the render mode.
export function seedFromFlatTabs(tabs) {
  const clean = dedupTabs(sanitizeTabs(tabs))
  const paneId = 'p0'
  const keys = clean.map(tabModel.tabKey)
  // A pure constructor: NO singleScreen slot (absence is the migration marker, and
  // this same builder is used by fixtures that model a legacy absent-slot blob). The
  // genuine first-boot seed lives in the RESET_FLAT reducer, which is the only path
  // that turns a legacy/flat active chat into the live Standard world.
  return {
    v: 1,
    viewMode: coerceViewMode('single'),
    layout: paneId,
    panes: {
      [paneId]: {
        id: paneId,
        tabs: clean,
        activeTabKey: keys.length ? keys[keys.length - 1] : null,
      },
    },
    focusedPaneId: paneId,
    nextId: 1,
  }
}

// The two view-modes. 'panes' is the tiled default; 'single' collapses a
// preserved multi-pane tree down to the focused pane's active tab, painted
// full-bleed (the workspaceView derivation reads viewMode; the tree is untouched
// so toggling back re-projects it exactly). Any other/absent value is 'panes'.
//
function coerceViewMode(mode) {
  return mode === 'single' ? 'single' : 'panes'
}

function singleScreenTab(ws) {
  // Standard's surface is its OWN slot only — an absent (legacy/uninitialized) slot
  // is the empty home, never the focused Builder pane. sanitizeSingleScreen(undefined)
  // collapses to null, so Standard never borrows Builder's focus (two-worlds design).
  const item = sanitizeSingleScreen(ws.singleScreen)
  if (!item) return null
  if (item.kind === 'apps') return tabModel.appsTab()
  return tabModel.makeTab(item.kind, item.id)
}

// Seed only the hidden Builder tree. Keeping this separate from the mode flip
// lets compound gestures prepare a candidate tree, then commit the mode only
// after the rest of the gesture succeeds.
function seedEmptyBuilder(ws) {
  if (!isEmptyTree(ws)) return ws
  const tab = singleScreenTab(ws)
  return tab ? doOpenTab(ws, tab) : ws
}

// Set the view-mode. Pure; returns the SAME reference on a true no-op. Builder
// has no empty state: entering it from an empty tree seeds the current Standard
// screen as its first tab. If Standard is itself the empty New Chat landing,
// there is no content to seed and the workspace honestly remains Standard.
export function setViewMode(ws, mode) {
  const requested = coerceViewMode(mode)
  const nextWs = requested === 'panes' ? seedEmptyBuilder(ws) : ws
  if (requested === 'panes' && isEmptyTree(nextWs)) return ws
  if (nextWs.viewMode === requested) return nextWs
  return { ...nextWs, viewMode: requested }
}

// Flip single <-> panes. An empty Standard workspace seeds its current screen.
export function toggleViewMode(ws) {
  return setViewMode(ws, ws.viewMode === 'single' ? 'panes' : 'single')
}

// ── Single-screen slot ops (two-worlds design) ──────────────────────────────
//
// The slot is the single world's ENTIRE memory: exactly one screen, the last
// concrete item opened while in single mode. These ops NEVER touch the pane tree
// (design: opens in single mode must not mutate the tree), so builder and single
// keep independent navigation contexts.

// The `chat:<id>` / `app:<id>` key of the slot item, or null for an empty/home
// slot. Shell unions this with the builder tab keys to keep the slot's iframe /
// chat MOUNTED even while it is absent from the tree (design: mount identity is
// independent of world visibility).
export function singleScreenKey(ws) {
  const slot = ws.singleScreen
  if (!slot || typeof slot !== 'object') return null
  if (slot.kind === 'app') return `app:${slot.id}`
  if (slot.kind === 'chat') return `chat:${slot.id}`
  if (slot.kind === 'apps') return tabModel.APPS_TAB_KEY
  return null
}

// The content currently selected by a mounted surface owner. Real builder
// panes select through activeTabKey; the single world's stable synthetic owner
// selects through its independent slot. Callers that bind lifecycle events to
// an owner use this instead of assuming every owner exists in ws.panes.
export function activeKeyForOwner(ws, ownerPaneId) {
  // The single-world slot owner selects through its OWN slot only. An absent slot
  // is empty (singleScreenKey → null), never the focused Builder pane's tab —
  // Standard never borrows Builder's focus (two-worlds design).
  if (String(ownerPaneId) === SINGLE_SLOT_PANE) return singleScreenKey(ws)
  return ws.panes?.[ownerPaneId]?.activeTabKey ?? null
}

// Set the single-screen slot to a concrete item (or null for the empty/home
// screen). Sanitized like the parse path — an invalid item collapses to null,
// never to builder focus. Same reference on a no-op so React can bail. This is
// the ONLY writer of the slot besides seeding and deletion reconciliation, so the
// "one screen = the last item opened in single mode" invariant lives here.
export function setSingleScreen(ws, slot) {
  const next = sanitizeSingleScreen(slot)
  const cur = ('singleScreen' in ws) ? ws.singleScreen : undefined
  // Compare against the normalized current value so re-setting the same item is a
  // no-op even when the stored id was a number and `next` is a string.
  if (deepEqual(sanitizeSingleScreen(cur), next) && ('singleScreen' in ws)) return ws
  return { ...ws, singleScreen: next }
}

// The concrete chat/app item the FOCUSED builder pane is showing, as a slot value.
// Settings never occupies the single-world slot; when Settings is active, fall
// back to the most recently positioned concrete tab in that same pane. Otherwise
// the first-ever builder→single toggle from the common Settings flow paints an
// empty shell even though the owner's chat is still directly underneath it.
function focusedSlotSeed(ws) {
  const pane = ws.panes[ws.focusedPaneId]
  const key = pane?.activeTabKey
  if (!pane || !key) return null
  const activeTab = pane.tabs.find(t => tabModel.tabKey(t) === key)
  const tab = activeTab?.kind === 'settings'
    ? [...pane.tabs].reverse().find(candidate => candidate.kind !== 'settings')
    : activeTab
  if (!tab) return null
  if (tab.kind === 'app') return { kind: 'app', id: String(tab.id) }
  if (tab.kind === 'chat') return { kind: 'chat', id: String(tab.id) }
  if (tab.kind === 'apps') return tabModel.appsTab()
  return null
}

// Seed the slot ONCE, on the first builder→single switch, using property ABSENCE
// as the migration marker (design §Recommended state). A blob that already carries
// an explicit slot (including explicit `null` = initialized empty) is left
// untouched — an initialized empty screen is never reseeded from builder focus.
// Same reference when the slot is already present.
export function seedSingleScreenIfAbsent(ws) {
  if ('singleScreen' in ws) return ws
  return { ...ws, singleScreen: focusedSlotSeed(ws) }
}

// True when the whole workspace holds no tabs.
export function isEmptyTree(ws) {
  return panesAreEmpty(ws.panes)
}

// normalize() owns the no-empty-Builder invariant, so a close that empties the
// tree already arrives here in Standard. Preserve an existing Standard item; if
// Standard was empty or uninitialized, carry the departing visible item instead.
// The caller uses autoReturned to make Undo restore tab + Builder in one gesture.
function completeCloseTransition(prevWs, nextWs) {
  const autoReturned = prevWs.viewMode === 'panes' && nextWs.viewMode === 'single'
  if (!autoReturned) return { ws: nextWs, autoReturned: false }
  if (nextWs.singleScreen) return { ws: nextWs, autoReturned: true }
  return {
    ws: setSingleScreen(nextWs, focusedSlotSeed(prevWs)),
    autoReturned: true,
  }
}

// Every live leaf pane id in in-order (left-to-right) sequence. The resolver
// walks this to find the first companion pane and to protect visible tabs; it is
// the public projection of the private leaf-walk the renderer already uses.
export function paneIdsInOrder(ws) {
  return leafIds(ws.layout).filter(id => ws.panes[id])
}

// The pane holding a given tab key, or null.
export function paneOf(ws, tabKey) {
  for (const id of leafIds(ws.layout)) {
    const pane = ws.panes[id]
    if (pane && pane.tabs.some(tab => tabModel.tabKey(tab) === tabKey)) return pane
  }
  return null
}

// The legacy `{view, chatId, appId, paneId}` route describing the FOCUSED pane's
// active tab — the projection `useNavigation` derives its compatibility triple
// from (design §1 ownership boundary). Numeric app conversion goes through
// tabModel.tabNavTarget (hard constraint 3), never an independent parse. An
// empty focused pane resolves to the empty chat surface. The paneId is always
// the focused pane so a snapshot route carries its own placement hint.
export function focusedContentRoute(ws) {
  const paneId = ws.focusedPaneId
  const pane = ws.panes[paneId]
  const activeKey = pane?.activeTabKey
  if (pane && activeKey) {
    const tab = pane.tabs.find(t => tabModel.tabKey(t) === activeKey)
    if (tab) {
      // Settings carries no chat/app id — the focused pane simply shows the
      // Settings surface. The derived `activeView` reads 'settings' whether that
      // is a builder tab (here) OR the global overlay; the render tells them
      // apart via the SEPARATE settingsOverlayOpen flag, never via this route
      // (design: the overlay must not be conflated with focused-content-is-Settings).
      if (tab.kind === 'settings') return { view: 'settings', chatId: null, appId: null, paneId }
      if (tab.kind === 'apps') return { view: 'apps', chatId: null, appId: null, paneId }
      const { view, opts } = tabModel.tabNavTarget(tab)
      if (view === 'canvas') return { view: 'canvas', chatId: null, appId: opts.appId, paneId }
      return { view: 'chat', chatId: opts.chatId, appId: null, paneId }
    }
  }
  return { view: 'chat', chatId: null, appId: null, paneId }
}

// The legacy `{view, chatId, appId, paneId}` route describing the SINGLE world's
// slot (two-worlds design). Numeric app conversion matches focusedContentRoute
// (tabModel posture: a finite number). An empty/null slot resolves to the empty
// chat surface — the single world's home screen (design: a deleted/absent slot is
// explicit empty, never builder focus). The paneId rides the focused pane only as
// a placement HINT for a later builder open; single-world nav never reads it.
export function singleScreenRoute(ws) {
  const slot = ws.singleScreen
  const paneId = ws.focusedPaneId
  if (slot && slot.kind === 'apps') {
    return { view: 'apps', chatId: null, appId: null, paneId }
  }
  if (slot && slot.kind === 'app') {
    const appId = Number(slot.id)
    if (Number.isFinite(appId)) return { view: 'canvas', chatId: null, appId, paneId }
  }
  if (slot && slot.kind === 'chat') {
    return { view: 'chat', chatId: slot.id, appId: null, paneId }
  }
  return { view: 'chat', chatId: null, appId: null, paneId }
}

// The legacy triple describing what the CURRENT WORLD shows: the single-screen
// slot in single mode, the focused pane's active tab in builder. This is the ONE
// place the two worlds' "active content" diverge for the nav adapter's projection
// and the reload snapshot — so a single-world reload restores the slot, not the
// builder focus (design: derive activeView from the current world).
export function activeContentRoute(ws) {
  // In single mode the active content is ALWAYS the Standard slot, never the focused
  // Builder pane. An absent (legacy/uninitialized) slot reports the empty home exactly
  // like an explicit-null slot — Standard is authoritative and never borrows Builder's
  // focus (two-worlds design); opening an item in single mode initializes the slot.
  if (ws.viewMode === 'single') return singleScreenRoute(ws)
  return focusedContentRoute(ws)
}

// The string app ids that are the active tab of one of `visibleLeaves` (or every
// leaf when omitted). This is the pinned set the synchronous iframe-cache
// derivation unions with the warm LRU (design §2/§4) and the set the app
// visibility gate (`isVisibleApp`) and frame-visibility forwarding read.
export function visibleAppIds(ws, visibleLeaves) {
  const set = new Set()
  const leaves = visibleLeaves || leafIds(ws.layout)
  for (const paneId of leaves) {
    const pane = ws.panes[paneId]
    if (!pane || !pane.activeTabKey) continue
    const active = pane.tabs.find(t => tabModel.tabKey(t) === pane.activeTabKey)
    if (active && active.kind === 'app') set.add(String(active.id))
  }
  return set
}

// The tab key an in-memory restorable route points at, or null for a route with
// no concrete workspace item (Settings, or a home-seed chat route).
function routeItemKey(route) {
  if (!route || typeof route !== 'object') return null
  if (route.view === 'apps') return tabModel.APPS_TAB_KEY
  if (route.view === 'canvas' && route.appId != null) return `app:${route.appId}`
  if (route.view === 'chat' && !route.homeSeed && route.chatId != null) return `chat:${route.chatId}`
  return null
}

// Retarget the `paneId` hints on a list of in-memory restorable routes after a
// workspace transition (design §5.1.3). A route's hint should name the pane that
// currently holds its item, so:
//   - if the route's item is open in nextWs, the hint FOLLOWS it to that pane —
//     this covers a cross-pane move even when the source pane survived (its old
//     hint stays stale otherwise) and a last-tab move to a NON-sibling pane
//     (the item's real destination, not the collapse sibling);
//   - else, when the hint names a pane the transition removed, it degrades to
//     the structural sibling the collapse chose (a closed tab reopened by Back
//     lands beside where its pane used to be), or the focused pane as a last
//     resort;
//   - else it is left untouched.
// Pure; returns the SAME array reference when nothing changed. Physical history
// entries (which cannot be enumerated) self-correct at restore time because
// OPEN_TAB dedups an already-open item to its true pane regardless of the hint.
export function reconcileRoutePanes(routes, prevWs, nextWs) {
  if (!Array.isArray(routes) || routes.length === 0) return routes
  let changed = false
  const out = routes.map((route) => {
    if (!route || typeof route !== 'object') return route
    const itemKey = routeItemKey(route)
    if (itemKey) {
      const pane = paneOf(nextWs, itemKey)
      if (pane) {
        if (pane.id === route.paneId) return route
        changed = true
        return { ...route, paneId: pane.id }
      }
    }
    // No live item at this route (closed tab, or a paneless view): degrade only a
    // hint that names a now-dead pane.
    if (route.paneId != null && !nextWs.panes[route.paneId]) {
      const sib = survivingSiblingOf(prevWs, nextWs, route.paneId) || nextWs.focusedPaneId
      if (sib && sib !== route.paneId) {
        changed = true
        return { ...route, paneId: sib }
      }
    }
    return route
  })
  return changed ? out : routes
}

// The surviving sibling a pane-removal collapse chose for a now-dead pane: the
// nearest leaf in the PRE-transition in-order sequence that still exists after
// the collapse (contract §5.1.3). This is the neighbour that inherited the
// removed pane's space, computed the same way the reducer picks a focus target —
// so dead-pane route hints retarget to the structural sibling, not global focus.
export function survivingSiblingOf(prevWs, nextWs, deadPaneId) {
  const ordered = leafIds(prevWs.layout)
  const survivors = new Set(Object.keys(nextWs.panes))
  return nearestSurviving(ordered, survivors, deadPaneId)
}

// True iff `raw` is a well-formed, current-version, invariant-satisfying workspace
// blob — the exact success condition of parseWorkspace, WITHOUT the flat-seed
// fallback. Lets boot distinguish "a valid persisted workspace is authoritative"
// from "no/again-invalid blob, fall back to the legacy triple" (contract §5.3.10).
export function isValidWorkspaceBlob(raw) {
  try {
    if (typeof raw !== 'string' || raw.length === 0) return false
    const parsed = JSON.parse(raw)
    if (!parsed || parsed.v !== 1) return false
    return isValidWorkspace(normalize(parsed))
  } catch {
    return false
  }
}

// Open a tab. A tab remains open until an explicit close, resource deletion, or
// prune removes it; opening another tab never evicts existing work.
// Options beyond the flat-strip open:
//   afterKey  insert directly after the named member tab (a resolver inserting a
//             built app right after its source chat); absent → append (legacy).
//   focus     move focus to the target pane (default true). A BACKGROUND resolver
//             placement passes focus:false so it can never switch which pane owns
//             the keyboard/Back or which content is on screen (design §6.2).
function doOpenTab(ws, tab, {
  paneId, activate = true, afterKey = null, focus = true,
} = {}) {
  const clean = sanitizeTab(tab)
  if (!clean) return ws
  const key = tabModel.tabKey(clean)

  // Already open anywhere: focus that pane and (if activating) make it active —
  // never duplicate, since a tab is unique workspace-wide.
  const existing = paneOf(ws, key)
  if (existing) {
    const candidate = {
      ...ws,
      focusedPaneId: focus ? existing.id : ws.focusedPaneId,
      panes: {
        ...ws.panes,
        [existing.id]: {
          ...existing,
          activeTabKey: activate ? key : existing.activeTabKey,
        },
      },
    }
    return commit(ws, candidate)
  }

  const targetId = (paneId && ws.panes[paneId]) ? paneId : ws.focusedPaneId
  const target = ws.panes[targetId]
  if (!target) return ws

  let tabs = target.tabs
  // Insert after the named anchor, else append.
  let at = tabs.length
  if (afterKey != null) {
    const ai = tabs.findIndex(t => tabModel.tabKey(t) === afterKey)
    if (ai !== -1) at = ai + 1
  }
  tabs = [...tabs.slice(0, at), clean, ...tabs.slice(at)]
  const candidate = {
    ...ws,
    focusedPaneId: focus ? targetId : ws.focusedPaneId,
    panes: {
      ...ws.panes,
      [targetId]: {
        ...target,
        tabs,
        activeTabKey: activate ? key : target.activeTabKey,
      },
    },
  }
  return commit(ws, candidate)
}

export function openTab(ws, tab, opts) {
  return doOpenTab(ws, tab, opts)
}

// Open a tab AT a drop target (design §3.4) — the single-commit path a drag drop
// takes so one Undo restores it. Unifies both drag sources: a strip tab already
// in the workspace degrades to a plain moveTab; a drawer item not yet open is
// created and placed. The tab lands exactly where the drop zone named it:
//   { paneId }              center-join: append + activate in that pane
//   { paneId, index }       strip caret: insert at the index
//   { paneId, edge }        pane split: alone in a new pane on that edge
//   { root: true, edge }    root split: alone in a new pane splitting the whole
// A split places the item in a NEW pane, never leaving it in the target pane, so
// it composes openTab-into-target then moveTab-out as one normalized result.
// Same reference on a no-op (moveTab/openTab both honor it).
export function openTabAt(ws, tab, target) {
  const clean = sanitizeTab(tab)
  if (!clean) return ws
  const key = tabModel.tabKey(clean)
  // Already open anywhere → this is a move, not an open (tab is unique).
  if (paneOf(ws, key)) return moveTab(ws, key, target)
  if (!target) return openTab(ws, clean)
  // Splits build a fresh pane directly — never insert into the target pane and
  // move out, so the target's ownership and active tab stay unchanged.
  if (target.edge != null && target.root === true) return rootSplitOpen(ws, clean, key, target.edge)
  if (target.edge != null && target.paneId != null) {
    // A drag drop is foreground — the drop gesture IS the intent (focus:true).
    return splitPaneWithTab(ws, clean, { paneId: target.paneId, edge: target.edge })
  }
  if (target.paneId != null) {
    // center-join / strip-caret: this genuinely adds the item to the target pane.
    const opened = openTab(ws, clean, { paneId: target.paneId, activate: true })
    return target.index != null ? moveTab(opened, key, { paneId: target.paneId, index: target.index }) : opened
  }
  return openTab(ws, clean)
}

// Close a tab; if it was the pane's active tab, activate the neighbour before it
// (else the new last tab) — fixing today's close-the-viewed-tab-into-nothing.
export function closeTab(ws, tabKey) {
  const pane = paneOf(ws, tabKey)
  if (!pane) return ws
  const removedIndex = pane.tabs.findIndex(tab => tabModel.tabKey(tab) === tabKey)
  const tabs = pane.tabs.filter((_, i) => i !== removedIndex)
  let active = pane.activeTabKey
  if (active === tabKey) {
    if (tabs.length === 0) active = null
    else if (removedIndex - 1 >= 0) active = tabModel.tabKey(tabs[removedIndex - 1])
    else active = tabModel.tabKey(tabs[tabs.length - 1])
  }
  return commit(ws, {
    ...ws,
    panes: { ...ws.panes, [pane.id]: { ...pane, tabs, activeTabKey: active } },
  })
}

// Close a whole pane: drop every tab it holds, then normalize (the emptied pane
// collapses and its space returns to the surviving sibling, focus follows). The
// keyboard/menu "Close pane" affordance (design §3.6) so a multi-tab pane need
// not be dismissed one ✕ at a time. Reversible — the reducer snapshot restores
// the pane and its tabs (the backing chats/apps were never deleted). Same
// reference on a no-op (unknown pane).
export function closePane(ws, paneId) {
  const pane = ws.panes[paneId]
  if (!pane) return ws
  return commit(ws, {
    ...ws,
    panes: { ...ws.panes, [paneId]: { ...pane, tabs: [], activeTabKey: null } },
  })
}

// Close every OTHER tab in the kept tab's pane (the context-menu "Close all
// other tabs" affordance). The kept tab becomes the pane's active tab — the
// gesture's intent is "just this one". Scoped to ONE pane on purpose: other
// panes are surfaces the owner arranged deliberately, and this menu never
// reaches across them. The pane can never empty (the kept tab survives), so
// no auto-return applies. Reversible — the reducer snapshot restores the
// closed tabs. Same reference on a no-op (unknown tab, or already alone).
export function closeOtherTabs(ws, tabKey) {
  const pane = paneOf(ws, tabKey)
  if (!pane || pane.tabs.length <= 1) return ws
  const kept = pane.tabs.find(tab => tabModel.tabKey(tab) === tabKey)
  return commit(ws, {
    ...ws,
    panes: { ...ws.panes, [pane.id]: { ...pane, tabs: [kept], activeTabKey: tabKey } },
  })
}

// Close only the tabs after the named tab in its pane. If the active tab is
// among those removed, the named tab becomes active; otherwise focus stays
// where it was. Other panes are untouched and the operation is reversible.
export function closeTabsToRight(ws, tabKey) {
  const pane = paneOf(ws, tabKey)
  if (!pane) return ws
  const index = pane.tabs.findIndex(tab => tabModel.tabKey(tab) === tabKey)
  if (index < 0 || index === pane.tabs.length - 1) return ws
  const tabs = pane.tabs.slice(0, index + 1)
  const activeSurvives = tabs.some(tab => tabModel.tabKey(tab) === pane.activeTabKey)
  return commit(ws, {
    ...ws,
    panes: {
      ...ws.panes,
      [pane.id]: {
        ...pane,
        tabs,
        activeTabKey: activeSurvives ? pane.activeTabKey : tabKey,
      },
    },
  })
}

// Move a tab within/between panes. target is one of:
//   { paneId, index? }  insert into an existing pane at index (append if absent)
//   { paneId, edge }    split that pane on the edge, the tab alone in the new one
//   { root: true, edge } split the whole workspace on the edge
// An edge is 'left'/'right' (a 'row' split) or 'top'/'bottom' (a 'col' split);
// the new pane sits on the named side at ratio 0.5 and takes focus. A split that
// would breach the pane-count or depth bound is refused (same reference).
export function moveTab(ws, tabKey, target) {
  const src = paneOf(ws, tabKey)
  if (!src || !target) return ws
  const tab = src.tabs.find(t => tabModel.tabKey(t) === tabKey)

  if (target.edge != null) {
    // A malformed edge is not a silent bottom-split; reject the whole move so a
    // corrupt drag payload can't reorganize the workspace behind the user.
    if (!EDGES.has(target.edge)) return ws
    if (target.root === true) return rootSplitMove(ws, src, tab, tabKey, target.edge)
    if (target.paneId != null) return edgeSplitMove(ws, src, tab, tabKey, target.paneId, target.edge)
    return ws
  }
  if (target.paneId != null) {
    return indexMove(ws, src, tab, tabKey, target.paneId, target.index)
  }
  return ws
}

function indexMove(ws, src, tab, tabKey, destId, index) {
  const dest = ws.panes[destId]
  if (!dest) return ws
  const panes = { ...ws.panes }
  const srcIdx = src.tabs.findIndex(t => tabModel.tabKey(t) === tabKey)
  const srcTabs = src.tabs.filter(t => tabModel.tabKey(t) !== tabKey)
  const base = destId === src.id
    ? srcTabs
    : dest.tabs.filter(t => tabModel.tabKey(t) !== tabKey)
  // The caret index is measured against the pane's CURRENT tab list, source
  // included. For a same-pane reorder the source has been spliced out of `base`,
  // so every target slot PAST the source's old position shifts down by one —
  // map in original-list coordinates FIRST, then clamp. Clamping before the
  // shift landed a rightward drag one slot too far ([A,B,C,D]: B between C,D →
  // after D). Cross-pane keeps the index as-is (the source was never in dest).
  let at
  if (index == null || index < 0) at = base.length
  else {
    at = (destId === src.id && srcIdx !== -1 && index > srcIdx) ? index - 1 : index
    at = Math.max(0, Math.min(at, base.length))
  }
  const destTabs = [...base.slice(0, at), tab, ...base.slice(at)]
  if (destId === src.id) {
    panes[src.id] = { ...src, tabs: destTabs }
  } else {
    panes[src.id] = { ...src, tabs: srcTabs }
    panes[destId] = { ...dest, tabs: destTabs, activeTabKey: tabKey }
  }
  return commit(ws, { ...ws, panes, focusedPaneId: destId })
}

function splitNodeFor(edge, splitId, existingSide, newPaneId, ratio) {
  const dir = (edge === 'left' || edge === 'right') ? 'row' : 'col'
  const newFirst = edge === 'left' || edge === 'top'
  return newFirst
    ? { id: splitId, dir, a: newPaneId, b: existingSide, ratio }
    : { id: splitId, dir, a: existingSide, b: newPaneId, ratio }
}

function edgeSplitMove(ws, src, tab, tabKey, paneId, edge) {
  if (!ws.panes[paneId]) return ws
  // Splitting a pane by moving its OWN sole tab onto its OWN edge is a true
  // no-op: the source empties and collapses, leaving one pane in the same spot
  // with a fresh id — a rename, not a move. Refuse it so a drawer-drag of an
  // already-open sole item (whose source carries no paneId, so the drag layer's
  // single-source guard misses it) can't churn the pane id or raise a false
  // "Moved" toast (finding: sole-item drawer-drag rename).
  if (src.id === paneId && src.tabs.length <= 1) return ws
  const newPaneId = `p${ws.nextId}`
  const splitId = `s${ws.nextId + 1}`
  const panes = { ...ws.panes }
  panes[src.id] = { ...src, tabs: src.tabs.filter(t => tabModel.tabKey(t) !== tabKey) }
  panes[newPaneId] = { id: newPaneId, tabs: [tab], activeTabKey: tabKey }
  const layout = replaceLeaf(ws.layout, paneId, splitNodeFor(edge, splitId, paneId, newPaneId, 0.5))
  return commitBounded(ws, {
    ...ws, layout, panes, focusedPaneId: newPaneId, nextId: ws.nextId + 2,
  })
}

function rootSplitMove(ws, src, tab, tabKey, edge) {
  // Root-splitting the whole workspace by moving the SOLE tab of the SOLE pane
  // is the same rename no-op as the edge case above — the source empties, the
  // new pane is all that survives. Refuse it (finding: sole-item drawer-drag
  // rename). With other panes present a root split is a real relocation, so the
  // guard is scoped to the single-pane workspace only.
  if (src.tabs.length <= 1 && leafIds(ws.layout).filter(id => ws.panes[id]).length <= 1) return ws
  const newPaneId = `p${ws.nextId}`
  const splitId = `s${ws.nextId + 1}`
  const panes = { ...ws.panes }
  panes[src.id] = { ...src, tabs: src.tabs.filter(t => tabModel.tabKey(t) !== tabKey) }
  panes[newPaneId] = { id: newPaneId, tabs: [tab], activeTabKey: tabKey }
  const layout = splitNodeFor(edge, splitId, ws.layout, newPaneId, 0.5)
  return commitBounded(ws, {
    ...ws, layout, panes, focusedPaneId: newPaneId, nextId: ws.nextId + 2,
  })
}

// Root-split the whole workspace to place a brand-new tab alone in the new pane.
// The counterpart of rootSplitMove for an open — never touching any existing
// pane's tab set. The drag layer's root-edge drop takes this path (a leaf split
// goes through splitPaneWithTab below).
function rootSplitOpen(ws, tab, tabKey, edge) {
  const newPaneId = `p${ws.nextId}`
  const splitId = `s${ws.nextId + 1}`
  const panes = { ...ws.panes, [newPaneId]: { id: newPaneId, tabs: [tab], activeTabKey: tabKey } }
  const layout = splitNodeFor(edge, splitId, ws.layout, newPaneId, 0.5)
  return commitBounded(ws, {
    ...ws, layout, panes, focusedPaneId: newPaneId, nextId: ws.nextId + 2,
  })
}

// Split `paneId` on `edge`, placing a BRAND-NEW tab alone in the freshly created
// pane (the item is active there — it is the pane's only tab). This is the single
// leaf-split-open primitive: a drawer/drop open (openTabAt) and the pane-aware
// resolver (design §6.2 auto-split) both take it. It NEVER touches the target
// pane's tab set — no transient insert, no oldest-tab eviction, no activeTabKey
// swap (that transient-insert-then-move-out was the drawer-drop eviction bug,
// review B1). Focus moves to the new pane iff `focus`: a drag drop is foreground,
// while a background resolver placement passes focus:false so a new pane replaces
// nothing on screen and keyboard/Back stay put. Refused (same reference) when the
// tab is invalid/already open, the pane or edge is unknown, or the split would
// breach the pane-count / depth bound (commitBounded).
export function splitPaneWithTab(ws, tab, { paneId, edge, focus = true } = {}) {
  const clean = sanitizeTab(tab)
  if (!clean || !ws.panes[paneId] || !EDGES.has(edge)) return ws
  const key = tabModel.tabKey(clean)
  if (paneOf(ws, key)) return ws
  const newPaneId = `p${ws.nextId}`
  const splitId = `s${ws.nextId + 1}`
  const panes = { ...ws.panes, [newPaneId]: { id: newPaneId, tabs: [clean], activeTabKey: key } }
  const layout = replaceLeaf(ws.layout, paneId, splitNodeFor(edge, splitId, paneId, newPaneId, 0.5))
  return commitBounded(ws, {
    ...ws,
    layout,
    panes,
    focusedPaneId: focus ? newPaneId : ws.focusedPaneId,
    nextId: ws.nextId + 2,
  })
}

// Make a member tab the pane's active tab.
export function setActiveTab(ws, paneId, tabKey) {
  const pane = ws.panes[paneId]
  if (!pane || pane.activeTabKey === tabKey) return ws
  if (!pane.tabs.some(tab => tabModel.tabKey(tab) === tabKey)) return ws
  return commit(ws, {
    ...ws,
    panes: { ...ws.panes, [paneId]: { ...pane, activeTabKey: tabKey } },
  })
}

// Move keyboard/Back focus to a live pane.
export function focusPane(ws, paneId) {
  if (!ws.panes[paneId] || ws.focusedPaneId === paneId) return ws
  return commit(ws, { ...ws, focusedPaneId: paneId })
}

function setSplitRatio(node, splitId, ratio) {
  if (!isSplit(node)) return null
  if (node.id === splitId) return { ...node, ratio: clampRatio(ratio) }
  const a = setSplitRatio(node.a, splitId, ratio)
  if (a) return { ...node, a }
  const b = setSplitRatio(node.b, splitId, ratio)
  if (b) return { ...node, b }
  return null
}

// Resize a split; the ratio is clamped to [0.1, 0.9].
export function setRatio(ws, splitId, ratio) {
  const layout = setSplitRatio(ws.layout, splitId, ratio)
  if (!layout) return ws
  return commit(ws, { ...ws, layout })
}

// A null/undefined live set means "unknown, keep everything of that kind".
function toIdSet(ids) {
  if (ids == null) return null
  const set = new Set()
  for (const id of ids) set.add(String(id))
  return set
}

// Whether the single-screen slot's backing chat/app is still live. A null/home
// slot is always "live" (nothing to prune). A null live set means "unknown, keep".
function slotIsLive(slot, chats, apps) {
  if (!slot || typeof slot !== 'object') return true
  if (slot.kind === 'chat') return chats == null || chats.has(String(slot.id))
  if (slot.kind === 'app') return apps == null || apps.has(String(slot.id))
  if (slot.kind === 'apps') return true
  return true
}

// Drop tabs whose backing chat/app is no longer live, then normalize (an emptied
// pane collapses). Used when a chat/app is deleted out of band. Reconciles BOTH
// worlds atomically (design §Recommended state, risk 3): a deleted item is
// removed from the builder tree AND clears a matching single-screen slot in the
// same op, so a phantom slot can never point at a dead item. A deleted slot
// degrades to the explicit-empty screen (null), NEVER to builder focus (design:
// no auto-fallback from a deleted slot).
export function prune(ws, { liveChatIds, liveAppIds } = {}) {
  const chats = toIdSet(liveChatIds)
  const apps = toIdSet(liveAppIds)
  const keep = (tab) => {
    if (tab.kind === 'chat') return chats == null || chats.has(tab.id)
    if (tab.kind === 'app') return apps == null || apps.has(tab.id)
    return true
  }
  const panes = {}
  let changed = false
  for (const id of Object.keys(ws.panes)) {
    const pane = ws.panes[id]
    const kept = pane.tabs.filter(keep)
    if (kept.length !== pane.tabs.length) changed = true
    panes[id] = { ...pane, tabs: kept }
  }
  // NOTE: prune()/PRUNE currently have no live dispatcher — the reactive
  // eviction effect and onChatMissing/onPaneChatMissing paths (Shell.jsx) are
  // the delete-reconciliation that actually runs, and they clear a dead slot
  // via CLOSE_TAB reason:'deleted'. This slot check exists so PRUNE remains
  // CORRECT if a bulk-reconcile caller is ever wired; it is not the delete path.
  const slotDead = ('singleScreen' in ws) && !!ws.singleScreen
    && !slotIsLive(ws.singleScreen, chats, apps)
  if (!changed && !slotDead) return ws
  const candidate = { ...ws, panes }
  if (slotDead) candidate.singleScreen = null
  return commit(ws, candidate)
}

// In-order walk of every tab across every pane — the flat projection the
// drawer, app-frame cache, and workspace strip consume.
export function flatten(ws) {
  const out = []
  for (const id of leafIds(ws.layout)) {
    const pane = ws.panes[id]
    if (pane) out.push(...pane.tabs)
  }
  return out
}

// ── Projection: the tree → renderable geometry (design §4) ──────────────────
//
// projectLayout is the SINGLE geometry authority for every mode. The renderer,
// canSplit, and the later resolver all consume the same rects — geometry is
// never computed in two places. Projection is PURE and NEVER mutates ws: it
// reads the tree/focus and returns positioned rectangles the flat content
// wrappers are laid into (no reparenting — a move changes only rect vars).

// True when node contains leafId anywhere beneath it.
function containsLeaf(node, leafId) {
  if (typeof node === 'string') return node === leafId
  if (!isSplit(node)) return false
  return containsLeaf(node.a, leafId) || containsLeaf(node.b, leafId)
}

// First in-order leaf id of a subtree — the "representative" a compact/phone
// pair pulls from the sibling subtree when the sibling is itself a split.
function firstLeaf(node) {
  if (typeof node === 'string') return node
  if (!isSplit(node)) return null
  return firstLeaf(node.a) ?? firstLeaf(node.b)
}

// The split whose DIRECT child is leafId, plus the side ('a'|'b') it sits on.
// This is the leaf's IMMEDIATE parent — the split whose sibling subtree the
// compact/phone projection pairs the focused leaf with.
function parentOfLeaf(node, leafId) {
  if (!isSplit(node)) return null
  if (node.a === leafId) return { split: node, side: 'a' }
  if (node.b === leafId) return { split: node, side: 'b' }
  return parentOfLeaf(node.a, leafId) || parentOfLeaf(node.b, leafId)
}

// Splits from the root to a leaf (0 for the root leaf). A split at this leaf
// would add one level, so canSplit refuses when depthOfLeaf + 1 > MAX_DEPTH.
function depthOfLeaf(node, leafId, depth = 0) {
  if (node === leafId) return depth
  if (!isSplit(node)) return -1
  const a = depthOfLeaf(node.a, leafId, depth + 1)
  if (a !== -1) return a
  return depthOfLeaf(node.b, leafId, depth + 1)
}

// Round every field so the renderer writes whole-pixel rects (no sub-pixel
// seams between abutting panes).
function normalizeRect(rect) {
  return {
    x: Math.round(Number(rect?.x) || 0),
    y: Math.round(Number(rect?.y) || 0),
    w: Math.max(0, Math.round(Number(rect?.w) || 0)),
    h: Math.max(0, Math.round(Number(rect?.h) || 0)),
  }
}

// A ratio-patched COPY of the tree for the divider drag preview — the renderer
// re-projects each frame with the dragged split's live ratio without a reducer
// dispatch (design §2: imperative, React-free per frame). Pure; ws untouched.
function withRatioOverride(node, override) {
  if (!isSplit(node)) return node
  if (node.id === override.splitId) return { ...node, ratio: override.ratio }
  return {
    ...node,
    a: withRatioOverride(node.a, override),
    b: withRatioOverride(node.b, override),
  }
}

// The minimum px extent a SUBTREE needs along one axis ('w' or 'h'). A leaf needs
// one pane minimum; a same-axis split needs both children's minima plus the gap
// between them; a cross-axis split needs the larger child. Dragging an ANCESTOR
// divider must reserve each child SUBTREE's aggregate minimum, not a single leaf
// minimum — otherwise row(row(p1,p2),p3) dragged toward a clamp projects the
// inner leaves far below MIN_PANE_W (finding E-i).
function subtreeMinExtent(node, axis) {
  if (!isSplit(node)) return axis === 'w' ? MIN_PANE_W : MIN_PANE_H
  const a = subtreeMinExtent(node.a, axis)
  const b = subtreeMinExtent(node.b, axis)
  const sameAxis = (node.dir === 'row' && axis === 'w') || (node.dir === 'col' && axis === 'h')
  return sameAxis ? a + PANE_GAP + b : Math.max(a, b)
}

// Wide mode: full tree walk. Divide each split's box along its own axis at the
// px-clamped ratio, leaving a PANE_GAP the divider is drawn into, and recurse.
// Emits one divider per split (each rendered along its own axis, so its ratio
// maps and it is draggable). Each child's clamp reserves that child SUBTREE's
// aggregate minimum (subtreeMinExtent), not a single leaf's.
function layoutTree(node, box, rects, dividers) {
  if (typeof node === 'string') {
    rects[node] = { ...box }
    return
  }
  if (!isSplit(node)) return
  if (node.dir === 'row') {
    const usable = box.w - PANE_GAP
    const r = clampRatioPx(node.ratio, usable, subtreeMinExtent(node.a, 'w'), subtreeMinExtent(node.b, 'w'))
    const wA = Math.round(usable * r)
    dividers.push({
      splitId: node.id, dir: 'row',
      x: box.x + wA, y: box.y, w: PANE_GAP, h: box.h,
      span: usable, origin: box.x, ratio: r,
    })
    layoutTree(node.a, { x: box.x, y: box.y, w: wA, h: box.h }, rects, dividers)
    layoutTree(
      node.b,
      { x: box.x + wA + PANE_GAP, y: box.y, w: usable - wA, h: box.h },
      rects, dividers,
    )
  } else {
    const usable = box.h - PANE_GAP
    const r = clampRatioPx(node.ratio, usable, subtreeMinExtent(node.a, 'h'), subtreeMinExtent(node.b, 'h'))
    const hA = Math.round(usable * r)
    dividers.push({
      splitId: node.id, dir: 'col',
      x: box.x, y: box.y + hA, w: box.w, h: PANE_GAP,
      span: usable, origin: box.y, ratio: r,
    })
    layoutTree(node.a, { x: box.x, y: box.y, w: box.w, h: hA }, rects, dividers)
    layoutTree(
      node.b,
      { x: box.x, y: box.y + hA + PANE_GAP, w: box.w, h: usable - hA },
      rects, dividers,
    )
  }
}

// Lay two leaves into a box along one axis at a px-clamped ratio, with a gap
// between them. The compact/phone pair uses this. A divider is emitted only
// when the pair is rendered along the parent split's OWN axis (withDivider) —
// a phone pair projected from a 'row' split renders stacked at 0.5 and gets NO
// divider because the row ratio does not map to a vertical drag (design §4).
function layoutPair(aId, bId, dir, ratio, box, withDivider, splitId) {
  const rects = {}
  if (dir === 'row') {
    const usable = box.w - PANE_GAP
    const r = clampRatioPx(ratio, usable, MIN_PANE_W, MIN_PANE_W)
    const wA = Math.round(usable * r)
    rects[aId] = { x: box.x, y: box.y, w: wA, h: box.h }
    rects[bId] = { x: box.x + wA + PANE_GAP, y: box.y, w: usable - wA, h: box.h }
    const divider = withDivider ? {
      splitId, dir: 'row', x: box.x + wA, y: box.y, w: PANE_GAP, h: box.h,
      span: usable, origin: box.x, ratio: r,
    } : null
    return { rects, divider }
  }
  const usable = box.h - PANE_GAP
  const r = clampRatioPx(ratio, usable, MIN_PANE_H, MIN_PANE_H)
  const hA = Math.round(usable * r)
  rects[aId] = { x: box.x, y: box.y, w: box.w, h: hA }
  rects[bId] = { x: box.x, y: box.y + hA + PANE_GAP, w: box.w, h: usable - hA }
  const divider = withDivider ? {
    splitId, dir: 'col', x: box.x, y: box.y + hA, w: box.w, h: PANE_GAP,
    span: usable, origin: box.y, ratio: r,
  } : null
  return { rects, divider }
}

// The mode a content box of {w, h} affords. Both dimensions must clear a
// threshold; otherwise it falls to the phone stack (design §4).
export function modeForRect({ w, h } = {}) {
  const width = Number(w) || 0
  const height = Number(h) || 0
  if (width >= WIDE_MIN_W && height >= WIDE_MIN_H) return 'wide'
  if (width >= COMPACT_MIN_W && height >= COMPACT_MIN_H) return 'compact'
  return 'phone'
}

// projectLayout(ws, mode, contentRect[, ratioOverride]) → the renderable
// geometry for the current mode (design §4). Returns:
//   { visibleLeaves: [paneId...],       — panes that render right now
//     rects:  { [paneId]: {x,y,w,h} },  — pane rectangles within contentRect
//     dividers: [{ splitId, dir, x,y,w,h, span, origin, ratio }] }
//
// - EXACTLY ONE visible leaf is the pixel-identical single-pane sentinel: rects
//   is the full contentRect and dividers is empty. The renderer branches on
//   `visibleLeaves.length === 1` to emit today's DOM verbatim (no pane chrome).
// - 'wide'    → all leaves, full edge-to-edge tree walk with a gap + divider per
//               split.
// - 'compact' → the focused leaf + its immediate-parent split's sibling (first
//               in-order leaf of the sibling subtree), laid along the parent's
//               axis at the parent's ratio; the pair gets a divider.
// - 'phone'   → the same pair, ALWAYS stacked vertically; the parent's ratio
//               applies only when its dir is 'col' (else 0.5), and a divider is
//               present only for a 'col' parent (a 'row' ratio does not map).
//
// ratioOverride = { splitId, ratio } | null lets the divider drag re-project
// each frame with a live ratio without a reducer commit.
export function projectLayout(ws, mode, contentRect, ratioOverride = null) {
  const content = normalizeRect(contentRect)
  const leaves = leafIds(ws.layout).filter(id => ws.panes[id])

  // Single (or zero) leaf → full rect, no chrome. The renderer's parity branch.
  if (leaves.length <= 1) {
    const id = leaves[0]
    return {
      visibleLeaves: id ? [id] : [],
      rects: id ? { [id]: { ...content } } : {},
      dividers: [],
    }
  }

  const tree = ratioOverride ? withRatioOverride(ws.layout, ratioOverride) : ws.layout
  const box = content

  // Wide renders EVERY leaf — but only while the box can still honor the pane
  // minimums. A 4-pane tree built legally at 1400px, shrunk to 960px, cannot fit
  // four 280px columns; rendering it anyway clamps every pane below the floor
  // (finding: wide min-width violation). So when the whole tree's aggregate
  // minimum exceeds the box on either axis, degrade to the compact focused-pair
  // projection (the overflow chip reaches the hidden panes) rather than paint
  // sub-floor panes. Design intent (§4): wide = up to 4 leaves, all edges valid
  // per minimums.
  let effectiveMode = mode
  if (mode === 'wide') {
    const fits = subtreeMinExtent(tree, 'w') <= box.w && subtreeMinExtent(tree, 'h') <= box.h
    if (fits) {
      const rects = {}
      const dividers = []
      layoutTree(tree, box, rects, dividers)
      return {
        visibleLeaves: leafIds(tree).filter(id => ws.panes[id]),
        rects,
        dividers,
      }
    }
    effectiveMode = 'compact'
  }

  // compact / phone: the focused leaf paired with its immediate sibling rep.
  const focused = ws.panes[ws.focusedPaneId] ? ws.focusedPaneId : leaves[0]
  const parent = parentOfLeaf(tree, focused)
  if (!parent) {
    // A live leaf with ≥2 leaves in the tree always has a parent split; this is
    // a defensive fall-through only. Show the focused leaf full-bleed.
    return { visibleLeaves: [focused], rects: { [focused]: { ...content } }, dividers: [] }
  }
  const siblingSubtree = parent.side === 'a' ? parent.split.b : parent.split.a
  const siblingRep = firstLeaf(siblingSubtree)
  const aId = parent.side === 'a' ? focused : siblingRep
  const bId = parent.side === 'a' ? siblingRep : focused
  const parentDir = parent.split.dir
  const parentRatio = parent.split.ratio

  if (effectiveMode === 'compact') {
    const { rects, divider } = layoutPair(
      aId, bId, parentDir, parentRatio, box, true, parent.split.id,
    )
    return { visibleLeaves: [aId, bId], rects, dividers: divider ? [divider] : [] }
  }

  // phone — always stacked; ratio maps only for a 'col' parent.
  const phoneRatio = parentDir === 'col' ? parentRatio : 0.5
  const { rects, divider } = layoutPair(
    aId, bId, 'col', phoneRatio, box, parentDir === 'col', parent.split.id,
  )
  return { visibleLeaves: [aId, bId], rects, dividers: divider ? [divider] : [] }
}

// canSplit(ws, paneId, edge, mode, contentRect) — the shared feasibility
// predicate (design §6.2). True iff a split of paneId on edge is allowed:
// within MAX_PANES / MAX_DEPTH, permitted by the mode (phone → top/bottom
// only), and each resulting child clears MIN_PANE_W × MIN_PANE_H inside the
// pane's CURRENT projected rect. The menu greys out and the resolver degrades
// on false — a cap or minimum is felt as "no target", never an error.
export function canSplit(ws, paneId, edge, mode, contentRect) {
  if (!ws || !ws.panes || !ws.panes[paneId]) return false
  if (!EDGES.has(edge)) return false
  if (mode === 'phone' && edge !== 'top' && edge !== 'bottom') return false

  const leaves = leafIds(ws.layout).filter(id => ws.panes[id])
  if (leaves.length >= MAX_PANES) return false
  if (depthOfLeaf(ws.layout, paneId, 0) + 1 > MAX_DEPTH) return false

  const content = normalizeRect(contentRect)
  const projected = projectLayout(ws, mode, content)
  let rect = projected.rects[paneId]
  if (!rect) return false
  const row = edge === 'left' || edge === 'right'
  if (row) {
    const childW = (rect.w - PANE_GAP) / 2
    return childW >= MIN_PANE_W && rect.h >= MIN_PANE_H
  }
  const childH = (rect.h - PANE_GAP) / 2
  return rect.w >= MIN_PANE_W && childH >= MIN_PANE_H
}

// canRootSplit(ws, edge, mode, contentRect) — the whole-workspace-split sibling
// of canSplit, for the root-edge drop zone (design §3.2). True iff wrapping the
// whole tree in a new split on `edge` is allowed: within MAX_PANES / MAX_DEPTH
// (a root split adds ONE level above every existing leaf, so it needs
// depthOf(layout) + 1 ≤ MAX_DEPTH), permitted by the mode (phone → top/bottom
// only), and each half of the full content box clears MIN_PANE_W × MIN_PANE_H.
// The drag layer greys the zone out on false — a cap or minimum is felt as "no
// target", never an error, exactly like canSplit.
export function canRootSplit(ws, edge, mode, contentRect) {
  if (!ws || ws.layout == null) return false
  if (!EDGES.has(edge)) return false
  if (mode === 'phone' && edge !== 'top' && edge !== 'bottom') return false
  const leaves = leafIds(ws.layout).filter(id => ws.panes[id])
  if (leaves.length >= MAX_PANES) return false
  if (depthOf(ws.layout) + 1 > MAX_DEPTH) return false
  const box = normalizeRect(contentRect)
  const row = edge === 'left' || edge === 'right'
  if (row) {
    const childW = (box.w - PANE_GAP) / 2
    return childW >= MIN_PANE_W && box.h >= MIN_PANE_H
  }
  const childH = (box.h - PANE_GAP) / 2
  return box.w >= MIN_PANE_W && childH >= MIN_PANE_H
}

// Recursively validate the layout tree's SHAPE: every node is a leaf string or a
// well-formed split (string id, dir in {'row','col'}, ratio finite in
// [0.1,0.9]), and split ids are unique. normalize keeps a corrupt split's fields
// verbatim (it repairs panes, not split shape), so this is what stops an
// `{id:null, dir:'diagonal'}` blob from reaching the renderer — parse falls back.
function isValidLayout(node, splitIds) {
  if (typeof node === 'string') return true
  if (!isSplit(node)) return false
  if (typeof node.id !== 'string' || splitIds.has(node.id)) return false
  splitIds.add(node.id)
  if (node.dir !== 'row' && node.dir !== 'col') return false
  const ratio = Number(node.ratio)
  if (!Number.isFinite(ratio) || ratio < 0.1 || ratio > 0.9) return false
  return isValidLayout(node.a, splitIds) && isValidLayout(node.b, splitIds)
}

// A workspace that survives every invariant with no repair left to do — the gate
// parseWorkspace applies after normalize to catch what normalize won't rebalance
// (too deep, too wide, malformed splits) and fall back rather than serve a broken
// tree.
function isValidWorkspace(ws) {
  if (!ws || ws.v !== 1 || ws.layout == null || !ws.panes) return false
  if (!isValidLayout(ws.layout, new Set())) return false
  const ids = leafIds(ws.layout)
  if (ids.length === 0 || ids.length > MAX_PANES) return false
  if (depthOf(ws.layout) > MAX_DEPTH) return false
  const seen = new Set()
  for (const id of ids) {
    if (seen.has(id) || !ws.panes[id]) return false
    seen.add(id)
  }
  for (const id of Object.keys(ws.panes)) {
    if (!seen.has(id)) return false
  }
  if (!ws.panes[ws.focusedPaneId]) return false
  const seenTab = new Set()
  for (const id of ids) {
    const pane = ws.panes[id]
    const keys = pane.tabs.map(tabModel.tabKey)
    for (const tab of pane.tabs) {
      if (tab.kind === 'app' && !Number.isFinite(Number(tab.id))) return false
      const key = tabModel.tabKey(tab)
      if (seenTab.has(key)) return false
      seenTab.add(key)
    }
    if (pane.activeTabKey == null && keys.length > 0) return false
    if (pane.activeTabKey != null && !keys.includes(pane.activeTabKey)) return false
  }
  return true
}

export function serializeWorkspace(ws) {
  return JSON.stringify(ws)
}

// The raw stored blob, or null if storage is unavailable. Browser storage getItem
// can THROW (SecurityError in a sandboxed frame, disabled storage, a privacy
// policy), and the caller reads it while evaluating parseWorkspace's argument —
// outside parseWorkspace's own try/catch. Guarding the read here keeps broken
// browser storage from taking down boot.
export function readWorkspaceRaw(storage) {
  try {
    return storage.getItem(STORAGE_KEY)
  } catch {
    return null
  }
}

// Forgiving read/write for the maximized-pane overlay (FOCUSED_PANE_VIEW_KEY),
// mirroring readWorkspaceRaw's throwing-getItem posture. writeFocusedPaneView
// REMOVES the key when nothing is maximized so a dismissed maximize can never
// resurrect on a later reload.
export function readFocusedPaneView(storage) {
  try {
    const raw = storage.getItem(FOCUSED_PANE_VIEW_KEY)
    return typeof raw === 'string' && raw.length > 0 ? raw : null
  } catch {
    return null
  }
}

export function writeFocusedPaneView(paneId, storage) {
  try {
    if (paneId == null) storage.removeItem(FOCUSED_PANE_VIEW_KEY)
    else storage.setItem(FOCUSED_PANE_VIEW_KEY, String(paneId))
  } catch { /* private mode / quota — the maximize just won't survive a reload */ }
}

// Re-seed the maximized-pane presentation on boot from a persisted id, but ONLY
// when it is still valid for the rehydrated workspace. Mirrors the runtime reset
// guards EXACTLY so a restored value can never desync the render (which reads the
// maximized geometry from this id but the active surface from ws.focusedPaneId):
//   - splits on + a multi-pane 'panes' world (single/immersive have no maximize),
//   - the pane still exists in the tree,
//   - and it IS the focused pane (the lockstep invariant toggle/reconcile keep).
// Any miss returns null → the workspace boots in its normal tiled view.
export function resolveInitialFocusedPaneView(ws, rawId) {
  if (rawId == null || !ws || !ws.panes) return null
  if (ws.viewMode === 'single') return null
  if (Object.keys(ws.panes).length <= 1) return null
  if (!ws.panes[rawId]) return null
  if (rawId !== ws.focusedPaneId) return null
  return rawId
}

// Forgiving read: any structural failure — bad JSON, wrong version, or an
// invariant that survives normalize (a too-deep/too-wide corrupt blob) — falls
// back to a fresh workspace. Never throws.
export function parseWorkspace(raw) {
  try {
    if (typeof raw !== 'string' || raw.length === 0) return seedFromFlatTabs([])
    const parsed = JSON.parse(raw)
    if (!parsed || parsed.v !== 1) return seedFromFlatTabs([])
    const normalized = normalize(parsed)
    if (!isValidWorkspace(normalized)) return seedFromFlatTabs([])
    return normalized
  } catch {
    return seedFromFlatTabs([])
  }
}

// The reducer's initial state: a workspace plus an empty single-slot undo.
export function initialWorkspaceState(ws) {
  return { ws, undo: null }
}

// Single-slot-undo reducer over the workspace. state = { ws, undo } where undo
// is { ws, label, toast } | null. `toast` is the 6s toast message this slot
// should surface (null = undoable but silent, e.g. a divider resize). A FRESH
// undo object is minted on every set, so its IDENTITY changing is the signal the
// UI binds its "Undo" toast to: when the slot is replaced or cleared, the old
// toast's Undo would revert a DIFFERENT mutation than it names, so the UI
// retracts/replaces it on any identity change (design §3.5 — a toast's Undo must
// never revert a mutation it does not describe).
//
// The slot only ever holds the IMMEDIATELY-preceding mutation. This is the
// invariant that keeps Undo honest: a snapshot carried across an intervening
// change would, when applied, silently roll that intervening change back too. So
// every action that changes the workspace either SETS the slot (it is the new
// undo target) or CLEARS it — never leaves a stale one in place.
//
//   Sets the slot (undoable):   CLOSE_TAB (user close), CLOSE_PANE, MOVE_TAB /
//                               edge-splits, OPEN_TAB_AT, SET_RATIO,
//                               APPLY_PLACEMENT.
//   Clears the slot on change:  SET_ACTIVE, FOCUS, OPEN_TAB, PRUNE,
//                               RESET_FLAT, and a CLOSE_TAB
//                               with reason:'deleted'.
//   Preserves the slot:         SET_VIEW_MODE — a pure view flip is ORTHOGONAL to
//                               the tree, so it neither creates nor clears an undo
//                               target (a pending tab-move stays undoable). (There is
//                               no longer a 'mode-convert' close: Settings tabs
//                               survive the flip, so nothing is removed on entry.)
//
// View-mode + undo: UNDO_LAST carries the CURRENT view-mode forward by default, so
// undoing a tree edit made across a standalone toggle reverts the edit, never the
// toggle. The ONE exception is a single-leaf split-drop: OPEN_TAB_AT folds the
// 'panes' flip into itself (action.flipViewMode) and marks the slot
// `restoreViewMode`, so undoing that one gesture reverts BOTH the split and the flip.
//
// PRUNE, RESET_FLAT, and reason:'deleted' close clear even when they change
// nothing, because the resource is gone and ANY older snapshot could resurrect
// it — the exact hazard the flat-strip model had no defense against.
// APPLY_PLACEMENT is undoable (agent rearrangements are reversible per design)
// and, crucially, takes a `resolve` FUNCTION (workspace → workspace) rather than
// a pre-resolved value: the reducer runs it against the CURRENT reducer state, so
// two placements dispatched in one React batch compose (the second sees the
// first, splits and all) instead of the second clobbering the first from a stale
// render snapshot. The pane-aware resolver lives in workspacePlacement.js and
// returns a normalized workspace. Returns the SAME state reference on a no-op.
export function workspaceReducer(state, action) {
  const { ws, undo } = state
  switch (action.type) {
    case 'OPEN_TAB': {
      const next = doOpenTab(ws, action.tab, {
        paneId: action.paneId,
        activate: action.activate,
      })
      if (next === ws) return state
      return { ws: next, undo: null }
    }
    case 'CLOSE_TAB': {
      const next = closeTab(ws, action.tabKey)
      if (action.reason === 'deleted') {
        // The backing chat/app was deleted; a snapshot here (or any older one)
        // would resurrect the tab without going through backend recovery. Clear.
        // Reconcile BOTH worlds atomically (two-worlds design, risk 3): if the
        // deleted item is also the single-screen slot, clear the slot in the same
        // action so a phantom slot can't point at a dead item. A deleted slot
        // degrades to the empty screen (null), never to builder focus.
        const withSlot = (singleScreenKey(ws) === action.tabKey && ws.singleScreen)
          ? setSingleScreen(next, null)
          : next
        if (withSlot === ws && undo == null) return state
        return { ws: withSlot, undo: null }
      }
      // User close (the strip ✕): reversible. If this close empties the builder,
      // AUTO-RETURN to single (owner semantic) and mark the undo restoreViewMode so
      // undo restores the closed tab AND builder mode as ONE gesture.
      if (next === ws) return state
      const closeLabel = action.label || 'Closed tab'
      const { ws: closed, autoReturned } = completeCloseTransition(ws, next)
      return { ws: closed, undo: { ws, label: closeLabel, toast: closeLabel, restoreViewMode: autoReturned } }
    }
    case 'CLOSE_PANE': {
      // Closing a pane closes all its tabs at once (a keyboard/menu affordance —
      // no per-tab clicks). Reversible: the snapshot restores the whole pane. An
      // auto-return applies here too (closing the sole/last pane empties builder).
      const next = closePane(ws, action.paneId)
      if (next === ws) return state
      const paneLabel = action.label || 'Closed pane'
      const { ws: closed, autoReturned } = completeCloseTransition(ws, next)
      return { ws: closed, undo: { ws, label: paneLabel, toast: paneLabel, restoreViewMode: autoReturned } }
    }
    case 'CLOSE_OTHER_TABS': {
      // Keep only the named tab in its pane. The kept tab survives, so the pane
      // never empties — no auto-return branch. One reversible gesture: the
      // snapshot restores every closed sibling at once.
      const next = closeOtherTabs(ws, action.tabKey)
      if (next === ws) return state
      const label = action.label || 'Closed other tabs'
      return { ws: next, undo: { ws, label, toast: label } }
    }
    case 'CLOSE_TABS_TO_RIGHT': {
      const next = closeTabsToRight(ws, action.tabKey)
      if (next === ws) return state
      const label = action.label || 'Closed tabs to the right'
      return { ws: next, undo: { ws, label, toast: label } }
    }
    case 'MOVE_TAB': {
      const next = moveTab(ws, action.tabKey, action.target)
      if (next === ws) return state
      const moveLabel = action.label || 'Moved tab'
      return { ws: next, undo: { ws, label: moveLabel, toast: moveLabel } }
    }
    case 'OPEN_TAB_AT': {
      // A drag drop from a drawer row (open the item AT the zone) or a strip tab
      // (degrades to a move). One commit, one undo slot — the drop is one tap
      // from repaired (design §3.5).
      // Prepare Standard's current screen as tab one before placing a drawer
      // drop into an empty hidden tree. Neither seed nor mode commits unless the
      // placement succeeds, so a rejected drop remains a true no-op.
      const working = action.flipViewMode === 'panes' ? seedEmptyBuilder(ws) : ws
      const placed = openTabAt(working, action.tab, action.target)
      // A rejected drop must not commit the prepared seed or its mode change.
      if (placed === working) return state
      // A single-leaf splitting drop made in single view-mode flips to 'panes' as
      // part of the SAME gesture (the drop's intent is a second visible surface).
      // Folding the flip into THIS action — rather than a following SET_VIEW_MODE —
      // keeps it one undo step: the slot is marked `restoreViewMode` so UNDO_LAST
      // reverts the mode along with the tree, never leaving the toggle reading the
      // flipped mode over a reverted tree. action.flipViewMode is null for every
      // ordinary drop, so this is a no-op there.
      const next = action.flipViewMode ? setViewMode(placed, action.flipViewMode) : placed
      if (next === ws) return state
      const dropLabel = action.label || 'Moved tab'
      return {
        ws: next,
        undo: { ws, label: dropLabel, toast: dropLabel, restoreViewMode: !!action.flipViewMode },
      }
    }
    case 'SET_ACTIVE': {
      const next = setActiveTab(ws, action.paneId, action.tabKey)
      return next === ws ? state : { ws: next, undo: null }
    }
    case 'FOCUS': {
      const next = focusPane(ws, action.paneId)
      return next === ws ? state : { ws: next, undo: null }
    }
    case 'SET_RATIO': {
      const next = setRatio(ws, action.splitId, action.ratio)
      if (next === ws) return state
      // Undoable via Cmd/Z but SILENT (toast:null) — a divider drag is a
      // continuous control, not a discrete "one tap from repaired" mutation.
      return { ws: next, undo: { ws, label: action.label || 'Resized', toast: null } }
    }
    case 'PRUNE': {
      const next = prune(ws, {
        liveChatIds: action.liveChatIds,
        liveAppIds: action.liveAppIds,
      })
      if (next === ws && undo == null) return state
      return { ws: next, undo: null }
    }
    case 'APPLY_PLACEMENT': {
      // resolve: (ws) => ws' — the pane-aware resolver (workspacePlacement.js)
      // run against the CURRENT reducer workspace so batched placements compose
      // instead of clobbering each other from a stale snapshot. It returns a
      // normalized workspace, the SAME reference on a no-op.
      const next = action.resolve(ws)
      if (next === ws) return state
      // An agent rearrangement is especially undoable AND must be announced
      // (design §3.5): it gets its OWN named toast rather than silently
      // overwriting a live drag's toast slot.
      return {
        ws: next,
        undo: { ws, label: 'Workspace placement', toast: action.toast || 'Agent arranged your workspace' },
      }
    }
    case 'SET_VIEW_MODE': {
      // A view transition is orthogonal to the undo slot. Entering Builder may
      // seed its empty hidden tree from Standard, but that seed is part of the
      // destination representation rather than a separate editable gesture.
      const flipped = action.mode === 'toggle' ? toggleViewMode(ws) : setViewMode(ws, action.mode)
      // On the FIRST-ever builder→single switch the slot is seeded from the focused
      // concrete item (two-worlds design: seed once, using property absence as the
      // migration marker). Every later switch leaves the slot exactly as the single
      // world last left it — builder work never rewrites the single screen.
      const next = flipped.viewMode === 'single' ? seedSingleScreenIfAbsent(flipped) : flipped
      if (next === ws) return state
      // INV 8 (review §3, P1): an explicit later mode intent REBASES a mode-COUPLED
      // undo to tree-only. A coupled undo (restoreViewMode: a single-leaf drop flip
      // or an empty-builder auto-return) would otherwise, when applied after the
      // owner has since toggled the mode, unconditionally restore the OLD mode and
      // override the owner's later choice. Dropping the flag here keeps the undo's
      // TREE restoration while leaving the mode as the owner last set it.
      const rebasedUndo = (undo && undo.restoreViewMode) ? { ...undo, restoreViewMode: false } : undo
      return { ws: next, undo: rebasedUndo }
    }
    case 'SET_SINGLE_SCREEN': {
      // The single world's ONE navigation write (two-worlds design): opening a
      // chat/app while in single mode sets the slot and NEVER touches the pane
      // tree. Orthogonal to the undo slot exactly like SET_VIEW_MODE — single-world
      // navigation preserves a pending builder undo. `item` is { kind, id } | null.
      const next = setSingleScreen(ws, action.item)
      if (next === ws) return state
      return { ws: next, undo }
    }
    case 'UNDO_LAST': {
      if (!undo) return state
      // Restore the captured tree. A slot flagged `restoreViewMode` reverts the
      // view-mode TOO — its gesture flipped the mode (a single-leaf split-drop), so
      // undoing the gesture must un-flip it (undo.ws already holds the pre-gesture
      // mode). Every OTHER slot carries the CURRENT view-mode forward, so undoing a
      // plain tree edit never yanks a single/panes toggle the user made afterward.
      // Reuse the snapshot reference when nothing needs rewriting.
      let restored
      if (undo.restoreViewMode) restored = undo.ws
      else restored = undo.ws.viewMode === ws.viewMode ? undo.ws : { ...undo.ws, viewMode: ws.viewMode }
      // Tree undo restores the tree/focus/mode but CARRIES FORWARD the current
      // single-screen slot (two-worlds design: tree undo must not resurrect an old
      // slot). The single→builder drag undo (restoreViewMode) equally preserves the
      // slot — its gesture only built the tree, it never changed the single world.
      // Reconcile the snapshot's slot to the CURRENT one unless they already match.
      const curSlot = ('singleScreen' in ws) ? ws.singleScreen : undefined
      const restSlot = ('singleScreen' in restored) ? restored.singleScreen : undefined
      if (!deepEqual(restSlot, curSlot)) {
        restored = ('singleScreen' in ws)
          ? { ...restored, singleScreen: ws.singleScreen }
          : restored
      }
      return { ws: restored, undo: null }
    }
    case 'RESET_FLAT': {
      const seeded = seedFromFlatTabs(action.tabs)
      // RESET_FLAT reseeds the BUILDER tree only (two-worlds design): preserve the
      // current world and single-screen slot unless the reset removed every tab,
      // in which case the shared no-empty-Builder invariant resolves to Standard.
      const candidate = { ...seeded, viewMode: ws.viewMode }
      if ('singleScreen' in ws) candidate.singleScreen = ws.singleScreen
      else if (candidate.viewMode === 'single') {
        // First boot of a legacy/flat active chat into Standard: the derivations no
        // longer borrow Builder focus, so seed the slot from the reset's focused item
        // or the surface renders the empty New-Chat home instead of that chat. Only
        // the genuine uninitialized case (no slot on the current ws) seeds here; a
        // later toggle/close still owns the slot via seedSingleScreenIfAbsent /
        // completeCloseTransition, and an explicit null slot stays New Chat.
        const seed = focusedSlotSeed(seeded)
        if (seed) candidate.singleScreen = seed
      }
      const next = normalize(candidate)
      if (deepEqual(next, ws) && undo == null) return state
      return { ws: next, undo: null }
    }
    default:
      return state
  }
}
