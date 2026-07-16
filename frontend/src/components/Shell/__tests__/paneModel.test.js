import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as tabModel from '../tabModel.js'
import * as paneModel from '../paneModel.js'

const { makeTab, tabKey } = tabModel

// A Map-backed sessionStorage stub so the legacy dual-write round-trip is
// testable without jsdom, mirroring the tabModel tests.
function fakeStorage(initial = null) {
  let value = initial
  return {
    getItem: () => value,
    setItem: (_k, v) => { value = v },
  }
}

// The in-order leaf pane ids of a workspace — introspection the tests use to
// assert layout shape without exporting internals from the module.
function paneIdsOf(node, out = []) {
  if (node && typeof node === 'object') {
    paneIdsOf(node.a, out)
    paneIdsOf(node.b, out)
  } else if (typeof node === 'string') {
    out.push(node)
  }
  return out
}

function splitDepth(node) {
  if (!node || typeof node !== 'object') return 0
  return 1 + Math.max(splitDepth(node.a), splitDepth(node.b))
}

// Every split node is well-formed and split ids are unique — the shape parse
// enforces and every op must preserve.
function assertLayoutShape(node, splitIds) {
  if (typeof node === 'string') return
  assert.ok(node && typeof node === 'object', 'a node is a leaf string or a split')
  assert.equal(typeof node.id, 'string', 'a split has a string id')
  assert.ok(!splitIds.has(node.id), 'split ids are unique')
  splitIds.add(node.id)
  assert.ok(node.dir === 'row' || node.dir === 'col', 'split dir is row|col')
  assert.ok(Number.isFinite(node.ratio) && node.ratio >= 0.1 && node.ratio <= 0.9,
    'split ratio is within [0.1, 0.9]')
  assertLayoutShape(node.a, splitIds)
  assertLayoutShape(node.b, splitIds)
}

// Assert every workspace-wide invariant, so both the op tests and the property
// suite can lean on one checker.
function assertInvariants(ws) {
  assert.equal(ws.v, 1)
  const ids = paneIdsOf(ws.layout)
  assert.ok(ids.length >= 1, 'at least one leaf')
  assert.ok(ids.length <= paneModel.MAX_PANES, 'leaf count within MAX_PANES')
  assert.ok(splitDepth(ws.layout) <= paneModel.MAX_DEPTH, 'depth within MAX_DEPTH')
  assertLayoutShape(ws.layout, new Set())

  const seenPane = new Set()
  for (const id of ids) {
    assert.ok(!seenPane.has(id), 'each pane appears as exactly one leaf')
    seenPane.add(id)
    assert.ok(ws.panes[id], 'every leaf resolves to a pane')
  }
  assert.equal(
    Object.keys(ws.panes).length, ids.length,
    'no pane exists outside the tree',
  )
  assert.ok(ws.panes[ws.focusedPaneId], 'focus names a live pane')

  const seenTab = new Set()
  for (const id of ids) {
    const pane = ws.panes[id]
    assert.ok(pane.tabs.length <= paneModel.MAX_PANE_TABS, 'pane within MAX_PANE_TABS')
    const keys = pane.tabs.map(tabKey)
    for (const key of keys) {
      assert.ok(!seenTab.has(key), 'a tab is unique workspace-wide')
      seenTab.add(key)
    }
    if (keys.length === 0) assert.equal(pane.activeTabKey, null)
    else assert.ok(keys.includes(pane.activeTabKey), 'active tab is a member')
  }
}

test('seedFromFlatTabs makes a single focused pane with the last tab active', () => {
  const ws = paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('app', 42)])
  assert.equal(paneIdsOf(ws.layout).length, 1)
  assert.equal(ws.focusedPaneId, 'p0')
  assert.deepEqual(paneModel.flatten(ws), [makeTab('chat', 'a'), makeTab('app', 42)])
  assert.equal(ws.panes.p0.activeTabKey, 'app:42')
  assertInvariants(ws)
})

test('seedFromFlatTabs sanitizes and dedups readOpenTabs-shaped input', () => {
  const seeded = paneModel.seedFromFlatTabs([
    { kind: 'chat', id: 'a' },
    { kind: 'app', id: 42 },        // numeric id normalizes to a string
    { kind: 'app', id: 'not-a-num' }, // dropped — would be NaN in tabNavTarget
    { kind: 'bogus', id: 'x' },      // unknown kind dropped
    { kind: 'chat', id: 'a' },       // duplicate dropped
  ])
  assert.deepEqual(paneModel.flatten(seeded), [makeTab('chat', 'a'), makeTab('app', 42)])
  // Round-trips through today's flat projection unchanged.
  assert.deepEqual(paneModel.flatten(seeded), tabModel.readOpenTabs(
    fakeStorage(JSON.stringify(paneModel.flatten(seeded))),
  ))
  assertInvariants(seeded)
})

test('normalize is idempotent and reference-stable on an already-clean tree', () => {
  const once = paneModel.normalize({
    v: 1,
    layout: 'p0',
    panes: { p0: { id: 'p0', tabs: [makeTab('chat', 'a')], activeTabKey: 'chat:a' } },
    focusedPaneId: 'p0',
    nextId: 1,
  })
  assert.equal(paneModel.normalize(once), once, 'same reference on a no-op normalize')
  assert.equal(paneModel.normalize(paneModel.normalize(once)), paneModel.normalize(once))
})

test('normalize enforces workspace-wide tab uniqueness, first occurrence winning', () => {
  const ws = paneModel.normalize({
    v: 1,
    layout: { id: 's0', dir: 'row', a: 'pA', b: 'pB', ratio: 0.5 },
    panes: {
      pA: { id: 'pA', tabs: [makeTab('chat', 'a')], activeTabKey: 'chat:a' },
      pB: { id: 'pB', tabs: [makeTab('chat', 'a'), makeTab('chat', 'b')], activeTabKey: 'chat:b' },
    },
    focusedPaneId: 'pA',
    nextId: 2,
  })
  // chat:a stays in pA (first occurrence); pB keeps only chat:b.
  assert.deepEqual(paneModel.paneOf(ws, 'chat:a').id, 'pA')
  assert.deepEqual(ws.panes.pB.tabs, [makeTab('chat', 'b')])
  assertInvariants(ws)
})

test('normalize clamps ratios and re-validates non-numeric app ids', () => {
  const ws = paneModel.normalize({
    v: 1,
    layout: { id: 's0', dir: 'row', a: 'pA', b: 'pB', ratio: 5 },
    panes: {
      pA: { id: 'pA', tabs: [makeTab('chat', 'a'), { kind: 'app', id: 'NaNish' }], activeTabKey: 'chat:a' },
      pB: { id: 'pB', tabs: [makeTab('app', 9)], activeTabKey: 'app:9' },
    },
    focusedPaneId: 'pA',
    nextId: 2,
  })
  assert.equal(ws.layout.ratio, 0.9, 'ratio clamped into [0.1, 0.9]')
  assert.deepEqual(ws.panes.pA.tabs, [makeTab('chat', 'a')], 'bad app id dropped')
  assertInvariants(ws)
})

test('normalize coerces a stale active tab to a real member', () => {
  const ws = paneModel.normalize({
    v: 1,
    layout: 'p0',
    panes: { p0: { id: 'p0', tabs: [makeTab('chat', 'a'), makeTab('chat', 'b')], activeTabKey: 'chat:ghost' } },
    focusedPaneId: 'p0',
    nextId: 1,
  })
  assert.equal(ws.panes.p0.activeTabKey, 'chat:b', 'falls back to the last tab')
})

