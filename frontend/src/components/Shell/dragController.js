// Workspace drag controller — the PURE half (design §3).
//
// This module owns the geometry and decision logic of the drag-to-organize
// gesture: the pointer thresholds (slop / hold / release-in-place), the zone
// hit-test against pane rectangles, the fixed zone precedence, boundary
// hysteresis, and the zone→reducer-action mapping. It imports only paneModel
// (itself pure) and touches no DOM, no timers, and no React — every decision a
// live drag makes is a pure function of a point plus a "scene" snapshot, so the
// whole thing is exhaustively unit-testable with node:test like paneModel.
//
// The thin React binding (useWorkspaceDrag.js) owns the side-effects: pointer
// capture, timers, the chip/preview/shield DOM, the drawer stand-down, and the
// single reducer dispatch on drop. It measures the live DOM into a `scene` and
// asks this module "what zone is under this point?" every move, then maps the
// committed zone to exactly one MOVE_TAB target.
//
// Geometry convention: every point and rect here is in CONTENT-LOCAL pixels
// (relative to .shell__content's box), the same coordinate space projectLayout
// emits its pane rects in. The binding subtracts the content bounding rect once.

import {
  STRIP_H, PANE_GAP, canSplit as paneCanSplit, canRootSplit, projectLayout,
} from './paneModel.js'

// ── Pointer thresholds (design §3.1) — exported as the tuned constants and as
// pure predicates so the binding never hard-codes a number and the tests pin
// each threshold directly. ───────────────────────────────────────────────────

// A mouse drag arms once the pointer travels past this from the press point;
// below it, the press is still a plain click (tab activate / row open).
export const POINTER_SLOP = 5
// Touch lift is a long-press: a tab strip lifts at 350ms, a drawer row at 450ms
// (drawer rows live in a vertical scroller beside swipe-to-close, so they need a
// longer, more deliberate hold to not fight the scroll).
export const TAB_HOLD_MS = 350
export const DRAWER_HOLD_MS = 450
// Movement past this BEFORE the hold completes cancels the lift and yields the
// gesture to native scrolling — a touch that starts moving was a scroll.
export const PRE_HOLD_MOVE_PX = 8
// After a touch lift, a release that never moved past this opens the context
// menu instead of dropping (lift → release-in-place = menu; lift → move = drag).
export const RELEASE_IN_PLACE_PX = 5

// ── Zone geometry (design §3.2 / §3.3) ───────────────────────────────────────

// Edge band: clamp(pane×0.22, 40px, cap). The horizontal cap is wider than the
// vertical one because side splits read from a taller, narrower band.
export const EDGE_BAND_MIN = 40
export const EDGE_BAND_CAP_W = 110
export const EDGE_BAND_CAP_H = 96
export const EDGE_BAND_FRACTION = 0.22
// The owning zone keeps ownership until the pointer travels this far past its
// boundary — kills band-boundary flicker without a time debounce.
export const HYSTERESIS_PX = 10
// Root-edge zones live in the outer strip of the content box (fine pointers
// only; on touch this collides with the OS edge-back gesture).
export const ROOT_EDGE_PX = 16
// A strip owns the caret from the pane's top down through the strip plus a small
// pad below it, so a drop just under the tabs still reads as "insert here".
export const STRIP_CARET_PAD = 8
// The caret preview is a thin tall bar between two tabs.
export const CARET_W = 2
export const CARET_H = 24
// The center (join-as-tab) preview insets the pane slightly so it reads as
// "drop inside", distinct from an edge split's flush half.
export const CENTER_INSET = 4

// ── Drag chip + drawer drag-out (design §3.1) ────────────────────────────────

// The chip trails the cursor by +12/+12 for a mouse and floats 56px ABOVE a
// touch point so the finger never hides the label.
export const CHIP_MOUSE_DX = 12
export const CHIP_MOUSE_DY = 12
export const CHIP_TOUCH_ABOVE = 56
// Dragging a row this far past the drawer's inner edge glides the drawer closed,
// revealing the live panes and their drop zones underneath.
export const DRAWER_EXIT_PX = 24

// ── Threshold predicates (pure) ──────────────────────────────────────────────

function hypot(dx, dy) {
  return Math.sqrt(dx * dx + dy * dy)
}

