// Pane model — the tiled-workspace successor of the flat tab strip.
//
// tabModel.js owns a single flat open set and computes active-ness against one
// global nav focus. This module generalizes that to a binary split tree of
// panes: each pane holds its own set of tabModel tabs plus its own active tab,
// and the workspace tracks which pane has focus. A single pane is the trivial
// leaf, so today's shell is the degenerate one-pane form of this model — see
// ARCHITECTURE.md's "Multi-pane workspace" section for the seams and the
// scroll/nav constraints migration must honor, and
// docs/design/split-pane-workspace.md for the full v1 design (§1 is this file's
// spec: the state shape, the normalize() invariants, and the reducer contract).
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

// A pane keeps at most this many tabs; it is the per-pane successor of
// tabModel.MAX_TABS (whose comment reserved exactly this). Only openTab enforces
// it, and it evicts a background tab rather than blocking the open (§3.6).
export const MAX_PANE_TABS = 6

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
// PANE_GAP sits between two sibling panes and is where a divider is drawn;
// OUTER_MARGIN insets the whole tiled area from the content-box edge.
export const PANE_GAP = 7
export const OUTER_MARGIN = 8

// Height of a per-pane tab strip. A pane's CONTENT rect is its pane rect minus
// this strip row (design §2). The renderer and the divider drag both subtract
// it, so it lives here as the single source of truth.
export const STRIP_H = 34

// Multi-pane exposure waits for the pane-aware back/sentinel work (stage B).
// Until then every user ENTRY POINT into splits — the context-menu split/move
// items and the phone pane chip/sheet — is ON by default now that the PR2
// gates (unit + positive-behavior e2e) are green; 'mobius:workspace-splits'
// = '0' is the kill switch that restores the single-pane fallback. Read once
// at module load; absent-localStorage runtimes get the default (enabled).
export const WORKSPACE_SPLITS_ENABLED = (() => {
  try { return localStorage.getItem('mobius:workspace-splits') !== '0' } catch { return true }
})()

// The smallest a pane may be. canSplit refuses a split whose either resulting
// child would fall below this within the pane's current projected rect — the
// shared feasibility predicate drag/menu/resolver all consult (design §3.2,
// §6.2). projectLayout clamps ratios against the same minimums at render time.
export const MIN_PANE_W = 280
export const MIN_PANE_H = 200

// sessionStorage key for the serialized workspace; the legacy flat key
// (tabModel's 'mobius-open-tabs') is dual-written for one release so a rollback
// still finds its tabs.
export const STORAGE_KEY = 'mobius-workspace'

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

// Keep only the last MAX_PANE_TABS tabs — the legacy readOpenTabs posture, so a
// pane never persists over its cap.
function capTabs(tabs) {
  return tabs.length > MAX_PANE_TABS ? tabs.slice(tabs.length - MAX_PANE_TABS) : tabs
}

// Ratio is a's fraction, stored clamped to [0.1, 0.9]; a non-finite ratio is a
// degenerate split and resets to an even 0.5 (px minimums are a render concern).
function clampRatio(ratio) {
  const n = Number(ratio)
  if (!Number.isFinite(n)) return 0.5
  return Math.min(0.9, Math.max(0.1, n))
}