test('normalize keeps a sole empty root but removes any other empty pane', () => {
  const sole = paneModel.normalize({
    v: 1,
    layout: 'p0',
    panes: { p0: { id: 'p0', tabs: [], activeTabKey: null } },
    focusedPaneId: 'p0',
    nextId: 1,
  })
  assert.equal(paneIdsOf(sole.layout).length, 1)
  assert.equal(sole.panes.p0.activeTabKey, null)
  assertInvariants(sole)
})

test('normalize collapses an emptied split back to the surviving pane', () => {
  const ws = paneModel.normalize({
    v: 1,
    layout: { id: 's0', dir: 'row', a: 'p1', b: 'p2', ratio: 0.5 },
    panes: {
      p1: { id: 'p1', tabs: [makeTab('chat', 'a')], activeTabKey: 'chat:a' },
      p2: { id: 'p2', tabs: [], activeTabKey: null },
    },
    focusedPaneId: 'p2',
    nextId: 3,
  })
  assert.equal(ws.layout, 'p1', 'single-child split collapses to its live child')
  assert.equal(ws.focusedPaneId, 'p1', 'focus follows to the surviving leaf')
  assert.deepEqual(Object.keys(ws.panes), ['p1'])
  assertInvariants(ws)
})

test('normalize collapses a nested single-child chain recursively', () => {
  const ws = paneModel.normalize({
    v: 1,
    layout: {
      id: 's0', dir: 'row', a: 'p1', ratio: 0.5,
      b: { id: 's1', dir: 'col', a: 'p2', b: { id: 's2', dir: 'row', a: 'p3', b: 'p4', ratio: 0.5 }, ratio: 0.5 },
    },
    panes: {
      p1: { id: 'p1', tabs: [makeTab('chat', 'x')], activeTabKey: 'chat:x' },
      p2: { id: 'p2', tabs: [], activeTabKey: null },
      p3: { id: 'p3', tabs: [], activeTabKey: null },
      p4: { id: 'p4', tabs: [], activeTabKey: null },
    },
    focusedPaneId: 'p3',
    nextId: 5,
  })
  assert.equal(ws.layout, 'p1', 'the whole empty right subtree collapses away')
  assert.deepEqual(Object.keys(ws.panes), ['p1'])
  assertInvariants(ws)
})

test('normalize drops dead pane refs and repairs a dead focus', () => {
  const ws = paneModel.normalize({
    v: 1,
    layout: { id: 's0', dir: 'row', a: 'p1', b: 'pMissing', ratio: 0.5 },
    panes: {
      p1: { id: 'p1', tabs: [makeTab('chat', 'a')], activeTabKey: 'chat:a' },
      pOrphan: { id: 'pOrphan', tabs: [makeTab('chat', 'z')], activeTabKey: 'chat:z' },
    },
    focusedPaneId: 'ghost',
    nextId: 3,
  })
  assert.equal(ws.layout, 'p1', 'a leaf with no pane is empty and pruned')
  assert.deepEqual(Object.keys(ws.panes), ['p1'], 'a pane outside the tree is dropped')
  assert.equal(ws.focusedPaneId, 'p1')
  assertInvariants(ws)
})

test('openTab adds, activates, and focuses; a plain re-open is a no-op', () => {
  let ws = paneModel.seedFromFlatTabs([])
  ws = paneModel.openTab(ws, makeTab('chat', 'a'))
  assert.deepEqual(paneModel.flatten(ws), [makeTab('chat', 'a')])
  assert.equal(ws.panes.p0.activeTabKey, 'chat:a')

  ws = paneModel.openTab(ws, makeTab('app', 7))
  assert.equal(ws.panes.p0.activeTabKey, 'app:7')

  // Re-opening the already-active, already-focused tab changes nothing.
  const same = paneModel.openTab(ws, makeTab('app', 7))
  assert.equal(same, ws, 'same reference on a dedup no-op')
  assertInvariants(ws)
})

test('openTab dedups an already-open tab by focusing its pane, never duplicating', () => {
  // Two panes, the tab living in the non-focused one.
  const base = paneModel.normalize({
    v: 1,
    layout: { id: 's0', dir: 'row', a: 'pA', b: 'pB', ratio: 0.5 },
    panes: {
      pA: { id: 'pA', tabs: [makeTab('chat', 'a')], activeTabKey: 'chat:a' },
      pB: { id: 'pB', tabs: [makeTab('chat', 'b'), makeTab('chat', 'c')], activeTabKey: 'chat:c' },
    },
    focusedPaneId: 'pA',
    nextId: 2,
  })
  const after = paneModel.openTab(base, makeTab('chat', 'b'))
  assert.equal(paneModel.flatten(after).length, 3, 'no duplicate created')
  assert.equal(after.focusedPaneId, 'pB', 'focus moves to the pane that owns it')
  assert.equal(after.panes.pB.activeTabKey, 'chat:b', 'and it becomes active there')
  assertInvariants(after)
})

test('openTab eviction is byte-identical to the legacy flat strip (oldest goes)', () => {
  // Legacy tabModel.addTab kept the last six and evicted the OLDEST
  // unconditionally, and reopening an existing tab left the flat array unchanged.
  // PR1's gate is no visible strip change, so [A..F] -> reopen A -> open G must
  // still yield [B,C,D,E,F,G] — no active-tab protection (that ships with PR3).
  let ws = paneModel.seedFromFlatTabs([])
  for (const letter of ['A', 'B', 'C', 'D', 'E', 'F']) {
    ws = paneModel.openTab(ws, makeTab('chat', letter))
  }
  assert.deepEqual(
    paneModel.flatten(ws).map(tabKey),
    ['chat:A', 'chat:B', 'chat:C', 'chat:D', 'chat:E', 'chat:F'],
  )

  // Reopen A: the flat array (order) is unchanged, exactly like legacy.
  const reopened = paneModel.openTab(ws, makeTab('chat', 'A'))
  assert.deepEqual(paneModel.flatten(reopened).map(tabKey), paneModel.flatten(ws).map(tabKey))

  const afterG = paneModel.openTab(reopened, makeTab('chat', 'G'))
  assert.deepEqual(
    paneModel.flatten(afterG).map(tabKey),
    ['chat:B', 'chat:C', 'chat:D', 'chat:E', 'chat:F', 'chat:G'],
    'oldest (A) evicted despite being reopened/active — legacy parity',
  )
  assertInvariants(afterG)
})

test('seedFromFlatTabs caps to the last MAX_PANE_TABS after dedup', () => {
  const tabs = []
  for (let i = 0; i < paneModel.MAX_PANE_TABS + 3; i += 1) tabs.push(makeTab('chat', `c${i}`))
  const ws = paneModel.seedFromFlatTabs(tabs)
  const flat = paneModel.flatten(ws).map(tabKey)
  assert.equal(flat.length, paneModel.MAX_PANE_TABS, 'over-cap seed is trimmed')
  assert.equal(flat.at(-1), 'chat:c8', 'the last tabs are the ones kept')
  assert.equal(flat[0], 'chat:c3', 'the oldest overflow is dropped')
  assertInvariants(ws)
})