// A mouse press becomes a drag once it moves past the slop radius.
export function passedSlop(dx, dy, slop = POINTER_SLOP) {
  return hypot(dx, dy) > slop
}

// A touch that moves this far before its hold timer fires is a scroll, not a
// lift — the binding cancels the pending lift and lets the page scroll.
export function preHoldMoveCancels(dx, dy, limit = PRE_HOLD_MOVE_PX) {
  return hypot(dx, dy) > limit
}

// After a lift, a release still within this radius opened no drag — it is the
// escalation branch that opens the context menu.
export function releasedInPlace(dx, dy, limit = RELEASE_IN_PLACE_PX) {
  return hypot(dx, dy) <= limit
}

// The long-press duration for a source kind: drawer rows hold longer than tabs.
export function holdMsFor(sourceKind) {
  return sourceKind === 'drawer' ? DRAWER_HOLD_MS : TAB_HOLD_MS
}

// The chip's top-left offset from the pointer, given the pointer type.
export function chipOffset(point, isTouch) {
  return isTouch
    ? { left: point.x + CHIP_MOUSE_DX, top: point.y - CHIP_TOUCH_ABOVE }
    : { left: point.x + CHIP_MOUSE_DX, top: point.y + CHIP_MOUSE_DY }
}

// True once a drawer drag has been pulled far enough past the drawer's inner
// (content-facing) edge to glide the drawer closed. `edgeX` is that inner edge
// in the same coordinate space as `pointX` (viewport px for the binding).
export function crossedDrawerExit(pointX, edgeX, gap = DRAWER_EXIT_PX) {
  return pointX - edgeX > gap
}

// ── Small geometry helpers ───────────────────────────────────────────────────

function clamp(v, lo, hi) {
  return Math.min(hi, Math.max(lo, v))
}

function contains(rect, point) {
  return point.x >= rect.x && point.x <= rect.x + rect.w
    && point.y >= rect.y && point.y <= rect.y + rect.h
}

function insetRect(rect, m) {
  return { x: rect.x + m, y: rect.y + m, w: Math.max(0, rect.w - 2 * m), h: Math.max(0, rect.h - 2 * m) }
}

// The band widths for a pane (design §3.2). Horizontal and vertical bands clamp
// against different caps.
export function edgeBands(rect) {
  return {
    w: clamp(rect.w * EDGE_BAND_FRACTION, EDGE_BAND_MIN, EDGE_BAND_CAP_W),
    h: clamp(rect.h * EDGE_BAND_FRACTION, EDGE_BAND_MIN, EDGE_BAND_CAP_H),
  }
}

// The rect the new pane would occupy for an edge split — a 50/50 halving minus
// the divider gap, matching the renderer's committed geometry closely enough
// that the preview morphs into the real result.
export function edgePreviewRect(rect, edge) {
  const halfW = (rect.w - PANE_GAP) / 2
  const halfH = (rect.h - PANE_GAP) / 2
  switch (edge) {
    case 'left': return { x: rect.x, y: rect.y, w: halfW, h: rect.h }
    case 'right': return { x: rect.x + rect.w - halfW, y: rect.y, w: halfW, h: rect.h }
    case 'top': return { x: rect.x, y: rect.y, w: rect.w, h: halfH }
    case 'bottom': return { x: rect.x, y: rect.y + rect.h - halfH, w: rect.w, h: halfH }
    default: return { ...rect }
  }
}

// The rect a root split's new pane would occupy — half the whole content box on
// that edge.
export function rootPreviewRect(content, edge) {
  return edgePreviewRect(content, edge)
}

// The strip caret's insertion index + preview rect for a point over a pane's
// strip. Tabs are the pane's measured tab rects (content-local left/right); the
// caret lands before the first tab whose midpoint the pointer is left of, else
// after the last tab.
export function caretZone(point, pane) {
  const tabs = pane.tabs || []
  let index = tabs.length
  let caretX = pane.rect.x + STRIP_CARET_PAD
  for (let i = 0; i < tabs.length; i += 1) {
    const mid = (tabs[i].left + tabs[i].right) / 2
    if (point.x < mid) { index = i; caretX = tabs[i].left - 1; break }
    caretX = tabs[i].right + 1
  }
  return {
    type: 'strip',
    paneId: pane.paneId,
    index,
    rect: { x: caretX, y: pane.rect.y + 5, w: CARET_W, h: CARET_H },
  }
}

