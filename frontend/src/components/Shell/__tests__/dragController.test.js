import test from 'node:test'
import assert from 'node:assert/strict'
import {
  POINTER_SLOP, TAB_HOLD_MS, DRAWER_HOLD_MS, PRE_HOLD_MOVE_PX, RELEASE_IN_PLACE_PX,
  HYSTERESIS_PX, ROOT_EDGE_PX, CARET_W, CARET_H, CENTER_INSET, DRAWER_EXIT_PX,
  CHIP_MOUSE_DX, CHIP_MOUSE_DY, CHIP_TOUCH_ABOVE,
  EDGE_BAND_MIN, EDGE_BAND_FRACTION,
  passedSlop, touchMoveIntent, releasedInPlace, holdMsFor, chipOffset,
  crossedDrawerExit, edgeBands, edgePreviewRect, caretZone, edgeZone, centerZone,
  rootEdgeZone, hitTest, zoneTarget, releaseZone, zoneEq, buildScene, paneAcceptsJoin,
} from '../dragController.js'
import * as paneModel from '../paneModel.js'
import { STRIP_H } from '../paneModel.js'

// A scene helper: a list of panes + the ambient flags hitTest reads. canSplit
// defaults to all-true so edge suppression is opt-in per test.
function pane(paneId, rect, opts = {}) {
  return {
    paneId,
    rect,
    tabs: opts.tabs || [],
    canSplit: { left: true, right: true, top: true, bottom: true, ...(opts.canSplit || {}) },
  }
}
function scene(panes, opts = {}) {
  return {
    contentRect: opts.contentRect || { x: 0, y: 0, w: 1000, h: 800 },
    mode: opts.mode || 'wide',
    allowRootEdge: opts.allowRootEdge || false,
    source: opts.source || null,
    panes,
    rootCanSplit: { left: true, right: true, top: true, bottom: true, ...(opts.rootCanSplit || {}) },
  }
}

// ── Threshold predicates ─────────────────────────────────────────────────────

test('passedSlop arms only past the slop radius', () => {
  assert.equal(passedSlop(POINTER_SLOP, 0), false) // exactly at slop is not past
  assert.equal(passedSlop(POINTER_SLOP + 0.1, 0), true)
  assert.equal(passedSlop(4, 4), true) // hypot 5.66 > 5
  assert.equal(passedSlop(3, 3), false) // hypot 4.24 < 5
})

test('touchMoveIntent preserves tab-body scrolling and gives the grip either-axis drag', () => {
  assert.equal(touchMoveIntent(8, 0, 'tab'), 'pending')
  assert.equal(touchMoveIntent(0, 8.1, 'tab'), 'drag', 'vertical pull drags a horizontal tab strip')
  assert.equal(touchMoveIntent(8.1, 0, 'tab'), 'scroll', 'horizontal pull scrolls over a tab body')
  assert.equal(touchMoveIntent(8.1, 0, 'tab-handle'), 'drag', 'the grip reorders horizontally')
  assert.equal(touchMoveIntent(0, 8.1, 'tab-handle'), 'drag', 'the grip can move across panes')
  assert.equal(touchMoveIntent(8.1, 0, 'drawer'), 'drag', 'horizontal pull drags a vertical drawer row')
  assert.equal(touchMoveIntent(0, 8.1, 'drawer'), 'scroll', 'vertical pull scrolls the drawer')
  assert.equal(touchMoveIntent(10, 10, 'drawer'), 'scroll', 'ambiguous diagonals favor native scroll')
  assert.equal(PRE_HOLD_MOVE_PX, 8)
})

test('releasedInPlace is true only within the release radius', () => {
  assert.equal(releasedInPlace(RELEASE_IN_PLACE_PX, 0), true)
  assert.equal(releasedInPlace(RELEASE_IN_PLACE_PX + 0.1, 0), false)
})

test('holdMsFor gives drawer rows the longer hold', () => {
  assert.equal(holdMsFor('tab'), TAB_HOLD_MS)
  assert.equal(holdMsFor('drawer'), DRAWER_HOLD_MS)
  assert.equal(TAB_HOLD_MS, 350)
  assert.equal(DRAWER_HOLD_MS, 450)
})