test('closeTab activates the neighbour before it, or the new last when at index 0', () => {
  const base = paneModel.normalize({
    v: 1,
    layout: 'p0',
    panes: {
      p0: {
        id: 'p0',
        tabs: [makeTab('chat', 'a'), makeTab('chat', 'b'), makeTab('chat', 'c'), makeTab('chat', 'd')],
        activeTabKey: 'chat:c',
      },
    },
    focusedPaneId: 'p0',
    nextId: 1,
  })
  const afterMid = paneModel.closeTab(base, 'chat:c')
  assert.equal(afterMid.panes.p0.activeTabKey, 'chat:b', 'neighbour at index-1 activates')

  const headActive = paneModel.setActiveTab(base, 'p0', 'chat:a')
  const afterHead = paneModel.closeTab(headActive, 'chat:a')
  assert.equal(afterHead.panes.p0.activeTabKey, 'chat:d', 'closing the head activates the new last')

  assert.equal(paneModel.closeTab(base, 'chat:absent'), base, 'closing an absent tab is a no-op')
  assertInvariants(afterMid)
})

test('closeTab that empties a pane collapses the workspace', () => {
  const ws = paneModel.normalize({
    v: 1,
    layout: { id: 's0', dir: 'row', a: 'pA', b: 'pB', ratio: 0.5 },
    panes: {
      pA: { id: 'pA', tabs: [makeTab('chat', 'a')], activeTabKey: 'chat:a' },
      pB: { id: 'pB', tabs: [makeTab('chat', 'b')], activeTabKey: 'chat:b' },
    },
    focusedPaneId: 'pB',
    nextId: 2,
  })
  const after = paneModel.closeTab(ws, 'chat:b')
  assert.equal(after.layout, 'pA', 'the emptied pane and its split are gone')
  assert.equal(after.focusedPaneId, 'pA')
  assertInvariants(after)
})

test('moveTab edge split creates a new focused pane on the named side', () => {
  const seed = paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('app', 7)])
  const ws = paneModel.moveTab(seed, 'app:7', { paneId: 'p0', edge: 'right' })
  assert.equal(splitDepth(ws.layout), 1)
  assert.equal(paneIdsOf(ws.layout).length, 2)
  assert.equal(ws.layout.dir, 'row', 'left/right is a row split')
  const [first, second] = paneIdsOf(ws.layout)
  assert.equal(first, 'p0', 'the original stays on the left for a right-edge drop')
  assert.equal(ws.focusedPaneId, second, 'the new pane takes focus')
  assert.deepEqual(paneModel.paneOf(ws, 'app:7').id, second)
  assert.deepEqual(paneModel.flatten(seed).length, 2)
  assertInvariants(ws)
})

test('moveTab top edge is a col split with the new pane first', () => {
  const seed = paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('chat', 'b')])
  const ws = paneModel.moveTab(seed, 'chat:b', { paneId: 'p0', edge: 'top' })
  assert.equal(ws.layout.dir, 'col', 'top/bottom is a col split')
  assert.equal(paneIdsOf(ws.layout)[0], ws.focusedPaneId, 'top drop puts the new pane first')
  assertInvariants(ws)
})

test('moveTab into an existing pane at an index reorders and re-homes the tab', () => {
  const base = paneModel.moveTab(
    paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('chat', 'b'), makeTab('chat', 'c')]),
    'chat:c', { paneId: 'p0', edge: 'right' },
  )
  const dest = paneModel.paneOf(base, 'chat:c').id
  const moved = paneModel.moveTab(base, 'chat:a', { paneId: dest, index: 0 })
  assert.deepEqual(paneModel.paneOf(moved, 'chat:a').id, dest)
  assert.equal(tabKey(moved.panes[dest].tabs[0]), 'chat:a', 'inserted at the caret index')
  assert.equal(moved.focusedPaneId, dest)
  assertInvariants(moved)
})

test('moveTab into a full destination pane is a no-op; a same-pane reorder is not', () => {
  // pB is at cap (6 tabs); pA has one tab to move.
  const full = []
  for (let i = 0; i < paneModel.MAX_PANE_TABS; i += 1) full.push(makeTab('chat', `b${i}`))
  const ws = paneModel.normalize({
    v: 1,
    layout: { id: 's0', dir: 'row', a: 'pA', b: 'pB', ratio: 0.5 },
    panes: {
      pA: { id: 'pA', tabs: [makeTab('chat', 'a')], activeTabKey: 'chat:a' },
      pB: { id: 'pB', tabs: full, activeTabKey: 'chat:b0' },
    },
    focusedPaneId: 'pA',
    nextId: 2,
  })
  assert.equal(paneModel.moveTab(ws, 'chat:a', { paneId: 'pB', index: 0 }), ws,
    'a cross-pane move into a capped pane is refused (no eviction contract for a drag)')

  // A reorder within the full pane itself is allowed — the count does not change.
  const reordered = paneModel.moveTab(ws, 'chat:b5', { paneId: 'pB', index: 0 })
  assert.equal(tabKey(reordered.panes.pB.tabs[0]), 'chat:b5', 'same-pane reorder still works at cap')
  assertInvariants(reordered)
})

test('moveTab refuses a fifth pane and refuses depth beyond two', () => {
  // Four leaves at depth two, one pane carrying a spare tab so the source is not
  // emptied by the move.
  const four = paneModel.normalize({
    v: 1,
    layout: {
      id: 's0', dir: 'row', ratio: 0.5,
      a: { id: 's1', dir: 'col', a: 'p1', b: 'p2', ratio: 0.5 },
      b: { id: 's2', dir: 'col', a: 'p3', b: 'p4', ratio: 0.5 },
    },
    panes: {
      p1: { id: 'p1', tabs: [makeTab('chat', 'a'), makeTab('chat', 'spare')], activeTabKey: 'chat:a' },
      p2: { id: 'p2', tabs: [makeTab('chat', 'b')], activeTabKey: 'chat:b' },
      p3: { id: 'p3', tabs: [makeTab('chat', 'c')], activeTabKey: 'chat:c' },
      p4: { id: 'p4', tabs: [makeTab('chat', 'd')], activeTabKey: 'chat:d' },
    },
    focusedPaneId: 'p1',
    nextId: 5,
  })
  assert.equal(paneIdsOf(four.layout).length, 4)
  const fifth = paneModel.moveTab(four, 'chat:spare', { paneId: 'p2', edge: 'right' })
  assert.equal(fifth, four, 'a fifth pane is refused as a same-reference no-op')

  // Three leaves at depth two, deepest pane carrying a spare tab.
  const three = paneModel.normalize({
    v: 1,
    layout: {
      id: 's0', dir: 'row', ratio: 0.5, a: 'p1',
      b: { id: 's1', dir: 'col', a: 'p2', b: 'p3', ratio: 0.5 },
    },
    panes: {
      p1: { id: 'p1', tabs: [makeTab('chat', 'a')], activeTabKey: 'chat:a' },
      p2: { id: 'p2', tabs: [makeTab('chat', 'b')], activeTabKey: 'chat:b' },
      p3: { id: 'p3', tabs: [makeTab('chat', 'c'), makeTab('chat', 'spare')], activeTabKey: 'chat:c' },
    },
    focusedPaneId: 'p3',
    nextId: 4,
  })
  const deeper = paneModel.moveTab(three, 'chat:spare', { paneId: 'p3', edge: 'bottom' })
  assert.equal(paneIdsOf(three.layout).length, 3)
  assert.equal(deeper, three, 'a depth-three split is refused even with panes to spare')
})

test('moveTab root edge wraps the whole tree in a new split', () => {
  // p0 keeps a spare tab so moving chat:a out does not empty and collapse it.
  const two = paneModel.moveTab(
    paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('chat', 'b'), makeTab('chat', 'spare')]),
    'chat:b', { paneId: 'p0', edge: 'right' },
  )
  assert.equal(paneIdsOf(two.layout).length, 2)
  const three = paneModel.moveTab(two, 'chat:a', { root: true, edge: 'left' })
  assert.equal(paneIdsOf(three.layout).length, 3)
  assert.equal(paneIdsOf(three.layout)[0], three.focusedPaneId, 'root left-edge is the new first leaf')
  assertInvariants(three)
})