// Coerce one raw tab to a canonical tabModel tab, or null if it can't be a live
// tab: this is tabModel.readOpenTabs's posture — drop unknown kinds, missing
// ids, and app ids that aren't finite numbers (they would become NaN in
// tabNavTarget and never resolve).
function sanitizeTab(raw) {
  if (!raw || (raw.kind !== 'chat' && raw.kind !== 'app') || raw.id == null) return null
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

  // Recompute the id generator from the tree itself rather than trusting the
  // stored value: a persisted `nextId` that lags the live ids (a corrupt or
  // rolled-back blob) would mint a colliding `pN`/`sN`, overwrite the existing
  // node, and lose its tab when the duplicate leaf collapses. Deterministic
  // repair: one past the largest pane/split suffix in play.
  let maxId = 0
  for (const id of Object.keys(panes)) maxId = Math.max(maxId, idSuffix(id))
  for (const id of splitIdsOf(layout)) maxId = Math.max(maxId, idSuffix(id))
  const nextId = maxId + 1
  const result = { v: 1, layout, panes, focusedPaneId: focused, nextId }
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

// Seed a single-pane workspace from today's flat open set. Tabs are sanitized,
// deduped, and capped to the last MAX_PANE_TABS (legacy readOpenTabs posture);
// the last tab is active (the on-screen one under the flat model).
export function seedFromFlatTabs(tabs) {
  const clean = capTabs(dedupTabs(sanitizeTabs(tabs)))
  const paneId = 'p0'
  const keys = clean.map(tabModel.tabKey)
  return {
    v: 1,
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
      const { view, opts } = tabModel.tabNavTarget(tab)
      if (view === 'canvas') return { view: 'canvas', chatId: null, appId: opts.appId, paneId }
      return { view: 'chat', chatId: opts.chatId, appId: null, paneId }
    }
  }
  return { view: 'chat', chatId: null, appId: null, paneId }
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

// Open a tab and report whether the open forced an eviction (the reducer needs
// that to decide undoability — an evicting open is undoable, a plain one is not).
function doOpenTab(ws, tab, { paneId, activate = true } = {}) {
  const clean = sanitizeTab(tab)
  if (!clean) return { ws, evicted: null }
  const key = tabModel.tabKey(clean)

  // Already open anywhere: focus that pane and (if activating) make it active —
  // never duplicate, since a tab is unique workspace-wide.
  const existing = paneOf(ws, key)
  if (existing) {
    const candidate = {
      ...ws,
      focusedPaneId: existing.id,
      panes: {
        ...ws.panes,
        [existing.id]: {
          ...existing,
          activeTabKey: activate ? key : existing.activeTabKey,
        },
      },
    }
    return { ws: commit(ws, candidate), evicted: null }
  }

  const targetId = (paneId && ws.panes[paneId]) ? paneId : ws.focusedPaneId
  const target = ws.panes[targetId]
  if (!target) return { ws, evicted: null }

  let tabs = target.tabs
  let evicted = null
  if (tabs.length >= MAX_PANE_TABS) {
    // Legacy parity — PR1's gate is "no visible strip change." tabModel.addTab
    // dropped the OLDEST tab unconditionally, so we do the same: no active-tab
    // protection here. The design §3.6 protected + named + undoable eviction
    // (which needs a toast to name what left) ships with PR3's toast UI. The
    // evicting open still snapshots undo (see the reducer), so a future toast's
    // Undo already has its restore point.
    evicted = tabs[0]
    tabs = tabs.slice(1)
  }
  tabs = [...tabs, clean]
  const candidate = {
    ...ws,
    focusedPaneId: targetId,
    panes: {
      ...ws.panes,
      [targetId]: {
        ...target,
        tabs,
        activeTabKey: activate ? key : target.activeTabKey,
      },
    },
  }
  return { ws: commit(ws, candidate), evicted }
}

export function openTab(ws, tab, opts) {
  return doOpenTab(ws, tab, opts).ws
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
  // A cross-pane move into a pane already at its cap is refused (same-reference
  // no-op) — MAX_PANE_TABS is an invariant, and unlike an open there is no
  // "evict to make room" contract for a drag. A same-pane reorder is exempt: it
  // does not change the count.
  if (destId !== src.id && dest.tabs.length >= MAX_PANE_TABS) return ws
  const panes = { ...ws.panes }
  const srcTabs = src.tabs.filter(t => tabModel.tabKey(t) !== tabKey)
  const base = destId === src.id
    ? srcTabs
    : dest.tabs.filter(t => tabModel.tabKey(t) !== tabKey)
  const at = (index == null || index < 0 || index > base.length) ? base.length : index
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

// Drop tabs whose backing chat/app is no longer live, then normalize (an emptied
// pane collapses). Used when a chat/app is deleted out of band.
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
  if (!changed) return ws
  return commit(ws, { ...ws, panes })
}

// In-order walk of every tab across every pane — the flat projection today's
// strip renders and the legacy dual-write's ordering baseline.
export function flatten(ws) {
  const out = []
  for (const id of leafIds(ws.layout)) {
    const pane = ws.panes[id]
    if (pane) out.push(...pane.tabs)
  }
  return out
}

// Rollback ordering for the legacy 'mobius-open-tabs' dual-write. Legacy
// readOpenTabs keeps the LAST MAX_TABS, so the most relevant tabs must come
// last: every background (non-focused) pane's tabs first, then the focused
// pane's other tabs, then its active tab dead last so a rollback keeps it.
export function flattenRollbackPriority(ws) {
  const out = []
  const focusedId = ws.focusedPaneId
  for (const id of leafIds(ws.layout)) {
    if (id === focusedId) continue
    const pane = ws.panes[id]
    if (pane) out.push(...pane.tabs)
  }
  const focused = ws.panes[focusedId]
  if (focused) {
    const active = focused.activeTabKey
    for (const tab of focused.tabs) {
      if (tabModel.tabKey(tab) !== active) out.push(tab)
    }
    const activeTab = focused.tabs.find(tab => tabModel.tabKey(tab) === active)
    if (activeTab) out.push(activeTab)
  }
  return out
}

// The on-screen tab of every leaf pane, in-order. PR1 has one pane, but the
// renderer (PR2) walks all visible panes the same way.
export function visibleTabs(ws) {
  const out = []
  for (const id of leafIds(ws.layout)) {
    const pane = ws.panes[id]
    if (!pane) continue
    const active = pane.tabs.find(tab => tabModel.tabKey(tab) === pane.activeTabKey)
    if (active) out.push(active)
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

// Inset a rect by m on every side — the tiled area's outer margin.
function insetRect(rect, m) {
  return {
    x: rect.x + m,
    y: rect.y + m,
    w: Math.max(0, rect.w - 2 * m),
    h: Math.max(0, rect.h - 2 * m),
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
// - 'wide'    → all leaves, full tree walk, gap + outer margin, a divider per
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
  const box = insetRect(content, OUTER_MARGIN)

  if (mode === 'wide') {
    const rects = {}
    const dividers = []
    layoutTree(tree, box, rects, dividers)
    return {
      visibleLeaves: leafIds(tree).filter(id => ws.panes[id]),
      rects,
      dividers,
    }
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

  if (mode === 'compact') {
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
  // Going 1 leaf → 2 introduces the outer margin the single-pane projection
  // omits (it returns the full content rect). Judge feasibility against the
  // POST-split usable box, else a first split is offered that then lands
  // children below the minimum once the inset applies (finding E-ii).
  if (projected.visibleLeaves.length <= 1) rect = insetRect(content, OUTER_MARGIN)
  const row = edge === 'left' || edge === 'right'
  if (row) {
    const childW = (rect.w - PANE_GAP) / 2
    return childW >= MIN_PANE_W && rect.h >= MIN_PANE_H
  }
  const childH = (rect.h - PANE_GAP) / 2
  return rect.w >= MIN_PANE_W && childH >= MIN_PANE_H
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
    if (pane.tabs.length > MAX_PANE_TABS) return false
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

// The raw stored blob, or null if storage is unavailable. sessionStorage.getItem
// can THROW (SecurityError in a sandboxed frame, disabled storage, a privacy
// policy), and the caller reads it while evaluating parseWorkspace's argument —
// outside parseWorkspace's own try/catch. Guarding the read here (the
// tabModel.readOpenTabs posture) keeps a broken storage from taking down boot.
export function readWorkspaceRaw(storage) {
  try {
    return storage.getItem(STORAGE_KEY)
  } catch {
    return null
  }
}

// Forgiving read: any structural failure — bad JSON, wrong version, or an
// invariant that survives normalize (a too-deep/too-wide corrupt blob) — falls
// back to a fresh flat seed. Never throws.
export function parseWorkspace(raw, { fallbackTabs = [] } = {}) {
  try {
    if (typeof raw !== 'string' || raw.length === 0) return seedFromFlatTabs(fallbackTabs)
    const parsed = JSON.parse(raw)
    if (!parsed || parsed.v !== 1) return seedFromFlatTabs(fallbackTabs)
    const normalized = normalize(parsed)
    if (!isValidWorkspace(normalized)) return seedFromFlatTabs(fallbackTabs)
    return normalized
  } catch {
    return seedFromFlatTabs(fallbackTabs)
  }
}

// Replace the focused pane's tabs with a flat resolver output — the PR1 bridge
// that keeps applyWorkspaceRequestsToFlatTabs (workspacePlacement.js) behind the
// existing placeInWorkspace seam. The pane's active tab is preserved when it is
// still in the list, else it falls to the last tab.
function applyFlat(ws, tabs) {
  const pane = ws.panes[ws.focusedPaneId]
  if (!pane) return ws
  // Cap defensively so a placement can never persist an over-cap pane; the flat
  // resolver already trims to tabModel.MAX_TABS, so this is a belt-and-braces
  // guard on the invariant, not a behavior change.
  const clean = capTabs(dedupTabs(sanitizeTabs(tabs)))
  const keys = clean.map(tabModel.tabKey)
  const active = (pane.activeTabKey != null && keys.includes(pane.activeTabKey))
    ? pane.activeTabKey
    : (keys.length ? keys[keys.length - 1] : null)
  return commit(ws, {
    ...ws,
    panes: { ...ws.panes, [pane.id]: { ...pane, tabs: clean, activeTabKey: active } },
  })
}

// The reducer's initial state: a workspace plus an empty single-slot undo.
export function initialWorkspaceState(ws) {
  return { ws, undo: null }
}

// Single-slot-undo reducer over the workspace. state = { ws, undo } where undo
// is { ws, label } | null.
//
// The slot only ever holds the IMMEDIATELY-preceding mutation. This is the
// invariant that keeps Undo honest: a snapshot carried across an intervening
// change would, when applied, silently roll that intervening change back too. So
// every action that changes the workspace either SETS the slot (it is the new
// undo target) or CLEARS it — never leaves a stale one in place.
//
//   Sets the slot (undoable):   CLOSE_TAB (user close), MOVE_TAB / edge-splits,
//                               an OPEN_TAB that evicted, SET_RATIO,
//                               APPLY_PLACEMENT.
//   Clears the slot on change:  SET_ACTIVE, FOCUS, a plain (non-evicting)
//                               OPEN_TAB, PRUNE, RESET_FLAT, and a CLOSE_TAB
//                               with reason:'deleted'.
//
// PRUNE, RESET_FLAT, and reason:'deleted' close clear even when they change
// nothing, because the resource is gone and ANY older snapshot could resurrect
// it — the exact hazard the flat-strip model had no defense against.
// APPLY_PLACEMENT is undoable (agent rearrangements are reversible per design)
// and, crucially, takes a `resolve` FUNCTION rather than a pre-resolved array:
// the reducer runs it against the CURRENT reducer state, so two placements
// dispatched in one React batch compose (the second sees the first) instead of
// the second clobbering the first from a stale render snapshot. Returns the SAME
// state reference on a no-op.
export function workspaceReducer(state, action) {
  const { ws, undo } = state
  switch (action.type) {
    case 'OPEN_TAB': {
      const { ws: next, evicted } = doOpenTab(ws, action.tab, {
        paneId: action.paneId,
        activate: action.activate,
      })
      if (next === ws) return state
      // Only an evicting open is undoable (its snapshot restores the evicted
      // tab); a plain open clears the slot like any other non-undoable change.
      return evicted
        ? { ws: next, undo: { ws, label: 'Opened tab' } }
        : { ws: next, undo: null }
    }
    case 'CLOSE_TAB': {
      const next = closeTab(ws, action.tabKey)
      if (action.reason === 'deleted') {
        // The backing chat/app was deleted; a snapshot here (or any older one)
        // would resurrect the tab without going through backend recovery. Clear.
        if (next === ws && undo == null) return state
        return { ws: next, undo: null }
      }
      // User close (the strip ✕): reversible.
      if (next === ws) return state
      return { ws: next, undo: { ws, label: action.label || 'Closed tab' } }
    }
    case 'MOVE_TAB': {
      const next = moveTab(ws, action.tabKey, action.target)
      if (next === ws) return state
      return { ws: next, undo: { ws, label: action.label || 'Moved tab' } }
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
      return { ws: next, undo: { ws, label: action.label || 'Resized' } }
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
      // resolve: (flatTabs) => flatTabs' — run against the CURRENT flat tabs so
      // batched placements compose instead of clobbering each other.
      const next = applyFlat(ws, action.resolve(flatten(ws)))
      if (next === ws) return state
      return { ws: next, undo: { ws, label: 'Workspace placement' } }
    }
    case 'UNDO_LAST': {
      if (!undo) return state
      return { ws: undo.ws, undo: null }
    }
    case 'RESET_FLAT': {
      const next = seedFromFlatTabs(action.tabs)
      if (deepEqual(next, ws) && undo == null) return state
      return { ws: next, undo: null }
    }
    default:
      return state
  }
}