test('chipOffset floats above a touch point and trails a mouse', () => {
  assert.deepEqual(chipOffset({ x: 100, y: 200 }, false), {
    left: 100 + CHIP_MOUSE_DX, top: 200 + CHIP_MOUSE_DY,
  })
  assert.deepEqual(chipOffset({ x: 100, y: 200 }, true), {
    left: 100 + CHIP_MOUSE_DX, top: 200 - CHIP_TOUCH_ABOVE,
  })
})

test('crossedDrawerExit fires only past the 24px glide threshold', () => {
  assert.equal(crossedDrawerExit(300, 280), false) // 20px past
  assert.equal(crossedDrawerExit(305, 280), true) // 25px past
  assert.equal(DRAWER_EXIT_PX, 24)
})

// ── Edge band geometry ───────────────────────────────────────────────────────

test('edgeBands are an uncapped proportional quarter above the floor', () => {
  assert.equal(EDGE_BAND_FRACTION, 0.25)
  // Tiny pane → both bands at the 40px floor.
  assert.deepEqual(edgeBands({ x: 0, y: 0, w: 100, h: 100 }), { w: EDGE_BAND_MIN, h: EDGE_BAND_MIN })
  // Huge pane → a proportional quarter, NO fixed pixel cap (uncapped: the old
  // 110/96 caps turned a wide pane's edge into a narrow precision target).
  assert.deepEqual(edgeBands({ x: 0, y: 0, w: 4000, h: 4000 }), { w: 1000, h: 1000 })
  // Typical desktop pane → a quarter on each axis, leaving 50% for center join.
  const b = edgeBands({ x: 0, y: 0, w: 1280, h: 900 })
  assert.ok(Math.abs(b.w - 320) < 1e-9 && Math.abs(b.h - 225) < 1e-9)
})

test('edgePreviewRect halves the pane minus the divider gap', () => {
  const r = { x: 10, y: 20, w: 407, h: 600 }
  const left = edgePreviewRect(r, 'left')
  const right = edgePreviewRect(r, 'right')
  assert.equal(left.x, 10)
  assert.equal(left.w, (407 - paneModel.PANE_GAP) / 2)
  assert.equal(right.x, 10 + 407 - (407 - paneModel.PANE_GAP) / 2)
  const top = edgePreviewRect(r, 'top')
  assert.equal(top.h, (600 - paneModel.PANE_GAP) / 2)
})

// ── Strip caret ──────────────────────────────────────────────────────────────

test('caretZone lands the index by tab midpoints', () => {
  const p = pane('p0', { x: 0, y: 0, w: 400, h: 600 }, {
    tabs: [{ key: 'a', left: 10, right: 90 }, { key: 'b', left: 90, right: 170 }],
  })
  // Left of the first tab's midpoint (50) → before index 0.
  assert.equal(caretZone({ x: 20, y: 5 }, p).index, 0)
  // Between the two midpoints (50 and 130) → index 1.
  assert.equal(caretZone({ x: 100, y: 5 }, p).index, 1)
  // Past the last midpoint → append at index 2.
  assert.equal(caretZone({ x: 300, y: 5 }, p).index, 2)
  // The caret preview is the thin tall bar.
  const z = caretZone({ x: 20, y: 5 }, p)
  assert.equal(z.rect.w, CARET_W)
  assert.equal(z.rect.h, CARET_H)
})

test('caretZone on an empty strip appends at index 0', () => {
  const p = pane('p0', { x: 0, y: 0, w: 400, h: 600 }, { tabs: [] })
  assert.equal(caretZone({ x: 200, y: 5 }, p).index, 0)
})

// ── hitTest: precedence ──────────────────────────────────────────────────────

test('strip caret beats every other zone at overlapping coordinates', () => {
  // A point in the top-left corner is simultaneously over the strip AND in the
  // outer root-edge band AND in the pane edge bands. Strip must win.
  const p = pane('p0', { x: 8, y: 8, w: 984, h: 784 }, {
    tabs: [{ key: 'a', left: 12, right: 80 }],
  })
  const s = scene([p], { allowRootEdge: true })
  const z = hitTest({ x: 12, y: 12 }, s)
  assert.equal(z.type, 'strip')
})

