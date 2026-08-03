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
// A live build preview is non-displacing on every device: it may reveal or
// switch the app in a companion pane, but it never replaces the active tab in
// the pane that owns keyboard focus. This is an internal lifecycle intent, not
// an `open_item` wire value.
export const ACTIVATE_LIVE_PREVIEW = 'live-preview'

const PLACEMENTS = new Set([PLACE_BESIDE_SOURCE, PLACE_WITH_SOURCE, PLACE_WITH_FOCUS])
const OPEN_ITEM_ACTIVATIONS = new Set([ACTIVATE_IN_BACKGROUND, ACTIVATE_FOREGROUND])
const REQUEST_ACTIVATIONS = new Set([
  ACTIVATE_IN_BACKGROUND,
  ACTIVATE_FOREGROUND,
  ACTIVATE_LIVE_PREVIEW,
])

// A runnable build preview expresses product intent without naming a tab strip,
// pane, split direction, or breakpoint. The resolver interprets `beside-source`
// as a visible companion pane only when it can add one without hiding anything;
// otherwise it parks the app beside its source. Subsequent app_updated events
// still live-swap an app that is already visible.
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
    activation: ACTIVATE_LIVE_PREVIEW,
    reason: 'chat-built-app',
  }
}

// Map an explicit `open_item` system event (the agent's typed POST /api/notify,
// design §6.3) to a request. Unknown item kinds / ids are dropped (silent no-op);
// an app id must be numeric (the tabNavTarget posture). A malformed source (bad
// kind, empty or non-numeric app id) is OMITTED, not fatal — the resolver then
// degrades to `with-focus`. An OMITTED placement/activation defaults (background
// beside-source when a source is present, else background with-focus), but a
// PRESENT-but-unrecognized value is a silent no-op (returns null): a newer
// backend may emit a v2 value that a cached older shell must skip, never coerce
// to a wrong default (the forward-compat rule; the co-deployed backend's 422 is
// the wire contract, this no-op is for in-image evolution).
export function openItemWorkspaceRequest(event) {
  const kind = event?.itemKind
  if (kind !== 'app' && kind !== 'chat') return null
  const rawId = event?.itemId
  if (rawId == null || String(rawId).length === 0) return null
  if (kind === 'app' && !Number.isInteger(Number(rawId))) return null
  const item = makeTab(kind, kind === 'app' ? Number(rawId) : rawId)

  // A present-but-unknown placement/activation is a forward-compat no-op.
  if (event?.placement != null && !PLACEMENTS.has(event.placement)) return null
  if (event?.activation != null && !OPEN_ITEM_ACTIVATIONS.has(event.activation)) return null

  // A well-formed source only; anything malformed is omitted (degrade to with-focus).
  let source = null
  const sKind = event?.sourceKind
  const sId = event?.sourceId
  if ((sKind === 'chat' || sKind === 'app')
      && sId != null && String(sId).length > 0
      && !(sKind === 'app' && !Number.isInteger(Number(sId)))) {
    source = makeTab(sKind, sKind === 'app' ? Number(sId) : sId)
  }

  const placement = event?.placement
    ?? (source ? PLACE_BESIDE_SOURCE : PLACE_WITH_FOCUS)
  const activation = event?.activation ?? ACTIVATE_IN_BACKGROUND

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
  if (event?.type === 'app_preview_ready') {
    return builtAppWorkspaceRequest(event.chatId, event.appId)
  }
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

// The background attention target for a placement (design §6.2): a background
// open lands as an inactive tab, so it earns the drawer/tab "new content" dot —
// an app flags into the newAppIds set, a chat into attentionChatIds. A FOREGROUND
// open is on screen and needs no dot. Returns {kind, id} to flag, or null. Kept
// pure here so the Shell wiring (which owns the setters) stays a thin dispatch.
export function attentionForRequest(request) {
  if (!request || request.activation !== ACTIVATE_IN_BACKGROUND) return null
  const item = request.item
  if (!item) return null
  if (item.kind === 'app') return { kind: 'app', id: Number(item.id) }
  if (item.kind === 'chat') return { kind: 'chat', id: item.id }
  return null
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
  if (!PLACEMENTS.has(request.placement) || !REQUEST_ACTIVATIONS.has(request.activation)) return false
  const s = request.source
  if (s != null) {
    if (s.kind !== 'chat' && s.kind !== 'app') return false
    if (s.id == null || String(s.id).length === 0) return false
  }
  return true
}

// The on-screen content of a workspace in a given mode: the active tab key of
// every leaf the projection actually shows (wide shows all leaves; compact/phone
// show a limited pair). This is what the user sees — the level the background
// guarantee must hold at, not just per-pane activeTabKey.
function visibleContentKeys(ws, mode, contentRect) {
  const proj = paneModel.projectLayout(ws, mode, contentRect)
  const keys = new Set()
  for (const paneId of proj.visibleLeaves) {
    const active = ws.panes[paneId]?.activeTabKey
    if (active) keys.add(active)
  }
  return keys
}

// The projection-level background guarantee (design §6.2): a background placement
// may ADD an on-screen pane but must never make a currently-visible pane's content
// VANISH. A new pane appearing is fine (a single-pane tile blooming into two); a
// compact/phone split that the limited projection can only show by dropping the
// other visible pane is NOT (finding: compact multi-pane background split hid the
// sibling). True iff every key visible before is still visible after.
function preservesVisibleContent(before, after, mode, contentRect) {
  const va = visibleContentKeys(after, mode, contentRect)
  for (const key of visibleContentKeys(before, mode, contentRect)) {
    if (!va.has(key)) return false
  }
  return true
}

// Attempt the auto-split of the source pane on its longer feasible axis. Returns
// the split workspace when feasible AND — for a background split — projection-safe
// (it drops no currently-visible pane); otherwise null so the caller degrades to a
// tab. A foreground split is always allowed: the projection change (focus follows
// the new pane) is the requested intent.
function tryAutoSplit(ws, item, sourcePane, env, { focus, preserveVisible }) {
  const edge = chooseSplitEdge(ws, sourcePane.id, env)
  if (!edge) return null
  const split = paneModel.splitPaneWithTab(ws, item, {
    paneId: sourcePane.id,
    edge,
    focus,
  })
  if (split === ws) return null
  if (!preserveVisible) return split
  return preservesVisibleContent(ws, split, env.mode, env.contentRect) ? split : null
}

// Insert the item as a tab directly after its source in the source's pane. The
// caller controls activation and focus separately.
function insertBesideSource(ws, item, sourcePane, source, { activate, focus }) {
  return paneModel.openTab(ws, item, {
    paneId: sourcePane.id,
    afterKey: tabKey(source),
    activate,
    focus,
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

// The companion pane for a chat source: the first eligible pane (in leaf order)
// holding an app whose SERVER chat_id equals the source chat (design §6.2 —
// derived from the live app list, no schema field). A live preview excludes the
// focused pane because that surface is not safe for it to switch. Only a chat
// source has a companion.
function companionPaneFor(ws, source, liveApps, excludedPaneId = null) {
  if (!source || source.kind !== 'chat' || !Array.isArray(liveApps)) return null
  const chatAppIds = new Set()
  for (const app of liveApps) {
    if (app && app.chat_id != null && String(app.chat_id) === String(source.id)) {
      chatAppIds.add(String(app.id))
    }
  }
  if (chatAppIds.size === 0) return null
  for (const paneId of paneModel.paneIdsInOrder(ws)) {
    if (paneId === excludedPaneId) continue
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
  const preview = activation === ACTIVATE_LIVE_PREVIEW
  const foreground = activation === ACTIVATE_FOREGROUND
  const mode = env.mode || 'wide'
  // A preview may switch an UNFOCUSED companion pane, but only foreground may
  // replace the active tab in the focused pane or move keyboard/Back focus.
  // Keeping those permissions separate is what lets a live app update become
  // visible without interrupting a message being composed beside it.
  const activateItem = foreground
  const focusItem = foreground

  // Two-worlds (finding F4): in the SINGLE world the only visible surface is the
  // slot, so a FOREGROUND agent open must SET THE SLOT — mutating the hidden pane
  // tree (as every branch below does) would leave the foregrounded item invisible.
  // This mirrors applyModeDestination's single branch (the one owning decision
  // point for USER nav; the agent path funnels here) and honors the invariant that
  // single-mode opens never touch the pane tree. BACKGROUND work still parks in the
  // builder tree plus its attention dot — the builder world is the workshop. The
  const world = ws.viewMode
  if (foreground && world === 'single') {
    return paneModel.setSingleScreen(ws, { kind: item.kind, id: String(item.id) })
  }

  const sourceKey = source ? tabKey(source) : null
  const singleShowsSource = (
    world === 'single'
    && sourceKey != null
    && ws.singleScreen != null
    && tabKey(ws.singleScreen) === sourceKey
  )

  // A preview may enter Builder when the owner is already looking at its source:
  // the source remains the foreground surface and the app can bloom beside it.
  // If the owner has moved elsewhere in Standard, keep that surface untouched
  // and park the preview in the hidden Builder tree for an explicit later open.
  let working = (
    preview
    && (world === 'panes' || singleShowsSource)
  )
    ? paneModel.setViewMode(ws, 'panes')
    : ws
  const itemKey = tabKey(item)

  // Keep the relational source in the Builder tree, but never surface it over
  // another active tab. The one safe activation is Standard → Builder while
  // Standard already shows that exact source, preserving what the owner sees.
  let sourcePane = source ? paneModel.paneOf(working, tabKey(source)) : null
  if (preview && source) {
    if (!sourcePane) {
      working = paneModel.openTab(working, source, {
        paneId: working.focusedPaneId,
        activate: singleShowsSource,
        focus: singleShowsSource,
      })
      sourcePane = paneModel.paneOf(working, tabKey(source))
    } else if (singleShowsSource) {
      working = paneModel.focusPane(
        paneModel.setActiveTab(working, sourcePane.id, tabKey(source)),
        sourcePane.id,
      )
      sourcePane = paneModel.paneOf(working, tabKey(source))
    }
  }

  // Already open anywhere → background is a no-op; foreground focuses it. A
  // live preview switches it in an unfocused companion pane. If it shares the
  // focused source pane, move it into a new pane first; if that cannot preserve
  // every visible tab, leave the focused pane untouched and keep it parked.
  const existing = paneModel.paneOf(working, itemKey)
  if (existing) {
    // If an earlier build parked chat + app in the pane the owner is using, turn
    // that relationship into two real panes instead of hiding the chat behind an
    // activated app tab. Moving an existing tab is the correct primitive;
    // splitPaneWithTab intentionally accepts brand-new tabs only.
    if (
      preview
      && sourcePane?.id === existing.id
      && existing.id === working.focusedPaneId
      && existing.activeTabKey !== itemKey
    ) {
      const edge = chooseSplitEdge(working, sourcePane.id, env)
      if (edge) {
        const moved = paneModel.moveTab(working, itemKey, {
          paneId: sourcePane.id,
          edge,
        })
        if (
          moved !== working
          && preservesVisibleContent(working, moved, mode, env.contentRect)
        ) {
          return paneModel.focusPane(moved, sourcePane.id)
        }
      }
    }

    if (!activateItem && !preview) return working
    if (preview && existing.id === working.focusedPaneId) return working
    const activated = paneModel.setActiveTab(working, existing.id, itemKey)
    return foreground ? paneModel.focusPane(activated, existing.id) : activated
  }

  sourcePane = source ? paneModel.paneOf(working, tabKey(source)) : null

  // with-focus, or a source we cannot resolve (absent, or its tab isn't open) →
  // append to the focused pane (design: source missing/closed degrades to with-focus).
  if (placement === PLACE_WITH_FOCUS || !source || !sourcePane) {
    return paneModel.openTab(working, item, {
      paneId: working.focusedPaneId,
      activate: activateItem,
      focus: focusItem,
    })
  }

  // with-source → a tab in the source's pane, every mode (activate iff foreground).
  if (placement === PLACE_WITH_SOURCE) {
    return insertBesideSource(working, item, sourcePane, source, {
      activate: activateItem,
      focus: focusItem,
    })
  }

  // beside-source, the device-aware table.
  if (mode === 'phone' && !preview) {
    // Generic phone placements keep their existing tab-stack behavior. A live
    // preview continues into the companion/split ladder below so it can become
    // visible without replacing the focused chat.
    return insertBesideSource(working, item, sourcePane, source, {
      activate: activateItem,
      focus: focusItem,
    })
  }

  const splitPolicy = {
    focus: focusItem,
    preserveVisible: !foreground,
  }
  const paneCount = paneModel.paneIdsInOrder(working).length
  if (paneCount <= 1) {
    // Tile, single pane: auto-split the source pane on its longer feasible axis;
    // the item is active in the new pane, focus stays put unless foreground. An
    // infeasible or projection-unsafe split degrades to a tab beside the source.
    return tryAutoSplit(working, item, sourcePane, env, splitPolicy)
      || insertBesideSource(working, item, sourcePane, source, {
        activate: activateItem,
        focus: focusItem,
      })
  }

  // Tile, multi-pane: companion pane → else split the source pane → else a
  // background tab beside the source (the degradation ladder, design §6.2).
  const companion = companionPaneFor(
    working,
    source,
    env.liveApps,
    preview ? working.focusedPaneId : null,
  )
  if (companion) {
    // Background work preserves the companion's visible tab. A live preview
    // switches that pane only when another pane owns focus; foreground may both
    // switch and focus it.
    return paneModel.openTab(working, item, {
      paneId: companion.id,
      activate: activateItem || preview,
      focus: focusItem,
    })
  }
  return tryAutoSplit(working, item, sourcePane, env, splitPolicy)
    || insertBesideSource(working, item, sourcePane, source, {
      activate: activateItem,
      focus: focusItem,
    })
}

// Fold a batch of requests through the resolver against one evolving workspace,
// FORWARD (producer order), so a batch and the same requests delivered one-at-a-
// time (each its own dispatch) reach the IDENTICAL workspace — every step re-
// projects against the accumulated result so splits compose. env carries
// {mode, contentRect, liveApps}; `projected` is derived per step. This is the
// exact fold placeInWorkspace dispatches, extracted here so it is unit-testable.
export function resolveWorkspaceRequests(ws, requests, env = {}) {
  let next = ws
  for (const request of requests || []) {
    const projected = paneModel.projectLayout(next, env.mode || 'wide', env.contentRect)
    next = resolveWorkspaceRequest(next, request, { ...env, projected })
  }
  return next
}