test('moveTab rejects a malformed edge instead of silently splitting', () => {
  const seed = paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('chat', 'b')])
  assert.equal(paneModel.moveTab(seed, 'chat:b', { paneId: 'p0', edge: 'diagonal' }), seed,
    'an unknown edge no-ops (would have coerced to a bottom col-split)')
  assert.equal(paneModel.moveTab(seed, 'chat:b', { root: true, edge: 'sideways' }), seed,
    'an unknown root edge no-ops too')
})

test('setActiveTab, focusPane, and setRatio each no-op on unchanged input', () => {
  const ws = paneModel.moveTab(
    paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('chat', 'b')]),
    'chat:b', { paneId: 'p0', edge: 'right' },
  )
  const splitId = ws.layout.id

  assert.equal(paneModel.setActiveTab(ws, 'p0', 'chat:absent'), ws, 'non-member active is a no-op')
  assert.equal(paneModel.setActiveTab(ws, 'p0', ws.panes.p0.activeTabKey), ws, 'same active is a no-op')
  assert.equal(paneModel.focusPane(ws, ws.focusedPaneId), ws, 'same focus is a no-op')
  assert.equal(paneModel.focusPane(ws, 'nope'), ws, 'unknown pane is a no-op')
  assert.equal(paneModel.setRatio(ws, 'no-such-split', 0.5), ws, 'unknown split is a no-op')

  const focused = paneModel.focusPane(ws, 'p0')
  assert.equal(focused.focusedPaneId, 'p0')
  const resized = paneModel.setRatio(ws, splitId, 0.95)
  assert.equal(resized.layout.ratio, 0.9, 'ratio is clamped to the max')
  assertInvariants(resized)
})

test('prune drops dead-backed tabs; a null live set keeps everything', () => {
  const ws = paneModel.seedFromFlatTabs([
    makeTab('chat', 'a'), makeTab('chat', 'b'), makeTab('app', 7), makeTab('app', 9),
  ])
  const kept = paneModel.prune(ws, {
    liveChatIds: ['a'],
    liveAppIds: [7],
  })
  assert.deepEqual(paneModel.flatten(kept), [makeTab('chat', 'a'), makeTab('app', 7)])

  assert.equal(paneModel.prune(ws, {}), ws, 'unknown live sets keep everything (same reference)')
  assert.equal(
    paneModel.prune(ws, { liveChatIds: null, liveAppIds: undefined }), ws,
    'explicit null/undefined means unknown, keep',
  )
  assertInvariants(kept)
})

test('visibleTabs returns each pane active tab in order', () => {
  const ws = paneModel.moveTab(
    paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('chat', 'b')]),
    'chat:b', { paneId: 'p0', edge: 'right' },
  )
  assert.deepEqual(paneModel.visibleTabs(ws).map(tabKey), ['chat:a', 'chat:b'])
})

// ── Projection: geometry (projectLayout / modeForRect / canSplit) ───────────

// A depth-2, four-leaf tree: s0 row → s1 col(p1,p2) | s2 col(p3,p4).
function fourPaneWs(focusedPaneId = 'p1') {
  return paneModel.normalize({
    v: 1,
    layout: {
      id: 's0', dir: 'row', ratio: 0.5,
      a: { id: 's1', dir: 'col', a: 'p1', b: 'p2', ratio: 0.5 },
      b: { id: 's2', dir: 'col', a: 'p3', b: 'p4', ratio: 0.5 },
    },
    panes: {
      p1: { id: 'p1', tabs: [makeTab('chat', 'a')], activeTabKey: 'chat:a' },
      p2: { id: 'p2', tabs: [makeTab('chat', 'b')], activeTabKey: 'chat:b' },
      p3: { id: 'p3', tabs: [makeTab('chat', 'c')], activeTabKey: 'chat:c' },
      p4: { id: 'p4', tabs: [makeTab('chat', 'd')], activeTabKey: 'chat:d' },
    },
    focusedPaneId,
    nextId: 5,
  })
}

// A two-pane split on the named edge, keeping a spare tab so the source pane
// survives the move.
function twoPaneWs(edge) {
  return paneModel.moveTab(
    paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('chat', 'b'), makeTab('chat', 'spare')]),
    'chat:b', { paneId: 'p0', edge },
  )
}

test('modeForRect maps usable content size to a mode (both dims must clear)', () => {
  assert.equal(paneModel.modeForRect({ w: 1000, h: 700 }), 'wide')
  assert.equal(paneModel.modeForRect({ w: 960, h: 600 }), 'wide', 'exact threshold is wide')
  assert.equal(paneModel.modeForRect({ w: 959, h: 600 }), 'compact', 'a hair under width drops a tier')
  assert.equal(paneModel.modeForRect({ w: 1000, h: 599 }), 'compact', 'height under 600 drops to compact')
  assert.equal(paneModel.modeForRect({ w: 700, h: 520 }), 'compact', 'exact compact threshold')
  assert.equal(paneModel.modeForRect({ w: 699, h: 520 }), 'phone', 'under compact width is phone')
  assert.equal(paneModel.modeForRect({ w: 1000, h: 519 }), 'phone', 'height under 520 is phone')
  assert.equal(paneModel.modeForRect({ w: 400, h: 800 }), 'phone', 'a tall narrow phone')
  assert.equal(paneModel.modeForRect(), 'phone', 'a missing rect is the phone floor')
})

test('projectLayout never mutates the workspace', () => {
  const ws = fourPaneWs('p3')
  const before = JSON.stringify(ws)
  paneModel.projectLayout(ws, 'wide', { w: 1200, h: 800 })
  paneModel.projectLayout(ws, 'compact', { w: 800, h: 600 })
  paneModel.projectLayout(ws, 'phone', { w: 420, h: 900 })
  assert.equal(JSON.stringify(ws), before, 'projection is pure — ws is untouched')
})

test('projectLayout returns the single-pane sentinel for a one-leaf tree', () => {
  const ws = paneModel.seedFromFlatTabs([makeTab('chat', 'a')])
  for (const mode of ['wide', 'compact', 'phone']) {
    const proj = paneModel.projectLayout(ws, mode, { w: 1200, h: 800 })
    assert.deepEqual(proj.visibleLeaves, ['p0'], `${mode}: exactly one visible leaf`)
    assert.deepEqual(proj.rects.p0, { x: 0, y: 0, w: 1200, h: 800 }, `${mode}: full content rect, no margin`)
    assert.deepEqual(proj.dividers, [], `${mode}: no dividers`)
  }
})

test('projectLayout wide: a row split fills the box with a gap and one divider', () => {
  const ws = twoPaneWs('right')     // layout: s? row a=p0 b=pNew
  const [left, right] = paneModel.projectLayout(ws, 'wide', { w: 1000, h: 700 }).visibleLeaves
  const proj = paneModel.projectLayout(ws, 'wide', { w: 1000, h: 700 })
  const L = proj.rects[left]
  const R = proj.rects[right]
  assert.equal(L.x, paneModel.OUTER_MARGIN, 'left starts at the outer margin')
  assert.equal(L.y, paneModel.OUTER_MARGIN)
  assert.equal(L.h, 700 - 2 * paneModel.OUTER_MARGIN, 'full box height')
  assert.equal(R.h, L.h)
  assert.equal(R.x, L.x + L.w + paneModel.PANE_GAP, 'right sits a gap past the left')
  assert.equal(
    L.w + paneModel.PANE_GAP + R.w, 1000 - 2 * paneModel.OUTER_MARGIN,
    'the two panes plus the gap fill the inset box',
  )
  assert.equal(proj.dividers.length, 1)
  const d = proj.dividers[0]
  assert.equal(d.dir, 'row')
  assert.equal(d.x, L.x + L.w, 'divider sits in the gap at the left/right seam')
  assert.equal(d.w, paneModel.PANE_GAP)
  assert.equal(d.origin, L.x, 'origin is the split box axis start')
  assert.equal(d.span, L.w + R.w, 'span is the usable axis length ratio maps over')
})