test('root edge beats a pane edge, and only when fine pointers allow it', () => {
  const p = pane('p0', { x: 8, y: 8, w: 984, h: 784 })
  // Below the strip, in the outer 16px on the left, in the left pane-edge band.
  const point = { x: 12, y: 400 }
  const withRoot = hitTest(point, scene([p], { allowRootEdge: true }))
  assert.equal(withRoot.type, 'root-edge')
  assert.equal(withRoot.edge, 'left')
  const withoutRoot = hitTest(point, scene([p], { allowRootEdge: false }))
  assert.equal(withoutRoot.type, 'edge')
  assert.equal(withoutRoot.edge, 'left')
})

test('pane edge beats center; center is the interior fallback', () => {
  const p = pane('p0', { x: 0, y: 0, w: 400, h: 600 })
  const s = scene([p])
  // In the left band, below the strip → edge left.
  assert.equal(hitTest({ x: 10, y: 300 }, s).type, 'edge')
  // Dead center → center.
  const c = hitTest({ x: 200, y: 300 }, s)
  assert.equal(c.type, 'center')
  assert.equal(c.rect.x, CENTER_INSET)
})

// ── hitTest: corner disambiguation ───────────────────────────────────────────

test('corners resolve by greater normalized penetration', () => {
  const p = pane('p0', { x: 0, y: 0, w: 400, h: 600 })
  const s = scene([p])
  // (5, 585): deeper into the left band than the bottom band → left.
  assert.equal(hitTest({ x: 5, y: 585 }, s).edge, 'left')
  // (40, 595): deeper into the bottom band than the left band → bottom.
  assert.equal(hitTest({ x: 40, y: 595 }, s).edge, 'bottom')
})

// ── hitTest: hysteresis ──────────────────────────────────────────────────────

test('an owning edge holds until the pointer is 10px past its band boundary', () => {
  const p = pane('p0', { x: 0, y: 0, w: 400, h: 600 }) // left band = 100px (0.25)
  const s = scene([p])
  const prev = { type: 'edge', paneId: 'p0', edge: 'left' }
  // 2px past the 100px boundary — inside the 10px hysteresis margin → stays left.
  assert.equal(hitTest({ x: 102, y: 300 }, s, prev).edge, 'left')
  // 12px past — beyond the margin → drops to center.
  assert.equal(hitTest({ x: 112, y: 300 }, s, prev).type, 'center')
})

test('an owning center makes a new edge earn 10px of penetration before it flips', () => {
  const p = pane('p0', { x: 0, y: 0, w: 400, h: 600 }) // left band = 100px (0.25)
  const s = scene([p])
  const prev = { type: 'center', paneId: 'p0' }
  // Just inside the band (3px penetration) — center still owns.
  assert.equal(hitTest({ x: 97, y: 300 }, s, prev).type, 'center')
  // Well inside the band — edge takes over.
  assert.equal(hitTest({ x: 20, y: 300 }, s, prev).edge, 'left')
})

test('a previous root-edge owner widens the root band by the hysteresis margin', () => {
  const p = pane('p0', { x: 8, y: 8, w: 984, h: 784 })
  const s = scene([p], { allowRootEdge: true })
  const prev = { type: 'root-edge', edge: 'left' }
  // 20px in — past the 16px band but within 16+10 → still root-edge under owner.
  const z = rootEdgeZone({ x: 20, y: 400 }, s, prev)
  assert.equal(z.type, 'root-edge')
  // Fresh (no owner) at 20px in → no root zone.
  assert.equal(rootEdgeZone({ x: 20, y: 400 }, s, null), null)
})

// ── hitTest: cap / min suppression (shared canSplit) ─────────────────────────

test('an edge whose canSplit is false never lights; the drop degrades to center', () => {
  const p = pane('p0', { x: 0, y: 0, w: 400, h: 600 }, { canSplit: { left: false } })
  const s = scene([p])
  // Deep in the (now-suppressed) left band → falls through to center.
  assert.equal(hitTest({ x: 10, y: 300 }, s).type, 'center')
  // The still-allowed right band lights normally.
  assert.equal(hitTest({ x: 395, y: 300 }, s).edge, 'right')
})

