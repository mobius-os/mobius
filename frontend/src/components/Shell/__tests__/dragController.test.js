import test from 'node:test'
import assert from 'node:assert/strict'
import {
  POINTER_SLOP, TAB_HOLD_MS, DRAWER_HOLD_MS, PRE_HOLD_MOVE_PX, RELEASE_IN_PLACE_PX,
  HYSTERESIS_PX, ROOT_EDGE_PX, CARET_W, CARET_H, CENTER_INSET, DRAWER_EXIT_PX,
  CHIP_MOUSE_DX, CHIP_MOUSE_DY, CHIP_TOUCH_ABOVE,
  EDGE_BAND_MIN, EDGE_BAND_CAP_W, EDGE_BAND_CAP_H,
  passedSlop, preHoldMoveCancels, releasedInPlace, holdMsFor, chipOffset,
  crossedDrawerExit, edgeBands, edgePreviewRect, caretZone, edgeZone, centerZone,
  rootEdgeZone, hitTest, zoneTarget, zoneEq, buildScene,
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

test('preHoldMoveCancels yields a pre-hold touch to native scroll past 8px', () => {
  assert.equal(preHoldMoveCancels(8, 0), false)
  assert.equal(preHoldMoveCancels(8.1, 0), true)
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

test('edgeBands clamp between the floor and the per-axis cap', () => {
  // Tiny pane → both bands at the 40px floor.
  assert.deepEqual(edgeBands({ x: 0, y: 0, w: 100, h: 100 }), { w: EDGE_BAND_MIN, h: EDGE_BAND_MIN })
  // Huge pane → each axis clamps at its own cap (110 / 96).
  assert.deepEqual(edgeBands({ x: 0, y: 0, w: 4000, h: 4000 }), { w: EDGE_BAND_CAP_W, h: EDGE_BAND_CAP_H })
  // Mid pane → the 0.22 fraction.
  const b = edgeBands({ x: 0, y: 0, w: 300, h: 300 })
  assert.ok(Math.abs(b.w - 66) < 1e-9 && Math.abs(b.h - 66) < 1e-9)
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
  const p = pane('p0', { x: 0, y: 0, w: 400, h: 600 }) // left band = 88px
  const s = scene([p])
  const prev = { type: 'edge', paneId: 'p0', edge: 'left' }
  // 2px past the 88px boundary — inside the 10px hysteresis margin → stays left.
  assert.equal(hitTest({ x: 90, y: 300 }, s, prev).edge, 'left')
  // 12px past — beyond the margin → drops to center.
  assert.equal(hitTest({ x: 100, y: 300 }, s, prev).type, 'center')
})

test('an owning center makes a new edge earn 10px of penetration before it flips', () => {
  const p = pane('p0', { x: 0, y: 0, w: 400, h: 600 }) // left band = 88px
  const s = scene([p])
  const prev = { type: 'center', paneId: 'p0' }
  // Just inside the band (3px penetration) — center still owns.
  assert.equal(hitTest({ x: 85, y: 300 }, s, prev).type, 'center')
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