test('projectLayout wide: ratio drives the split fractions', () => {
  const base = twoPaneWs('right')
  const wide = paneModel.setRatio(base, base.layout.id, 0.7)
  const proj = paneModel.projectLayout(wide, 'wide', { w: 1000, h: 700 })
  const [left, right] = proj.visibleLeaves
  const usable = proj.rects[left].w + proj.rects[right].w
  assert.equal(proj.rects[left].w, Math.round(usable * 0.7), 'left takes ~70%')
  assert.ok(Math.abs(proj.dividers[0].ratio - 0.7) < 1e-9, 'divider reports the effective ratio')
})

test('projectLayout wide: a depth-2 four-pane tree yields four rects and three dividers', () => {
  const proj = paneModel.projectLayout(fourPaneWs('p1'), 'wide', { w: 1400, h: 900 })
  assert.deepEqual(proj.visibleLeaves.sort(), ['p1', 'p2', 'p3', 'p4'])
  assert.equal(Object.keys(proj.rects).length, 4)
  assert.equal(proj.dividers.length, 3, 'one divider per split (s0 outer, s1 + s2 inner)')
  const ids = proj.dividers.map(d => d.splitId).sort()
  assert.deepEqual(ids, ['s0', 's1', 's2'])
  // The inner col dividers span only their own column, not the whole width.
  const outer = proj.dividers.find(d => d.splitId === 's0')
  const inner = proj.dividers.find(d => d.splitId === 's1')
  assert.equal(outer.dir, 'row')
  assert.equal(inner.dir, 'col')
  assert.ok(inner.w <= proj.rects.p1.w + 1, 'inner col divider spans its column width')
})

test('projectLayout wide: the render-time px clamp keeps both children usable', () => {
  const skewed = paneModel.setRatio(twoPaneWs('right'), twoPaneWs('right').layout.id, 0.95)
  const proj = paneModel.projectLayout(skewed, 'wide', { w: 640, h: 600 })
  const [left, right] = proj.visibleLeaves
  assert.ok(proj.rects[left].w >= paneModel.MIN_PANE_W, 'left clamped to the 280px minimum')
  assert.ok(proj.rects[right].w >= paneModel.MIN_PANE_W, 'right clamped to the 280px minimum')
  assert.ok(proj.dividers[0].ratio < 0.95, 'the stored 0.95 is clamped down at render')
})

test('projectLayout wide: a box too small for two minimums degrades to an even split', () => {
  // usable = box.w - gap; box.w = w - 2*margin. Pick w so usable can not seat
  // two 280px panes (hi < lo in clampRatio) → 0.5.
  const proj = paneModel.projectLayout(twoPaneWs('right'), 'wide', { w: 523, h: 600 })
  assert.equal(proj.dividers[0].ratio, 0.5, 'degenerate split falls back to 50/50')
})

test('projectLayout compact: a two-pane split shows the pair along the parent axis', () => {
  const proj = paneModel.projectLayout(twoPaneWs('right'), 'compact', { w: 900, h: 600 })
  assert.equal(proj.visibleLeaves.length, 2)
  assert.equal(proj.dividers.length, 1, 'a compact pair renders along its own axis, so it has a divider')
  assert.equal(proj.dividers[0].dir, 'row', 'a row parent lays the pair side by side')
})

test('projectLayout compact: a nested focused leaf pairs with its immediate sibling', () => {
  // Focus p3 (in s2, the right column). Its immediate parent is s2; the sibling
  // rep is p4. p1/p2 (the left column) are NOT shown.
  const proj = paneModel.projectLayout(fourPaneWs('p3'), 'compact', { w: 900, h: 600 })
  assert.deepEqual(proj.visibleLeaves, ['p3', 'p4'], 'focused leaf + its col sibling only')
  assert.equal(proj.dividers[0].dir, 'col', 'their shared parent s2 is a col split')
  assert.equal(proj.dividers[0].splitId, 's2')
})

test('projectLayout phone: the pair is always stacked; a row parent maps no divider', () => {
  // A row-split pair on a phone renders stacked (col) at a fixed 50/50 with NO
  // divider — the row ratio does not map to a vertical drag.
  const proj = paneModel.projectLayout(twoPaneWs('right'), 'phone', { w: 420, h: 900 })
  const [top, bottom] = proj.visibleLeaves
  assert.equal(proj.dividers.length, 0, 'no divider for a row-parented phone pair')
  assert.equal(proj.rects[top].w, proj.rects[bottom].w, 'stacked panes share the full width')
  assert.equal(proj.rects[bottom].y, proj.rects[top].y + proj.rects[top].h + paneModel.PANE_GAP)
  assert.ok(Math.abs(proj.rects[top].h - proj.rects[bottom].h) <= 1, 'a row parent stacks 50/50')
})

test('projectLayout phone: a col parent maps its ratio and keeps a divider', () => {
  const colPair = paneModel.setRatio(twoPaneWs('bottom'), twoPaneWs('bottom').layout.id, 0.7)
  const proj = paneModel.projectLayout(colPair, 'phone', { w: 420, h: 900 })
  assert.equal(proj.dividers.length, 1, 'a col-parented phone pair renders along its axis')
  assert.equal(proj.dividers[0].dir, 'col')
  const [top, bottom] = proj.visibleLeaves
  assert.ok(proj.rects[top].h > proj.rects[bottom].h, 'the 0.7 ratio is honored (top taller)')
})

test('projectLayout phone: a nested focused leaf still pairs with its sibling, stacked', () => {
  const proj = paneModel.projectLayout(fourPaneWs('p3'), 'phone', { w: 420, h: 900 })
  assert.deepEqual(proj.visibleLeaves, ['p3', 'p4'])
  assert.equal(proj.rects.p3.x, proj.rects.p4.x, 'stacked — same x')
  assert.equal(proj.dividers.length, 1, 's2 is a col split, so the stacked pair maps its ratio')
})

test('canSplit: mode edge restrictions (phone allows only top/bottom)', () => {
  const ws = paneModel.seedFromFlatTabs([makeTab('chat', 'a')])
  const big = { w: 1000, h: 700 }
  for (const edge of ['left', 'right', 'top', 'bottom']) {
    assert.equal(paneModel.canSplit(ws, 'p0', edge, 'wide', big), true, `wide allows ${edge}`)
  }
  assert.equal(paneModel.canSplit(ws, 'p0', 'left', 'phone', { w: 420, h: 900 }), false, 'phone forbids left')
  assert.equal(paneModel.canSplit(ws, 'p0', 'right', 'phone', { w: 420, h: 900 }), false, 'phone forbids right')
  assert.equal(paneModel.canSplit(ws, 'p0', 'top', 'phone', { w: 420, h: 900 }), true, 'phone allows top')
  assert.equal(paneModel.canSplit(ws, 'p0', 'bottom', 'phone', { w: 420, h: 900 }), true, 'phone allows bottom')
  assert.equal(paneModel.canSplit(ws, 'p0', 'diagonal', 'wide', big), false, 'a non-edge is never splittable')
})

