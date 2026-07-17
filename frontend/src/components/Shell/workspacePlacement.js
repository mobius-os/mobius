import * as paneModel from './paneModel.js'
import { makeTab, tabKey } from './tabModel.js'

// The producer vocabulary (design §6.1): a request names VALUES — what to open,
// its relational source, an intent placement, and an activation — never geometry
// (no split direction, pane id, ratio, or breakpoint). The device-aware resolver
// below owns what "beside" means on this screen today.
export const WORKSPACE_OPEN_ITEM = 'open-item'
export const PLACE_BESIDE_SOURCE = 'beside-source'
export const PLACE_WITH_SOURCE = 'with-source'
export const PLACE_WITH_FOCUS = 'with-focus'
export const ACTIVATE_IN_BACKGROUND = 'background'
export const ACTIVATE_FOREGROUND = 'foreground'

const PLACEMENTS = new Set([PLACE_BESIDE_SOURCE, PLACE_WITH_SOURCE, PLACE_WITH_FOCUS])
const ACTIVATIONS = new Set([ACTIVATE_IN_BACKGROUND, ACTIVATE_FOREGROUND])

// Build completion expresses product intent without naming a tab strip, pane,
// split direction, or breakpoint. The resolver interprets `beside-source` as a
// split when the device supports one, a companion tab when a related app pane is
// already open, or a background tab on a phone — without changing producers.
export function builtAppWorkspaceRequest(chatId, appId) {
  const normalizedAppId = Number(appId)
  if (
    chatId == null
    || String(chatId).length === 0
    || !Number.isInteger(normalizedAppId)
    || normalizedAppId <= 0
  ) return null

  return {
    type: WORKSPACE_OPEN_ITEM,
    item: makeTab('app', normalizedAppId),
    source: makeTab('chat', chatId),
    placement: PLACE_BESIDE_SOURCE,
    activation: ACTIVATE_IN_BACKGROUND,
    reason: 'chat-built-app',
  }
}

// Map an explicit `open_item` system event (the agent's typed POST /api/notify,
// design §6.3) to a request. Unknown item kinds / ids are dropped (silent no-op);
// an app id must be numeric (the tabNavTarget posture). A malformed or absent
// source is simply omitted — the resolver then degrades to `with-focus`. Missing
// placement/activation default to a background beside-source when a source is
// present, else a background with-focus (the safe, non-focus-stealing default).
export function openItemWorkspaceRequest(event) {
  const kind = event?.itemKind
  if (kind !== 'app' && kind !== 'chat') return null
  const rawId = event?.itemId
  if (rawId == null || String(rawId).length === 0) return null
  if (kind === 'app' && !Number.isInteger(Number(rawId))) return null
  const item = makeTab(kind, kind === 'app' ? Number(rawId) : rawId)

  let source = null
  const sKind = event?.sourceKind
  const sId = event?.sourceId
  if ((sKind === 'chat' || sKind === 'app') && sId != null && String(sId).length > 0) {
    if (sKind === 'app' && !Number.isInteger(Number(sId))) return null
    source = makeTab(sKind, sKind === 'app' ? Number(sId) : sId)
  }

  const placement = PLACEMENTS.has(event?.placement)
    ? event.placement
    : (source ? PLACE_BESIDE_SOURCE : PLACE_WITH_FOCUS)
  const activation = ACTIVATIONS.has(event?.activation)
    ? event.activation
    : ACTIVATE_IN_BACKGROUND

  return {
    type: WORKSPACE_OPEN_ITEM,
    item,
    source,
    placement,
    activation,
    reason: 'agent-open-item',
  }
}

export function workspaceRequestFromSystemEvent(event) {
  if (event?.type === 'app_created') return builtAppWorkspaceRequest(event.chatId, event.appId)
  if (event?.type === 'open_item') return openItemWorkspaceRequest(event)
  return null
}

export function workspaceRequestsForBuiltApps(arrivals) {
  const requests = []
  for (const arrival of arrivals || []) {
    const request = builtAppWorkspaceRequest(arrival?.chatId, arrival?.appId)
    if (request) requests.push(request)
  }
  return requests
}

// ── The pane-aware resolver (design §6.2) ───────────────────────────────────
//
// resolveWorkspaceRequest(ws, request, env) → a NEW normalized workspace (or the
// SAME reference on a no-op). Pure. It sits behind the unchanged placeInWorkspace
// seam: producers keep emitting intent, and this turns intent into geometry for
// the partner's device today, reusing the SAME canSplit min-size predicate the
// drag/menu layer uses so it can never request a split the UI itself would refuse.
//
// env = {
//   mode:        'phone' | 'compact' | 'wide'  (paneModel.modeForRect),
//   projected:   paneModel.projectLayout(ws, mode, contentRect)  (pane rects),
//   contentRect: { w, h }  (the same rect projected/canSplit read),
//   liveApps:    the /api/apps rows — for the companion-pane chat_id derivation,
// }

// A well-formed open-item request; anything else is a silent no-op so a producer
// may emit a v2 shape before this resolver understands it (the forward-compat rule).
function isOpenItemRequest(request) {
  if (!request || request.type !== WORKSPACE_OPEN_ITEM) return false
  const item = request.item
  if (!item || (item.kind !== 'chat' && item.kind !== 'app')) return false
  if (item.id == null || String(item.id).length === 0) return false
  if (item.kind === 'app' && !Number.isFinite(Number(item.id))) return false
  if (!PLACEMENTS.has(request.placement) || !ACTIVATIONS.has(request.activation)) return false
  const s = request.source
  if (s != null) {
    if (s.kind !== 'chat' && s.kind !== 'app') return false
    if (s.id == null || String(s.id).length === 0) return false
  }
  return true
}