test('root-edge suppression respects rootCanSplit per edge', () => {
  const p = pane('p0', { x: 8, y: 8, w: 984, h: 784 })
  const s = scene([p], { allowRootEdge: true, rootCanSplit: { left: false } })
  // Left root split suppressed → the point falls to the pane's left edge.
  const z = hitTest({ x: 12, y: 400 }, s)
  assert.equal(z.type, 'edge')
  assert.equal(z.edge, 'left')
})

// ── hitTest: mode (phone only splits top/bottom) ─────────────────────────────

test('phone mode arms only top/bottom edges', () => {
  const p = pane('p0', { x: 0, y: 0, w: 400, h: 600 })
  const s = scene([p], { mode: 'phone' })
  // A left-band point on a phone → no side split → center.
  assert.equal(hitTest({ x: 10, y: 300 }, s).type, 'center')
  // A top-band point below the strip → top split.
  assert.equal(hitTest({ x: 200, y: STRIP_H + 12 }, s).edge, 'top')
})

// ── Self-drop no-ops ─────────────────────────────────────────────────────────

test('a single-tab source pane offers no edge split of itself', () => {
  const p = pane('p0', { x: 0, y: 0, w: 400, h: 600 })
  const s = scene([p], { source: { key: 'chat:1', paneId: 'p0', paneTabCount: 1 } })
  // The lone tab dropped back on its own edge would be a no-op → center-suppressed too.
  assert.equal(hitTest({ x: 10, y: 300 }, s), null) // own pane, single tab, no zone
})

test('a multi-tab source pane still offers an edge split of itself', () => {
  const p = pane('p0', { x: 0, y: 0, w: 400, h: 600 })
  const s = scene([p], { source: { key: 'chat:1', paneId: 'p0', paneTabCount: 2 } })
  assert.equal(hitTest({ x: 10, y: 300 }, s).edge, 'left')
})

test('center never lights over the source pane', () => {
  const p0 = pane('p0', { x: 0, y: 0, w: 400, h: 600 })
  const p1 = pane('p1', { x: 407, y: 0, w: 400, h: 600 })
  const s = scene([p0, p1], { source: { key: 'chat:1', paneId: 'p0', paneTabCount: 2 } })
  // Dead center of the SOURCE pane → no join zone.
  assert.equal(hitTest({ x: 200, y: 300 }, s), null)
  // Dead center of the OTHER pane → join.
  assert.equal(hitTest({ x: 607, y: 300 }, s).type, 'center')
})

// ── feasibility gating: a full pane's strip/center never light ───────────────

test('a full pane offers no center or strip zone (feasibility gating)', () => {
  const full = pane('p0', { x: 0, y: 0, w: 400, h: 600 }, {
    tabs: [{ key: 'a', left: 10, right: 90 }],
  })
  full.tabCount = 6 // MAX_PANE_TABS
  const other = pane('p1', { x: 407, y: 0, w: 400, h: 600 })
  const s = scene([full, other], { source: { key: 'chat:x', paneId: null, paneTabCount: 0 } })
  // Center of the full pane → suppressed.
  assert.equal(hitTest({ x: 200, y: 300 }, s), null)
  // Over the full pane's strip → suppressed.
  assert.equal(hitTest({ x: 40, y: 5 }, s), null)
  // The roomy sibling still joins.
  assert.equal(hitTest({ x: 607, y: 300 }, s).type, 'center')
})

test('a same-pane reorder still lights the strip of a full source pane', () => {
  // The source already lives here, so a reorder never grows the count → allowed.
  const full = pane('p0', { x: 0, y: 0, w: 400, h: 600 }, {
    tabs: [{ key: 'a', left: 10, right: 90 }],
  })
  full.tabCount = 6
  const s = scene([full], { source: { key: 'chat:a', paneId: 'p0', paneTabCount: 6 } })
  assert.equal(hitTest({ x: 40, y: 5 }, s).type, 'strip')
})