test('canSplit: MAX_PANES and MAX_DEPTH bounds', () => {
  const big = { w: 2000, h: 1400 }
  // Four leaves already → no fifth pane.
  assert.equal(paneModel.canSplit(fourPaneWs('p1'), 'p1', 'right', 'wide', big), false, 'a fifth pane is refused')

  // Three leaves, a leaf at depth two → a split there would be depth three.
  const three = paneModel.normalize({
    v: 1,
    layout: {
      id: 's0', dir: 'row', ratio: 0.5, a: 'p1',
      b: { id: 's1', dir: 'col', a: 'p2', b: 'p3', ratio: 0.5 },
    },
    panes: {
      p1: { id: 'p1', tabs: [makeTab('chat', 'a')], activeTabKey: 'chat:a' },
      p2: { id: 'p2', tabs: [makeTab('chat', 'b')], activeTabKey: 'chat:b' },
      p3: { id: 'p3', tabs: [makeTab('chat', 'c')], activeTabKey: 'chat:c' },
    },
    focusedPaneId: 'p2',
    nextId: 4,
  })
  assert.equal(paneModel.canSplit(three, 'p2', 'right', 'wide', big), false, 'a depth-three split is refused')
  assert.equal(paneModel.canSplit(three, 'p1', 'top', 'wide', big), true, 'a depth-one leaf can still split')
})

test('canSplit: minimum pane size within the current projected rect', () => {
  const ws = paneModel.seedFromFlatTabs([makeTab('chat', 'a')])
  // A row split needs (w - gap)/2 ≥ 280 on each half; 560 is just under.
  assert.equal(paneModel.canSplit(ws, 'p0', 'right', 'wide', { w: 560, h: 600 }), false,
    'too narrow to seat two 280px columns')
  // But a col split of the same rect works — height is ample.
  assert.equal(paneModel.canSplit(ws, 'p0', 'top', 'wide', { w: 560, h: 600 }), true,
    'a vertical split fits because each half clears 200px')
  // A short rect fails the col split on height.
  assert.equal(paneModel.canSplit(ws, 'p0', 'bottom', 'wide', { w: 1000, h: 380 }), false,
    'too short to seat two 200px rows')
  assert.equal(paneModel.canSplit(ws, 'nope', 'right', 'wide', { w: 1000, h: 700 }), false,
    'an unknown pane can not split')
})

test('flattenRollbackPriority keeps the focused active tab through legacy truncation', () => {
  // Two panes, eight tabs, focus on pB whose active tab is chat:f6.
  const ws = paneModel.normalize({
    v: 1,
    layout: { id: 's0', dir: 'row', a: 'pA', b: 'pB', ratio: 0.5 },
    panes: {
      pA: {
        id: 'pA',
        tabs: [makeTab('chat', 'f0'), makeTab('chat', 'f1'), makeTab('chat', 'f2'), makeTab('chat', 'f3')],
        activeTabKey: 'chat:f1',
      },
      pB: {
        id: 'pB',
        tabs: [makeTab('chat', 'f4'), makeTab('chat', 'f5'), makeTab('chat', 'f6'), makeTab('chat', 'f7')],
        activeTabKey: 'chat:f6',
      },
    },
    focusedPaneId: 'pB',
    nextId: 2,
  })

  const rollback = paneModel.flattenRollbackPriority(ws)
  // Background pane first, then focused pane's other tabs, then its active last.
  assert.equal(tabKey(rollback.at(-1)), 'chat:f6', 'the focused active tab is dead last')

  // The legacy key keeps only the last MAX_TABS; the round-trip must preserve the
  // focused pane's tabs and its active tab.
  const store = fakeStorage()
  tabModel.writeOpenTabs(rollback, store)
  const survivors = tabModel.readOpenTabs(store).map(tabKey)
  assert.equal(survivors.length, tabModel.MAX_TABS)
  for (const key of ['chat:f4', 'chat:f5', 'chat:f6', 'chat:f7']) {
    assert.ok(survivors.includes(key), `focused pane tab ${key} survives rollback`)
  }
  assert.equal(survivors.at(-1), 'chat:f6', 'and its active tab is the last kept')
})

test('readWorkspaceRaw survives a throwing storage instead of crashing boot', () => {
  const throwing = {
    getItem() { throw new DOMException('The operation is insecure.', 'SecurityError') },
  }
  // Must not throw — sessionStorage.getItem can raise in a sandboxed frame, and
  // the Shell reads it while building the reducer's initial state.
  assert.equal(paneModel.readWorkspaceRaw(throwing), null)
  // The null feeds parseWorkspace, which then seeds from the flat fallback.
  const ws = paneModel.parseWorkspace(paneModel.readWorkspaceRaw(throwing), {
    fallbackTabs: [makeTab('chat', 'a')],
  })
  assert.deepEqual(ws, paneModel.seedFromFlatTabs([makeTab('chat', 'a')]))

  const working = fakeStorage(JSON.stringify(paneModel.seedFromFlatTabs([makeTab('chat', 'z')])))
  assert.ok(typeof paneModel.readWorkspaceRaw(working) === 'string', 'a healthy storage reads through')
})

test('serialize/parse round-trips a valid workspace', () => {
  const ws = paneModel.moveTab(
    paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('app', 7)]),
    'app:7', { paneId: 'p0', edge: 'bottom' },
  )
  const back = paneModel.parseWorkspace(paneModel.serializeWorkspace(ws), { fallbackTabs: [] })
  assert.deepEqual(back, ws)
})

test('parseWorkspace falls back on garbage, wrong version, and too-deep trees', () => {
  const fallbackTabs = [makeTab('chat', 'seed')]
  const seed = paneModel.seedFromFlatTabs(fallbackTabs)

  assert.deepEqual(paneModel.parseWorkspace('not json {{{', { fallbackTabs }), seed)
  assert.deepEqual(paneModel.parseWorkspace(null, { fallbackTabs }), seed)
  assert.deepEqual(paneModel.parseWorkspace('', { fallbackTabs }), seed)
  assert.deepEqual(
    paneModel.parseWorkspace(JSON.stringify({ v: 2, layout: 'p0', panes: {} }), { fallbackTabs }),
    seed,
  )

  // A structurally-typed but too-deep (depth-three) tree survives normalize
  // unchanged, so parse rejects it and falls back.
  const tooDeep = JSON.stringify({
    v: 1,
    layout: {
      id: 's0', dir: 'row', ratio: 0.5, a: 'p1',
      b: { id: 's1', dir: 'col', ratio: 0.5, a: 'p2', b: { id: 's2', dir: 'row', a: 'p3', b: 'p4', ratio: 0.5 } },
    },
    panes: {
      p1: { id: 'p1', tabs: [makeTab('chat', 'a')], activeTabKey: 'chat:a' },
      p2: { id: 'p2', tabs: [makeTab('chat', 'b')], activeTabKey: 'chat:b' },
      p3: { id: 'p3', tabs: [makeTab('chat', 'c')], activeTabKey: 'chat:c' },
      p4: { id: 'p4', tabs: [makeTab('chat', 'd')], activeTabKey: 'chat:d' },
    },
    focusedPaneId: 'p1',
    nextId: 5,
  })
  assert.deepEqual(paneModel.parseWorkspace(tooDeep, { fallbackTabs }), seed)
})