// The strip region a caret owns: the strip row plus a small pad below it.
function overStrip(point, pane) {
  return point.y >= pane.rect.y && point.y <= pane.rect.y + STRIP_H + STRIP_CARET_PAD
    && point.x >= pane.rect.x && point.x <= pane.rect.x + pane.rect.w
}

// The edge (split) zone for a point inside a pane, with cap/min suppression via
// the pane's shared canSplit booleans, corner disambiguation by greater
// normalized penetration, and boundary hysteresis biased toward the previous
// owner (design §3.2). Returns null when no edge lights (so the caller falls to
// center).
export function edgeZone(point, pane, scene, prevZone) {
  const { rect } = pane
  const bands = edgeBands(rect)
  const source = scene.source
  const isSource = source && source.paneId === pane.paneId
  // Dropping a pane's only tab back onto its own edge is a no-op — never light.
  const singleSource = isSource && (source.paneTabCount || 0) <= 1
  if (singleSource) return null

  const allowed = scene.mode === 'phone'
    ? ['top', 'bottom']
    : ['left', 'right', 'top', 'bottom']
  const prevEdge = (prevZone && prevZone.type === 'edge' && prevZone.paneId === pane.paneId)
    ? prevZone.edge : null
  const prevCenterHere = !!(prevZone && prevZone.paneId === pane.paneId && prevZone.type === 'center')

  const pen = {
    left: { v: 1 - (point.x - rect.x) / bands.w, band: bands.w },
    right: { v: 1 - (rect.x + rect.w - point.x) / bands.w, band: bands.w },
    top: { v: 1 - (point.y - rect.y) / bands.h, band: bands.h },
    bottom: { v: 1 - (rect.y + rect.h - point.y) / bands.h, band: bands.h },
  }

  let best = null
  for (const edge of allowed) {
    if (!pane.canSplit[edge]) continue
    const { v, band } = pen[edge]
    // The hysteresis margin, converted from px to penetration units (penetration
    // changes by 1/band per pixel). The current owner edge holds until the
    // pointer is HYSTERESIS_PX past the band boundary; a competing edge (when the
    // owner is this pane's center) must reach HYSTERESIS_PX inside the band.
    const bonus = HYSTERESIS_PX / band
    const threshold = edge === prevEdge ? -bonus : (prevCenterHere ? bonus : 0)
    if (v <= threshold) continue
    const score = v + (edge === prevEdge ? bonus : 0)
    if (!best || score > best.score) best = { edge, score }
  }
  if (!best) return null
  return { type: 'edge', paneId: pane.paneId, edge: best.edge, rect: edgePreviewRect(rect, best.edge) }
}

// The center (join-as-tab) zone for a pane — the fallback inside a pane when no
// edge or strip lights. Never lights over the source's own pane (a self-join is
// a no-op).
export function centerZone(point, pane, scene) {
  const source = scene.source
  if (source && source.paneId === pane.paneId) return null
  return { type: 'center', paneId: pane.paneId, rect: insetRect(pane.rect, CENTER_INSET) }
}

// The root-edge (whole-workspace split) zone — the outer ROOT_EDGE_PX of the
// content box, fine pointers only, gated by the shared canRootSplit predicate.
// Nearest content edge wins the corner; hysteresis widens the band toward a
// previous root-edge owner.
export function rootEdgeZone(point, scene, prevZone) {
  const c = scene.contentRect
  const margin = ROOT_EDGE_PX + (prevZone && prevZone.type === 'root-edge' ? HYSTERESIS_PX : 0)
  const dist = {
    left: point.x - c.x,
    right: c.x + c.w - point.x,
    top: point.y - c.y,
    bottom: c.y + c.h - point.y,
  }
  const allowed = scene.mode === 'phone'
    ? new Set(['top', 'bottom'])
    : new Set(['left', 'right', 'top', 'bottom'])
  const cands = []
  for (const edge of ['left', 'right', 'top', 'bottom']) {
    if (dist[edge] >= 0 && dist[edge] <= margin) cands.push([edge, dist[edge]])
  }
  cands.sort((a, b) => a[1] - b[1])
  for (const [edge] of cands) {
    if (!allowed.has(edge)) continue
    if (!scene.rootCanSplit[edge]) continue
    return { type: 'root-edge', edge, rect: rootPreviewRect(c, edge) }
  }
  return null
}