test('paneAcceptsJoin: room OR same-source-pane', () => {
  const p = pane('p0', { x: 0, y: 0, w: 400, h: 600 })
  p.tabCount = 6
  assert.equal(paneAcceptsJoin(scene([p], { source: null }), p), false)
  assert.equal(paneAcceptsJoin(scene([p], { source: { paneId: 'p0' } }), p), true)
  p.tabCount = 2
  assert.equal(paneAcceptsJoin(scene([p], { source: null }), p), true)
})

// ── caret hysteresis (jitter damping at a tab midpoint) ──────────────────────

test('caret index holds through midpoint jitter within the hysteresis margin', () => {
  const p = pane('p0', { x: 0, y: 0, w: 400, h: 600 }, {
    tabs: [{ key: 'a', left: 0, right: 100 }, { key: 'b', left: 100, right: 200 }],
  })
  // Owner index 0 (before tab a). Nudging 4px past tab-a's midpoint (50) → still 0.
  const prev = { type: 'strip', paneId: 'p0', index: 0 }
  assert.equal(caretZone({ x: 54, y: 5 }, p, prev).index, 0)
  // 12px past the midpoint (beyond HYSTERESIS_PX) → flips to 1.
  assert.equal(caretZone({ x: 62, y: 5 }, p, prev).index, 1)
})

// ── root-edge hysteresis widens only the SAME edge ───────────────────────────

test('root-edge hysteresis widens only the previously-owned edge', () => {
  const p = pane('p0', { x: 8, y: 8, w: 984, h: 784 })
  const s = scene([p], { allowRootEdge: true })
  // Owner = left root edge. A point 22px in on the LEFT stays left (16+10 band).
  const prevLeft = { type: 'root-edge', edge: 'left' }
  assert.equal(rootEdgeZone({ x: 22, y: 400 }, s, prevLeft).edge, 'left')
  // The SAME 22px-in point on the RIGHT is NOT widened by a left owner → no zone.
  assert.equal(rootEdgeZone({ x: 1000 - 22, y: 400 }, s, prevLeft), null)
})

// ── zoneTarget mapping (drop → exactly one reducer target) ───────────────────

test('zoneTarget maps each zone kind to one MOVE_TAB target', () => {
  assert.deepEqual(zoneTarget({ type: 'edge', paneId: 'p1', edge: 'right' }), { paneId: 'p1', edge: 'right' })
  assert.deepEqual(zoneTarget({ type: 'root-edge', edge: 'top' }), { root: true, edge: 'top' })
  assert.deepEqual(zoneTarget({ type: 'strip', paneId: 'p2', index: 3 }), { paneId: 'p2', index: 3 })
  assert.deepEqual(zoneTarget({ type: 'center', paneId: 'p0' }), { paneId: 'p0' })
  assert.equal(zoneTarget(null), null)
})

test('zoneEq compares structural identity, not rects', () => {
  assert.ok(zoneEq(
    { type: 'edge', paneId: 'p0', edge: 'left', rect: { x: 0 } },
    { type: 'edge', paneId: 'p0', edge: 'left', rect: { x: 99 } },
  ))
  assert.ok(!zoneEq(
    { type: 'edge', paneId: 'p0', edge: 'left' },
    { type: 'edge', paneId: 'p0', edge: 'right' },
  ))
  assert.ok(!zoneEq({ type: 'strip', paneId: 'p0', index: 1 }, { type: 'strip', paneId: 'p0', index: 2 }))
  assert.ok(!zoneEq(null, { type: 'center', paneId: 'p0' }))
})

// ── buildScene: shares the real canSplit / canRootSplit predicates ───────────

function twoChatPanes(a, b) {
  let ws = paneModel.seedFromFlatTabs([{ kind: 'chat', id: a }, { kind: 'chat', id: b }])
  ws = paneModel.moveTab(ws, `chat:${b}`, { root: true, edge: 'right' })
  return paneModel.focusPane(ws, 'p0')
}