test('parseWorkspace repairs a recoverable blob instead of falling back', () => {
  // Unknown-kind + non-numeric app tabs, a duplicate across panes, a dead pane
  // ref, an out-of-range ratio, and a dead focus — all repairable by normalize.
  const raw = JSON.stringify({
    v: 1,
    layout: { id: 's0', dir: 'row', ratio: 9, a: 'pA', b: 'pMissing' },
    panes: {
      pA: {
        id: 'pA',
        tabs: [
          { kind: 'chat', id: 'a' },
          { kind: 'bogus', id: 'x' },
          { kind: 'app', id: 'not-a-number' },
          { kind: 'app', id: 7 },
        ],
        activeTabKey: 'chat:a',
      },
      pB: { id: 'pB', tabs: [{ kind: 'chat', id: 'a' }], activeTabKey: 'chat:a' },
      pOrphan: { id: 'pOrphan', tabs: [{ kind: 'chat', id: 'z' }], activeTabKey: 'chat:z' },
    },
    focusedPaneId: 'ghost',
    nextId: 3,
  })
  const ws = paneModel.parseWorkspace(raw, { fallbackTabs: [makeTab('chat', 'seed')] })
  assert.equal(ws.v, 1)
  assert.deepEqual(paneModel.flatten(ws), [makeTab('chat', 'a'), makeTab('app', 7)])
  assert.equal(ws.focusedPaneId, paneIdsOf(ws.layout)[0])
  assertInvariants(ws)
})

test('parseWorkspace falls back on a malformed split node', () => {
  const fallbackTabs = [makeTab('chat', 'seed')]
  const seed = paneModel.seedFromFlatTabs(fallbackTabs)
  const bad = JSON.stringify({
    v: 1,
    layout: { id: null, dir: 'diagonal', ratio: 0.5, a: 'pA', b: 'pB' },
    panes: {
      pA: { id: 'pA', tabs: [makeTab('chat', 'a')], activeTabKey: 'chat:a' },
      pB: { id: 'pB', tabs: [makeTab('chat', 'b')], activeTabKey: 'chat:b' },
    },
    focusedPaneId: 'pA',
    nextId: 3,
  })
  // normalize keeps the split's shape verbatim, so isValidWorkspace is what
  // catches id:null / dir:'diagonal' and forces the fallback.
  assert.deepEqual(paneModel.parseWorkspace(bad, { fallbackTabs }), seed)
})

test('parseWorkspace falls back when a pane exceeds MAX_PANE_TABS', () => {
  const fallbackTabs = [makeTab('chat', 'seed')]
  const seed = paneModel.seedFromFlatTabs(fallbackTabs)
  const over = []
  for (let i = 0; i < paneModel.MAX_PANE_TABS + 1; i += 1) over.push(makeTab('chat', `c${i}`))
  const raw = JSON.stringify({
    v: 1,
    layout: 'p0',
    panes: { p0: { id: 'p0', tabs: over, activeTabKey: 'chat:c0' } },
    focusedPaneId: 'p0',
    nextId: 1,
  })
  // normalize does not trim per-pane tab count; the cap is an accepted-on-read
  // invariant, so an over-cap blob is rejected rather than silently served.
  assert.deepEqual(paneModel.parseWorkspace(raw, { fallbackTabs }), seed)
})

test('normalize recomputes nextId so a stale generator cannot lose a tab', () => {
  // A persisted two-pane workspace whose stored nextId (1) lags its live ids
  // (pane p1 exists). The next edge move must NOT mint a colliding p1 and lose a
  // tab when the duplicate leaf collapses.
  const persisted = paneModel.parseWorkspace(JSON.stringify({
    v: 1,
    layout: { id: 's5', dir: 'row', a: 'p0', b: 'p1', ratio: 0.5 },
    panes: {
      p0: { id: 'p0', tabs: [makeTab('chat', 'keep'), makeTab('chat', 'spare')], activeTabKey: 'chat:keep' },
      p1: { id: 'p1', tabs: [makeTab('chat', 'other')], activeTabKey: 'chat:other' },
    },
    focusedPaneId: 'p0',
    nextId: 1,
  }), { fallbackTabs: [] })
  assert.ok(persisted.nextId > 5, 'nextId is recomputed past every live suffix')

  const before = new Set(paneModel.flatten(persisted).map(tabKey))
  const moved = paneModel.moveTab(persisted, 'chat:spare', { paneId: 'p1', edge: 'right' })
  const after = new Set(paneModel.flatten(moved).map(tabKey))
  for (const key of before) assert.ok(after.has(key), `${key} survived the split (no id collision)`)
  assert.equal(after.size, before.size, 'no tab lost, none duplicated')
  assertInvariants(moved)
})

test('reducer no-ops return the same state reference', () => {
  const state = paneModel.initialWorkspaceState(
    paneModel.seedFromFlatTabs([makeTab('chat', 'a')]),
  )
  assert.equal(
    paneModel.workspaceReducer(state, { type: 'FOCUS', paneId: state.ws.focusedPaneId }),
    state,
  )
  assert.equal(
    paneModel.workspaceReducer(state, { type: 'OPEN_TAB', tab: makeTab('chat', 'a'), activate: true }),
    state,
  )
  assert.equal(paneModel.workspaceReducer(state, { type: 'UNDO_LAST' }), state)
  assert.equal(paneModel.workspaceReducer(state, { type: 'WAT' }), state)
})

test('reducer UNDO_LAST restores exactly the pre-action workspace for a move', () => {
  const start = paneModel.initialWorkspaceState(
    paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('chat', 'b')]),
  )
  const moved = paneModel.workspaceReducer(start, {
    type: 'MOVE_TAB', tabKey: 'chat:b', target: { paneId: 'p0', edge: 'right' },
  })
  assert.notEqual(moved.ws, start.ws)
  assert.ok(moved.undo, 'a move is undoable')

  const undone = paneModel.workspaceReducer(moved, { type: 'UNDO_LAST' })
  assert.equal(undone.ws, start.ws, 'undo restores the exact pre-move reference')
  assert.equal(undone.undo, null, 'and clears the slot')
})

test('reducer marks only an evicting open undoable', () => {
  let state = paneModel.initialWorkspaceState(paneModel.seedFromFlatTabs([]))
  for (let i = 0; i < paneModel.MAX_PANE_TABS; i += 1) {
    state = paneModel.workspaceReducer(state, {
      type: 'OPEN_TAB', tab: makeTab('chat', `c${i}`), activate: false,
    })
  }
  assert.equal(state.undo, null, 'plain opens are not undoable')

  const evicting = paneModel.workspaceReducer(state, {
    type: 'OPEN_TAB', tab: makeTab('chat', 'new'), activate: true,
  })
  assert.ok(evicting.undo, 'an open that evicted is undoable')
  const undone = paneModel.workspaceReducer(evicting, { type: 'UNDO_LAST' })
  assert.equal(undone.ws, state.ws, 'undo brings the evicted tab back')
})

test('reducer PRUNE clears the undo slot', () => {
  const start = paneModel.initialWorkspaceState(
    paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('chat', 'b')]),
  )
  const moved = paneModel.workspaceReducer(start, {
    type: 'MOVE_TAB', tabKey: 'chat:b', target: { paneId: 'p0', edge: 'right' },
  })
  assert.ok(moved.undo)
  // A prune that removes nothing still clears the slot so Cmd/Z can't resurrect.
  const pruned = paneModel.workspaceReducer(moved, {
    type: 'PRUNE', liveChatIds: ['a', 'b'], liveAppIds: [],
  })
  assert.equal(pruned.undo, null, 'PRUNE clears the slot')
})