// The pane whose rect contains the point, else null (the point is in the outer
// margin between panes).
function paneAt(point, scene) {
  for (const pane of scene.panes) {
    if (contains(pane.rect, point)) return pane
  }
  return null
}

// The single hit-test entry the binding calls each move. Fixed precedence
// (design §3.2): tab-strip caret > workspace-root edge > pane edge > center.
// `prevZone` is the zone from the previous move, threaded through for
// hysteresis; pass null for a fresh test. Returns a zone with a `rect` (the
// preview geometry) or null (no drop target — the drop cancels).
export function hitTest(point, scene, prevZone = null) {
  // 1. strip caret — checked first so a drop over the tabs always reads as an
  // insert, even in the outer-margin corner where a root edge would also apply.
  const pane = paneAt(point, scene)
  if (pane && overStrip(point, pane)) return caretZone(point, pane)

  // 2. workspace-root edge — fine pointers only.
  if (scene.allowRootEdge) {
    const re = rootEdgeZone(point, scene, prevZone)
    if (re) return re
  }

  // 3. pane edge, then 4. center — both need a pane under the point.
  if (pane) {
    const ez = edgeZone(point, pane, scene, prevZone)
    if (ez) return ez
    const cz = centerZone(point, pane, scene)
    if (cz) return cz
  }
  return null
}

// Map a committed zone to exactly one MOVE_TAB target (design §3.4). A caret is
// an index insert, an edge is a pane split, a root edge is a whole-workspace
// split, a center is an append. Null zone → null (the drop cancels).
export function zoneTarget(zone) {
  if (!zone) return null
  switch (zone.type) {
    case 'edge': return { paneId: zone.paneId, edge: zone.edge }
    case 'root-edge': return { root: true, edge: zone.edge }
    case 'strip': return { paneId: zone.paneId, index: zone.index }
    case 'center': return { paneId: zone.paneId }
    default: return null
  }
}

// Structural identity of two zones — the binding uses it to know whether the
// preview should morph (identity changed) or merely re-position, and hysteresis
// uses it to recognize the previous owner.
export function zoneEq(a, b) {
  if (a === b) return true
  if (!a || !b) return false
  return a.type === b.type && a.paneId === b.paneId
    && a.edge === b.edge && a.index === b.index
}

// Build a scene from the live workspace + projection. `measureTabs(paneId)`
// returns that pane's measured tab rects ([{ key, left, right }] in content
// coordinates); the binding supplies it from the DOM, tests supply a stub. The
// shared canSplit / canRootSplit predicates are evaluated HERE, so a zone that
// would violate a cap or a minimum is never even offered — the exact predicates
// the context menu and the resolver consult (design §3.2 / §6.2).
export function buildScene(ws, projection, mode, contentRect, source, allowRootEdge, measureTabs) {
  const panes = projection.visibleLeaves.map((paneId) => {
    const rect = projection.rects[paneId]
    return {
      paneId,
      rect,
      tabs: measureTabs ? (measureTabs(paneId) || []) : [],
      canSplit: {
        left: paneCanSplit(ws, paneId, 'left', mode, contentRect),
        right: paneCanSplit(ws, paneId, 'right', mode, contentRect),
        top: paneCanSplit(ws, paneId, 'top', mode, contentRect),
        bottom: paneCanSplit(ws, paneId, 'bottom', mode, contentRect),
      },
    }
  })
  return {
    contentRect: {
      x: Number(contentRect?.x) || 0,
      y: Number(contentRect?.y) || 0,
      w: Number(contentRect?.w) || 0,
      h: Number(contentRect?.h) || 0,
    },
    mode,
    allowRootEdge: !!allowRootEdge,
    source,
    panes,
    rootCanSplit: {
      left: canRootSplit(ws, 'left', mode, contentRect),
      right: canRootSplit(ws, 'right', mode, contentRect),
      top: canRootSplit(ws, 'top', mode, contentRect),
      bottom: canRootSplit(ws, 'bottom', mode, contentRect),
    },
  }
}

// A convenience the binding uses to re-derive the projection when the workspace
// changes mid-scene (rare — the scene is rebuilt per drag). Re-exported so the
// binding imports one module.
export { projectLayout }
