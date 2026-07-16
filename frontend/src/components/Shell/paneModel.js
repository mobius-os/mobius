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

// A pane keeps at most this many tabs; it is the per-pane successor of
// tabModel.MAX_TABS (whose comment reserved exactly this). Only openTab enforces
// it, and it evicts a background tab rather than blocking the open (§3.6).
export const MAX_PANE_TABS = 6

// At most four leaf panes, nested at most two splits deep (a balanced binary
// tree of depth 2 has four leaves). moveTab refuses to cross either bound.
export const MAX_PANES = 4
export const MAX_DEPTH = 2

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

  const nextId = Number.isInteger(ws.nextId) && ws.nextId > 0 ? ws.nextId : 1
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

// Seed a single-pane workspace from today's flat open set. Tabs are sanitized
// and deduped; the last tab is active (the on-screen one under the flat model).
export function seedFromFlatTabs(tabs) {
  const clean = dedupTabs(sanitizeTabs(tabs))
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
    // Evict the oldest tab that is neither the newcomer (not present yet) nor
    // the pane's protected active tab — the primary open never silently fails.
    const victimIdx = tabs.findIndex(t => tabModel.tabKey(t) !== target.activeTabKey)
    if (victimIdx !== -1) {
      evicted = tabs[victimIdx]
      tabs = tabs.filter((_, i) => i !== victimIdx)
    }
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

  if (target.root === true && target.edge != null) {
    return rootSplitMove(ws, src, tab, tabKey, target.edge)
  }
  if (target.paneId != null && target.edge != null) {
    return edgeSplitMove(ws, src, tab, tabKey, target.paneId, target.edge)
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

// A workspace that survives every invariant with no repair left to do — the gate
// parseWorkspace applies after normalize to catch what normalize won't rebalance
// (too deep, too wide) and fall back rather than serve a broken tree.
function isValidWorkspace(ws) {
  if (!ws || ws.v !== 1 || ws.layout == null || !ws.panes) return false
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
  const clean = dedupTabs(sanitizeTabs(tabs))
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
// is { ws, label } | null. The undoable set is enumerated, not implied (§1):
// MOVE_TAB, edge-splits (also MOVE_TAB), CLOSE_TAB, an OPEN_TAB that evicted, and
// SET_RATIO all snapshot the pre-action workspace into the slot. SET_ACTIVE,
// FOCUS, a plain OPEN_TAB, and APPLY_FLAT change the workspace but carry the
// existing slot forward untouched. PRUNE and RESET_FLAT clear the slot so Cmd/Z
// can never resurrect a deleted chat/app. APPLY_FLAT is deliberately NOT
// undoable: the design lists APPLY_PLACEMENT as undoable, but the PR1 flat bridge
// mirrors today's non-undoable automatic placement. Returns the SAME state
// reference on a no-op.
export function workspaceReducer(state, action) {
  const { ws, undo } = state
  switch (action.type) {
    case 'OPEN_TAB': {
      const { ws: next, evicted } = doOpenTab(ws, action.tab, {
        paneId: action.paneId,
        activate: action.activate,
      })
      if (next === ws) return state
      return evicted
        ? { ws: next, undo: { ws, label: 'Opened tab' } }
        : { ws: next, undo }
    }
    case 'CLOSE_TAB': {
      const next = closeTab(ws, action.tabKey)
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
      return next === ws ? state : { ws: next, undo }
    }
    case 'FOCUS': {
      const next = focusPane(ws, action.paneId)
      return next === ws ? state : { ws: next, undo }
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
    case 'APPLY_FLAT': {
      const next = applyFlat(ws, action.tabs)
      return next === ws ? state : { ws: next, undo }
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