test('reducer APPLY_PLACEMENT preserves the active tab and is undoable', () => {
  const seeded = paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('chat', 'b'), makeTab('app', 7)])
  const start = { ws: paneModel.setActiveTab(seeded, 'p0', 'chat:a'), undo: { ws: seeded, label: 'prior' } }
  assert.equal(start.ws.panes.p0.activeTabKey, 'chat:a')

  const applied = paneModel.workspaceReducer(start, {
    type: 'APPLY_PLACEMENT',
    resolve: () => [makeTab('chat', 'a'), makeTab('app', 7), makeTab('app', 9)],
  })
  assert.deepEqual(
    paneModel.flatten(applied.ws),
    [makeTab('chat', 'a'), makeTab('app', 7), makeTab('app', 9)],
  )
  assert.equal(applied.ws.panes.p0.activeTabKey, 'chat:a', 'the surviving active tab is kept')
  assert.equal(applied.undo.ws, start.ws, 'placement snapshots the pre-placement workspace (undoable)')
})

test('reducer APPLY_PLACEMENT composes batched dispatches instead of clobbering', () => {
  // The former bug: two placements resolved against the same stale render
  // snapshot, so the second REPLACED the first. A resolve function run against
  // current reducer state makes the second see the first.
  const s0 = paneModel.initialWorkspaceState(paneModel.seedFromFlatTabs([makeTab('chat', 'home')]))
  const s1 = paneModel.workspaceReducer(s0, {
    type: 'APPLY_PLACEMENT', resolve: (tabs) => [...tabs, makeTab('app', 1)],
  })
  const s2 = paneModel.workspaceReducer(s1, {
    type: 'APPLY_PLACEMENT', resolve: (tabs) => [...tabs, makeTab('app', 2)],
  })
  const keys = paneModel.flatten(s2.ws).map(tabKey)
  assert.ok(keys.includes('app:1'), 'first placement survives the second')
  assert.ok(keys.includes('app:2'), 'second placement is applied too')
})

test('reducer clears the slot on any intervening non-undoable change', () => {
  // Single-slot undo is only for the IMMEDIATELY preceding mutation. A plain
  // (non-evicting) open after a move must clear the slot so a later UNDO cannot
  // clobber the open by restoring the stale pre-move snapshot.
  const start = paneModel.initialWorkspaceState(
    paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('chat', 'b')]),
  )
  const moved = paneModel.workspaceReducer(start, {
    type: 'MOVE_TAB', tabKey: 'chat:b', target: { paneId: 'p0', edge: 'right' },
  })
  assert.ok(moved.undo, 'the move set the slot')
  const opened = paneModel.workspaceReducer(moved, {
    type: 'OPEN_TAB', tab: makeTab('chat', 'later'), activate: true,
  })
  assert.equal(opened.undo, null, 'a plain open clears the stale slot')
  const undone = paneModel.workspaceReducer(opened, { type: 'UNDO_LAST' })
  assert.equal(undone, opened, 'UNDO_LAST is a no-op — the later tab is not clobbered')
  assert.ok(paneModel.paneOf(undone.ws, 'chat:later'), 'the later tab is still open')
})

test('reducer CLOSE_TAB reason:deleted clears the slot; a user close snapshots', () => {
  const seed = paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('chat', 'b')])

  // User close (strip ✕) is reversible.
  const userClose = paneModel.workspaceReducer(
    { ws: seed, undo: null },
    { type: 'CLOSE_TAB', tabKey: 'chat:b' },
  )
  assert.ok(userClose.undo, 'a user close is undoable')
  assert.equal(
    paneModel.workspaceReducer(userClose, { type: 'UNDO_LAST' }).ws, seed,
    'and UNDO brings the tab back',
  )

  // Deletion must NOT be resurrectable: the slot is cleared, and any pre-existing
  // slot is cleared too (an older snapshot could resurrect the deleted resource).
  const deleteClose = paneModel.workspaceReducer(
    { ws: seed, undo: { ws: null, label: 'stale' } },
    { type: 'CLOSE_TAB', tabKey: 'chat:b', reason: 'deleted' },
  )
  assert.ok(!paneModel.paneOf(deleteClose.ws, 'chat:b'), 'the tab is gone')
  assert.equal(deleteClose.undo, null, 'reason:deleted clears the slot — no resurrection')
})

test('reducer RESET_FLAT clears the slot', () => {
  const start = { ws: paneModel.seedFromFlatTabs([makeTab('chat', 'a')]), undo: { ws: null, label: 'x' } }
  const reset = paneModel.workspaceReducer(start, {
    type: 'RESET_FLAT', tabs: [makeTab('chat', 'b'), makeTab('chat', 'c')],
  })
  assert.deepEqual(paneModel.flatten(reset.ws), [makeTab('chat', 'b'), makeTab('chat', 'c')])
  assert.equal(reset.undo, null)
})

// A small seeded PRNG so a failing property run is reproducible.
function makeRng(seed) {
  let s = seed >>> 0
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0
    return s / 0x100000000
  }
}

function collectSplitIds(node, out = []) {
  if (node && typeof node === 'object') {
    out.push(node.id)
    collectSplitIds(node.a, out)
    collectSplitIds(node.b, out)
  }
  return out
}

test('property: random op sequences keep every invariant and stay normalize-stable', () => {
  const edges = ['left', 'right', 'top', 'bottom']
  for (let run = 0; run < 300; run += 1) {
    const rng = makeRng(run + 1)
    const pick = (arr) => arr[Math.floor(rng() * arr.length)]
    let ws = paneModel.seedFromFlatTabs([makeTab('chat', 'c0'), makeTab('app', 1)])

    for (let step = 0; step < 40; step += 1) {
      const paneIds = paneIdsOf(ws.layout)
      const flat = paneModel.flatten(ws).map(tabKey)
      const op = Math.floor(rng() * 9)
      switch (op) {
        case 0: {
          const kind = rng() < 0.5 ? 'chat' : 'app'
          const id = kind === 'app' ? Math.floor(rng() * 6) + 1 : `c${Math.floor(rng() * 6)}`
          ws = paneModel.openTab(ws, makeTab(kind, id), { activate: rng() < 0.7 })
          break
        }
        case 1:
          if (flat.length) ws = paneModel.closeTab(ws, pick(flat))
          break
        case 2:
          if (flat.length) ws = paneModel.moveTab(ws, pick(flat), { paneId: pick(paneIds), edge: pick(edges) })
          break
        case 3:
          if (flat.length) {
            ws = paneModel.moveTab(ws, pick(flat), {
              paneId: pick(paneIds),
              index: Math.floor(rng() * 4),
            })
          }
          break
        case 4:
          if (flat.length) ws = paneModel.moveTab(ws, pick(flat), { root: true, edge: pick(edges) })
          break
        case 5: {
          const pid = pick(paneIds)
          const keys = ws.panes[pid].tabs.map(tabKey)
          if (keys.length) ws = paneModel.setActiveTab(ws, pid, pick(keys))
          break
        }
        case 6:
          ws = paneModel.focusPane(ws, pick(paneIds))
          break
        case 7: {
          const splits = collectSplitIds(ws.layout)
          if (splits.length) ws = paneModel.setRatio(ws, pick(splits), rng())
          break
        }
        case 8:
          ws = paneModel.prune(ws, {
            liveChatIds: rng() < 0.5 ? null : ['c0', 'c1', 'c2'],
            liveAppIds: rng() < 0.5 ? null : [1, 2, 3],
          })
          break
        default:
          break
      }

      assertInvariants(ws)
      assert.equal(paneModel.normalize(ws), ws, 'every op leaves a normalized, reference-stable workspace')
    }
  }
})