test('buildScene projects panes and evaluates the shared feasibility predicates', () => {
  const ws = twoChatPanes('1', '2')
  const content = { x: 0, y: 0, w: 1400, h: 900 }
  const proj = paneModel.projectLayout(ws, 'wide', content)
  const measure = (paneId) => [{ key: `chat:${paneId === 'p0' ? '1' : '2'}`, left: 0, right: 60 }]
  const s = buildScene(ws, proj, 'wide', content, null, true, measure)
  assert.equal(s.panes.length, 2)
  // A roomy wide layout can split further on every edge.
  assert.deepEqual(s.panes[0].canSplit, { left: true, right: true, top: true, bottom: true })
  // The root can still be wrapped (depth 1 → 2, leaves 2 → 3).
  assert.equal(s.rootCanSplit.left, true)
  // Tab measurement flowed through.
  assert.equal(s.panes[0].tabs[0].right, 60)
  // Live tab count is carried for the join-feasibility gate.
  assert.equal(s.panes[0].tabCount, 1)
})

test('buildScene suppresses root split at the depth cap', () => {
  // A depth-2, 4-leaf tree: row(p0, col(...)) already at MAX_DEPTH.
  let ws = paneModel.seedFromFlatTabs([
    { kind: 'chat', id: '1' }, { kind: 'chat', id: '2' },
    { kind: 'chat', id: '3' }, { kind: 'chat', id: '4' },
  ])
  ws = paneModel.moveTab(ws, 'chat:2', { root: true, edge: 'right' })
  ws = paneModel.moveTab(ws, 'chat:3', { paneId: 'p1', edge: 'bottom' })
  ws = paneModel.moveTab(ws, 'chat:4', { paneId: 'p0', edge: 'bottom' })
  const content = { x: 0, y: 0, w: 1600, h: 1000 }
  const proj = paneModel.projectLayout(ws, 'wide', content)
  const s = buildScene(ws, proj, 'wide', content, null, true, () => [])
  // No root split possible — the tree is already as deep and as wide as allowed.
  assert.deepEqual(s.rootCanSplit, { left: false, right: false, top: false, bottom: false })
})

test('buildScene canRootSplit matches paneModel.canRootSplit directly', () => {
  const ws = twoChatPanes('1', '2')
  const content = { x: 0, y: 0, w: 1400, h: 900 }
  for (const edge of ['left', 'right', 'top', 'bottom']) {
    assert.equal(
      buildScene(ws, paneModel.projectLayout(ws, 'wide', content), 'wide', content, null, true, () => []).rootCanSplit[edge],
      paneModel.canRootSplit(ws, edge, 'wide', content),
    )
  }
})

test('canRootSplit refuses a side split on a phone', () => {
  const ws = twoChatPanes('1', '2')
  const content = { x: 0, y: 0, w: 412, h: 760 }
  assert.equal(paneModel.canRootSplit(ws, 'left', 'phone', content), false)
  assert.equal(paneModel.canRootSplit(ws, 'right', 'phone', content), false)
  // Top/bottom can still stack if the halves clear the min height.
  assert.equal(paneModel.canRootSplit(ws, 'top', 'phone', content), true)
})

// ── root-edge orthogonal-axis gate (a point must be INSIDE the content box) ───

test('root-edge does not arm for a point outside the content box on the other axis', () => {
  const p = pane('p0', { x: 8, y: 8, w: 984, h: 784 })
  const s = scene([p], { allowRootEdge: true }) // content { x:0, y:0, w:1000, h:800 }
  // 8px shy of the LEFT edge but ABOVE the content (level with the header) — not
  // a left-split target: it is outside the box on the vertical axis.
  assert.equal(rootEdgeZone({ x: 8, y: -5 }, s, null), null)
  assert.equal(hitTest({ x: 8, y: -5 }, s, null), null)
  // The same horizontal offset, INSIDE the box vertically, DOES arm a left split.
  assert.equal(rootEdgeZone({ x: 8, y: 400 }, s, null).edge, 'left')
})

// ── hysteresis flips at EXACTLY 10px across every zone family ─────────────────

test('a caret crossing commits at exactly HYSTERESIS_PX, not one pixel later', () => {
  const p = pane('p0', { x: 0, y: 0, w: 400, h: 600 }, {
    tabs: [{ key: 'a', left: 0, right: 100 }, { key: 'b', left: 100, right: 200 }],
  })
  const prev = { type: 'strip', paneId: 'p0', index: 0 } // midpoint of tab a = 50
  assert.equal(caretZone({ x: 59, y: 5 }, p, prev).index, 0, '<10px past holds')
  assert.equal(caretZone({ x: 60, y: 5 }, p, prev).index, 1, 'exactly 10px past flips')
})