// The eviction-protection set for placing into an existing pane: every visible
// pane's on-screen tab (background work must never make an on-screen tab vanish —
// ARCHITECTURE.md's protect-the-on-screen-tab rule), plus the source and item.
function protectKeys(ws, extraTabs) {
  const keys = new Set()
  for (const paneId of paneModel.paneIdsInOrder(ws)) {
    const active = ws.panes[paneId]?.activeTabKey
    if (active) keys.add(active)
  }
  for (const tab of extraTabs || []) if (tab) keys.add(tabKey(tab))
  return keys
}

// Insert the item as a tab directly after its source in the source's pane. The
// activation controls whether it takes the pane's active slot and focus — a
// background insert leaves both untouched (design §6.2: no on-screen switch).
function insertBesideSource(ws, item, sourcePane, source, foreground) {
  return paneModel.openTab(ws, item, {
    paneId: sourcePane.id,
    afterKey: tabKey(source),
    activate: foreground,
    focus: foreground,
    protect: protectKeys(ws, [source, item]),
  })
}

// The split edge for a source pane's auto-split: the LONGER feasible axis first
// (a wider pane splits left|right, a taller one top|bottom), degrading to the
// other axis, then null when neither clears MAX_PANES / MAX_DEPTH / min-size. The
// new pane sits on the trailing side so the preview blooms beside/below the source.
function chooseSplitEdge(ws, paneId, env) {
  const rect = env.projected?.rects?.[paneId] || env.contentRect || {}
  const wider = (Number(rect.w) || 0) >= (Number(rect.h) || 0)
  const order = wider ? ['right', 'bottom'] : ['bottom', 'right']
  for (const edge of order) {
    if (paneModel.canSplit(ws, paneId, edge, env.mode, env.contentRect)) return edge
  }
  return null
}

// The companion pane for a chat source: the first pane (in leaf order) holding an
// app whose SERVER chat_id equals the source chat (design §6.2 — derived from the
// live app list, no schema field). Only a chat source has a companion.
function companionPaneFor(ws, source, liveApps) {
  if (!source || source.kind !== 'chat' || !Array.isArray(liveApps)) return null
  const chatAppIds = new Set()
  for (const app of liveApps) {
    if (app && app.chat_id != null && String(app.chat_id) === String(source.id)) {
      chatAppIds.add(String(app.id))
    }
  }
  if (chatAppIds.size === 0) return null
  for (const paneId of paneModel.paneIdsInOrder(ws)) {
    const pane = ws.panes[paneId]
    if (pane && pane.tabs.some(t => t.kind === 'app' && chatAppIds.has(String(t.id)))) {
      return pane
    }
  }
  return null
}

export function resolveWorkspaceRequest(ws, request, env = {}) {
  if (!isOpenItemRequest(request)) return ws
  const { item, source, placement, activation } = request
  const foreground = activation === ACTIVATE_FOREGROUND
  const itemKey = tabKey(item)
  const mode = env.mode || 'wide'

  // Already open anywhere → no-op; a foreground request focuses its pane + tab.
  const existing = paneModel.paneOf(ws, itemKey)
  if (existing) {
    if (!foreground) return ws
    return paneModel.focusPane(
      paneModel.setActiveTab(ws, existing.id, itemKey), existing.id,
    )
  }

  const sourcePane = source ? paneModel.paneOf(ws, tabKey(source)) : null

  // with-focus, or a source we cannot resolve (absent, or its tab isn't open) →
  // append to the focused pane (design: source missing/closed degrades to with-focus).
  if (placement === PLACE_WITH_FOCUS || !source || !sourcePane) {
    return paneModel.openTab(ws, item, {
      paneId: ws.focusedPaneId,
      activate: foreground,
      focus: foreground,
      protect: protectKeys(ws, [item]),
    })
  }

  // with-source → a tab in the source's pane, every mode (activate iff foreground).
  if (placement === PLACE_WITH_SOURCE) {
    return insertBesideSource(ws, item, sourcePane, source, foreground)
  }

  // beside-source, the device-aware table.
  if (mode === 'phone') {
    // Phone stack: a tab after the source in its pane (byte-identical to today).
    return insertBesideSource(ws, item, sourcePane, source, foreground)
  }

  const paneCount = paneModel.paneIdsInOrder(ws).length
  if (paneCount <= 1) {
    // Tile, single pane: auto-split the source pane on its longer feasible axis;
    // the item is active in the new pane, focus stays put unless foreground.
    // Infeasible split → degrade to a background/companion tab beside the source.
    const edge = chooseSplitEdge(ws, sourcePane.id, env)
    if (edge) return paneModel.splitPaneWithTab(ws, item, { paneId: sourcePane.id, edge, focus: foreground })
    return insertBesideSource(ws, item, sourcePane, source, foreground)
  }

  // Tile, multi-pane: companion pane → else split the source pane → else a
  // background tab beside the source (the degradation ladder, design §6.2).
  const companion = companionPaneFor(ws, source, env.liveApps)
  if (companion) {
    // Background must NOT switch the companion pane's visible content, so only a
    // foreground request activates + focuses it.
    return paneModel.openTab(ws, item, {
      paneId: companion.id,
      activate: foreground,
      focus: foreground,
      protect: protectKeys(ws, [source, item]),
    })
  }
  const edge = chooseSplitEdge(ws, sourcePane.id, env)
  if (edge) return paneModel.splitPaneWithTab(ws, item, { paneId: sourcePane.id, edge, focus: foreground })
  return insertBesideSource(ws, item, sourcePane, source, foreground)
}