test('a root-edge owner is lost at exactly HYSTERESIS_PX past its band', () => {
  const p = pane('p0', { x: 8, y: 8, w: 984, h: 784 })
  const s = scene([p], { allowRootEdge: true })
  const prev = { type: 'root-edge', edge: 'left' }
  assert.equal(rootEdgeZone({ x: 25, y: 400 }, s, prev).edge, 'left', '<10px past holds')
  assert.equal(rootEdgeZone({ x: 26, y: 400 }, s, prev), null, 'exactly 10px past drops')
})

test('a challenger edge wins from an owning center at exactly HYSTERESIS_PX inside', () => {
  // A 512px pane → left band 128px; 10/128 divides FP-cleanly at the boundary, so the
  // "exactly 10px" assertion isn't at the mercy of 1 - 90/100 rounding below 0.1.
  const p = pane('p0', { x: 0, y: 0, w: 512, h: 600 })
  const s = scene([p])
  const prev = { type: 'center', paneId: 'p0' }
  // x=118 is exactly 10px inside the left band boundary (at x=128) → edge wins.
  assert.equal(hitTest({ x: 118, y: 300 }, s, prev).edge, 'left', 'exactly 10px in flips')
  assert.equal(hitTest({ x: 119, y: 300 }, s, prev).type, 'center', '9px in still center')
})

// ── releaseZone: commit only the previewed operation (TOCTOU) ─────────────────

test('releaseZone commits an identical zone and cancels a flipped one', () => {
  const previewed = { type: 'edge', paneId: 'p0', edge: 'right', rect: {} }
  const same = { type: 'edge', paneId: 'p0', edge: 'right', rect: {} }
  const flipped = { type: 'center', paneId: 'p0', rect: {} } // edge fell to a join
  assert.equal(releaseZone(same, previewed), same, 'same operation commits')
  assert.equal(releaseZone(flipped, previewed), null, 'a different operation cancels')
  assert.equal(releaseZone(null, previewed), null, 'no fresh zone cancels')
})

// ── sole-tab suppression: a drawer drag of the pane's only tab never splits ───

test('edgeZone suppresses a split of the pane holding the dragged sole tab', () => {
  const soleKey = 'chat:5'
  const p = { paneId: 'p0', rect: { x: 0, y: 0, w: 400, h: 600 }, tabs: [],
    canSplit: { left: true, right: true, top: true, bottom: true }, soleTabKey: soleKey }
  // Drawer source (no paneId) whose key IS this pane's sole tab → no edge lights.
  const s = scene([p], { source: { key: soleKey, paneId: null } })
  assert.equal(edgeZone({ x: 10, y: 300 }, p, s, null), null)
  // A different dragged item DOES light the edge.
  const other = scene([p], { source: { key: 'app:9', paneId: null } })
  assert.equal(edgeZone({ x: 10, y: 300 }, p, other, null).edge, 'left')
})

test('buildScene records each single-tab pane sole key', () => {
  const ws = paneModel.seedFromFlatTabs([{ kind: 'chat', id: '5' }])
  const content = { x: 0, y: 0, w: 1400, h: 900 }
  const proj = paneModel.projectLayout(ws, 'wide', content)
  const s = buildScene(ws, proj, 'wide', content, null, true, () => [])
  assert.equal(s.panes[0].soleTabKey, 'chat:5')
})

test('phone edge bands are proportional thirds; desktop is an uncapped quarter', () => {
  const b = edgeBands({ x: 0, y: 0, w: 400, h: 800 }, 'phone')
  assert.equal(b.h, 800 * 0.34)
  assert.equal(b.w, 400 * 0.34)
  // Desktop is now ALSO uncapped — a proportional quarter, not the old 96px cap.
  const desktop = edgeBands({ x: 0, y: 0, w: 400, h: 800 })
  assert.equal(desktop.h, 800 * 0.25)
})
