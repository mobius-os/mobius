import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as tabModel from '../tabModel.js'
import * as paneModel from '../paneModel.js'

const { makeTab, tabKey } = tabModel

// A Map-backed browser-storage stub so persistence round-trips are
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
    const keys = pane.tabs.map(tabKey)
    for (const key of keys) {
      assert.ok(!seenTab.has(key), 'a tab is unique workspace-wide')
      seenTab.add(key)
    }
    assert.deepEqual(
      [...pane.recentTabKeys].sort(), [...keys].sort(),
      'recency is a duplicate-free permutation of the pane tabs',
    )
    if (keys.length === 0) assert.equal(pane.activeTabKey, null)
    else {
      assert.ok(keys.includes(pane.activeTabKey), 'active tab is a member')
      assert.equal(pane.recentTabKeys[0], pane.activeTabKey, 'active tab is most recent')
    }
  }
}

test('seedFromFlatTabs makes a single focused pane with the last tab active', () => {
  const ws = paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('app', 42)])
  assert.equal(paneIdsOf(ws.layout).length, 1)
  assert.equal(ws.focusedPaneId, 'p0')
  assert.deepEqual(paneModel.flatten(ws), [makeTab('chat', 'a'), makeTab('app', 42)])
  assert.equal(ws.panes.p0.activeTabKey, 'app:42')
  assert.deepEqual(ws.panes.p0.recentTabKeys, ['app:42', 'chat:a'])
  assertInvariants(ws)
})

test('seedFromFlatTabs sanitizes and dedups untrusted input', () => {
  const seeded = paneModel.seedFromFlatTabs([
    { kind: 'chat', id: 'a' },
    { kind: 'app', id: 42 },        // numeric id normalizes to a string
    { kind: 'app', id: 'not-a-num' }, // dropped — would be NaN in tabNavTarget
    { kind: 'bogus', id: 'x' },      // unknown kind dropped
    { kind: 'chat', id: 'a' },       // duplicate dropped
  ])
  assert.deepEqual(paneModel.flatten(seeded), [makeTab('chat', 'a'), makeTab('app', 42)])
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
  assert.deepEqual(ws.panes.p0.recentTabKeys, ['chat:b', 'chat:a'],
    'an older blob seeds recency from the former close order')
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

test('openTab preserves every explicitly opened tab beyond the former six-tab ceiling', () => {
  let ws = paneModel.seedFromFlatTabs([])
  for (const letter of ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I']) {
    ws = paneModel.openTab(ws, makeTab('chat', letter))
  }
  assert.deepEqual(
    paneModel.flatten(ws).map(tabKey),
    ['chat:A', 'chat:B', 'chat:C', 'chat:D', 'chat:E', 'chat:F', 'chat:G', 'chat:H', 'chat:I'],
  )

  // Reopening an existing tab changes focus/activation only; ownership and order
  // remain explicit and duplicate-free.
  const reopened = paneModel.openTab(ws, makeTab('chat', 'A'))
  assert.deepEqual(paneModel.flatten(reopened).map(tabKey), paneModel.flatten(ws).map(tabKey))
  assertInvariants(reopened)
})

test('openTab afterKey inserts after the named member instead of appending (PR4)', () => {
  let ws = paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('chat', 'b')])
  ws = paneModel.openTab(ws, makeTab('app', 9), {
    paneId: 'p0', afterKey: 'chat:a', activate: false, focus: false,
  })
  assert.deepEqual(paneModel.flatten(ws).map(tabKey), ['chat:a', 'app:9', 'chat:b'])
  assert.equal(ws.panes.p0.activeTabKey, 'chat:b', 'a background insert leaves the active tab put')
})

test('openTab focus:false into another pane never moves focus (PR4 background rule)', () => {
  const ws = paneModel.normalize({
    v: 1,
    layout: { id: 's1', dir: 'row', ratio: 0.5, a: 'p0', b: 'p1' },
    panes: {
      p0: { id: 'p0', tabs: [makeTab('chat', 'a')], activeTabKey: 'chat:a' },
      p1: { id: 'p1', tabs: [makeTab('chat', 'b')], activeTabKey: 'chat:b' },
    },
    focusedPaneId: 'p0', nextId: 2,
  })
  const out = paneModel.openTab(ws, makeTab('app', 9), { paneId: 'p1', activate: false, focus: false })
  assert.equal(out.focusedPaneId, 'p0', 'focus stayed on p0')
  assert.equal(out.panes.p1.activeTabKey, 'chat:b', 'p1 on-screen tab unchanged')
  assert.deepEqual(out.panes.p1.tabs.map(tabKey), ['chat:b', 'app:9'])
})

test('splitPaneWithTab places a NEW tab alone in a fresh pane; focus:false stays put (PR4)', () => {
  const ws = paneModel.seedFromFlatTabs([makeTab('chat', 'a')])
  const bg = paneModel.splitPaneWithTab(ws, makeTab('app', 9), { paneId: 'p0', edge: 'right', focus: false })
  const leaves = paneModel.paneIdsInOrder(bg)
  assert.equal(leaves.length, 2)
  assert.equal(bg.focusedPaneId, 'p0', 'a background split does not steal focus')
  const other = bg.panes[leaves.find(id => id !== 'p0')]
  assert.deepEqual(other.tabs.map(tabKey), ['app:9'])
  assert.equal(other.activeTabKey, 'app:9', 'the item is active in the new pane')
  assertInvariants(bg)

  // focus:true moves focus to the new pane.
  const fg = paneModel.splitPaneWithTab(ws, makeTab('app', 9), { paneId: 'p0', edge: 'bottom', focus: true })
  assert.notEqual(fg.focusedPaneId, 'p0')
  assert.equal(fg.panes[fg.focusedPaneId].activeTabKey, 'app:9')

  // An already-open item, an unknown pane, or a malformed edge is a same-ref no-op.
  const withApp = paneModel.openTab(ws, makeTab('app', 9), { activate: false })
  assert.equal(paneModel.splitPaneWithTab(withApp, makeTab('app', 9), { paneId: 'p0', edge: 'right' }), withApp)
  assert.equal(paneModel.splitPaneWithTab(ws, makeTab('app', 9), { paneId: 'nope', edge: 'right' }), ws)
  assert.equal(paneModel.splitPaneWithTab(ws, makeTab('app', 9), { paneId: 'p0', edge: 'diagonal' }), ws)
})

test('splitPaneWithTab refuses to breach the pane-count / depth bound (PR4)', () => {
  // Build a balanced 4-leaf tree (MAX_PANES): row(col(p0,·), col(p1,·)). A fifth
  // split — more leaves AND deeper — must be a same-reference no-op.
  let ws = paneModel.seedFromFlatTabs(
    ['a', 'b', 'c', 'd'].map(id => makeTab('chat', id)),
  )
  ws = paneModel.moveTab(ws, 'chat:c', { paneId: 'p0', edge: 'right' })
  ws = paneModel.moveTab(ws, 'chat:b', { paneId: 'p0', edge: 'bottom' })
  ws = paneModel.moveTab(ws, 'chat:d', { paneId: 'p1', edge: 'bottom' })
  assert.equal(paneModel.paneIdsInOrder(ws).length, paneModel.MAX_PANES)
  const target = paneModel.paneIdsInOrder(ws)[0]
  assert.equal(paneModel.splitPaneWithTab(ws, makeTab('app', 9), { paneId: target, edge: 'right' }), ws)
})

test('paneIdsInOrder returns live leaves in in-order sequence (PR4)', () => {
  const ws = threeLeafAppWs()
  assert.deepEqual(paneModel.paneIdsInOrder(ws), ['p1', 'p3', 'p2'])
  assert.deepEqual(paneModel.paneIdsInOrder(paneModel.seedFromFlatTabs([makeTab('chat', 'x')])), ['p0'])
})

test('seedFromFlatTabs preserves more than six deduplicated tabs', () => {
  const tabs = Array.from({ length: 10 }, (_, i) => makeTab('chat', `c${i}`))
  tabs.push(makeTab('chat', 'c0'))
  const ws = paneModel.seedFromFlatTabs(tabs)
  const flat = paneModel.flatten(ws).map(tabKey)
  assert.equal(flat.length, 10)
  assert.equal(flat[0], 'chat:c0')
  assert.equal(flat.at(-1), 'chat:c9')
  assertInvariants(ws)
})

test('closeTab returns to the most recently visited surviving tab in that pane', () => {
  let base = paneModel.normalize({
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
  // Visit A, then C. A is not adjacent to C, so this distinguishes MRU from
  // the old left-neighbour selection.
  base = paneModel.setActiveTab(base, 'p0', 'chat:a')
  base = paneModel.setActiveTab(base, 'p0', 'chat:c')
  assert.deepEqual(base.panes.p0.recentTabKeys, ['chat:c', 'chat:a', 'chat:b', 'chat:d'])
  const afterMid = paneModel.closeTab(base, 'chat:c')
  assert.equal(afterMid.panes.p0.activeTabKey, 'chat:a', 'the previously visited tab activates')

  const afterPrevious = paneModel.closeTab(afterMid, 'chat:a')
  assert.equal(afterPrevious.panes.p0.activeTabKey, 'chat:b', 'repeated closes keep walking recency')

  assert.equal(paneModel.closeTab(base, 'chat:absent'), base, 'closing an absent tab is a no-op')
  assertInvariants(afterMid)
})

test('moving the active tab away reveals the source pane MRU and activates it at destination', () => {
  let ws = paneModel.seedFromFlatTabs(
    ['a', 'b', 'c', 'd', 'destination'].map(id => makeTab('chat', id)),
  )
  ws = paneModel.moveTab(ws, 'chat:destination', { paneId: 'p0', edge: 'right' })
  const destinationPaneId = paneModel.paneOf(ws, 'chat:destination').id

  ws = paneModel.setActiveTab(ws, 'p0', 'chat:a')
  ws = paneModel.setActiveTab(ws, 'p0', 'chat:c')
  assert.deepEqual(ws.panes.p0.recentTabKeys.slice(0, 2), ['chat:c', 'chat:a'])

  const moved = paneModel.moveTab(ws, 'chat:c', { paneId: destinationPaneId })
  assert.equal(moved.panes.p0.activeTabKey, 'chat:a', 'the source returns to its previous tab')
  assert.equal(moved.panes[destinationPaneId].activeTabKey, 'chat:c', 'the moved tab is active at destination')
  assertInvariants(moved)
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

test('moveTab re-homes a tab into a destination that already has more than six tabs', () => {
  const many = Array.from({ length: 9 }, (_, i) => makeTab('chat', `b${i}`))
  const ws = paneModel.normalize({
    v: 1,
    layout: { id: 's0', dir: 'row', a: 'pA', b: 'pB', ratio: 0.5 },
    panes: {
      pA: { id: 'pA', tabs: [makeTab('chat', 'a')], activeTabKey: 'chat:a' },
      pB: { id: 'pB', tabs: many, activeTabKey: 'chat:b0' },
    },
    focusedPaneId: 'pA',
    nextId: 2,
  })
  const moved = paneModel.moveTab(ws, 'chat:a', { paneId: 'pB', index: 0 })
  assert.notEqual(moved, ws)
  assert.equal(tabKey(moved.panes.pB.tabs[0]), 'chat:a')
  assert.equal(moved.panes.pB.tabs.length, 10)
  assert.ok(moved.panes.pB.tabs.some(tab => tabKey(tab) === 'chat:b8'))
  assertInvariants(moved)
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

test('projectLayout wide: a row split fills the edge-to-edge box with a gap and divider', () => {
  const ws = twoPaneWs('right')     // layout: s? row a=p0 b=pNew
  const [left, right] = paneModel.projectLayout(ws, 'wide', { w: 1000, h: 700 }).visibleLeaves
  const proj = paneModel.projectLayout(ws, 'wide', { w: 1000, h: 700 })
  const L = proj.rects[left]
  const R = proj.rects[right]
  assert.equal(L.x, 0, 'left starts at the content edge')
  assert.equal(L.y, 0)
  assert.equal(L.h, 700, 'full box height')
  assert.equal(R.h, L.h)
  assert.equal(R.x, L.x + L.w + paneModel.PANE_GAP, 'right sits a gap past the left')
  assert.equal(
    L.w + paneModel.PANE_GAP + R.w, 1000,
    'the two panes plus the gap fill the content box',
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

test('readWorkspaceRaw survives a throwing storage instead of crashing boot', () => {
  const throwing = {
    getItem() { throw new DOMException('The operation is insecure.', 'SecurityError') },
  }
  // Must not throw — browser storage getItem can raise in a sandboxed frame, and
  // the Shell reads it while building the reducer's initial state.
  assert.equal(paneModel.readWorkspaceRaw(throwing), null)
  // The null feeds parseWorkspace, which then seeds a safe empty workspace.
  const ws = paneModel.parseWorkspace(paneModel.readWorkspaceRaw(throwing))
  assert.deepEqual(ws, paneModel.seedFromFlatTabs([]))

  const working = fakeStorage(JSON.stringify(paneModel.seedFromFlatTabs([makeTab('chat', 'z')])))
  assert.ok(typeof paneModel.readWorkspaceRaw(working) === 'string', 'a healthy storage reads through')
})

test('serialize/parse round-trips a valid workspace', () => {
  const ws = paneModel.moveTab(
    paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('app', 7)]),
    'app:7', { paneId: 'p0', edge: 'bottom' },
  )
  const back = paneModel.parseWorkspace(paneModel.serializeWorkspace(ws))
  assert.deepEqual(back, ws)
})

test('parseWorkspace falls back on garbage, wrong version, and too-deep trees', () => {
  const seed = paneModel.seedFromFlatTabs([])

  assert.deepEqual(paneModel.parseWorkspace('not json {{{'), seed)
  assert.deepEqual(paneModel.parseWorkspace(null), seed)
  assert.deepEqual(paneModel.parseWorkspace(''), seed)
  assert.deepEqual(
    paneModel.parseWorkspace(JSON.stringify({ v: 2, layout: 'p0', panes: {} })),
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
  assert.deepEqual(paneModel.parseWorkspace(tooDeep), seed)
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
  const ws = paneModel.parseWorkspace(raw)
  assert.equal(ws.v, 1)
  assert.deepEqual(paneModel.flatten(ws), [makeTab('chat', 'a'), makeTab('app', 7)])
  assert.equal(ws.focusedPaneId, paneIdsOf(ws.layout)[0])
  assertInvariants(ws)
})

test('parseWorkspace falls back on a malformed split node', () => {
  const seed = paneModel.seedFromFlatTabs([])
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
  assert.deepEqual(paneModel.parseWorkspace(bad), seed)
})

test('parseWorkspace accepts a persisted pane with more than six tabs', () => {
  const many = Array.from({ length: 10 }, (_, i) => makeTab('chat', `c${i}`))
  const raw = JSON.stringify({
    v: 1,
    layout: 'p0',
    panes: { p0: { id: 'p0', tabs: many, activeTabKey: 'chat:c0' } },
    focusedPaneId: 'p0',
    nextId: 1,
  })
  const parsed = paneModel.parseWorkspace(raw)
  assert.deepEqual(parsed.panes.p0.tabs, many)
  assert.equal(parsed.panes.p0.activeTabKey, 'chat:c0')
  assertInvariants(parsed)
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
  }))
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

test('reducer keeps every opened tab beyond six and treats opens as non-undoable', () => {
  let state = paneModel.initialWorkspaceState(paneModel.seedFromFlatTabs([]))
  for (let i = 0; i < 10; i += 1) {
    state = paneModel.workspaceReducer(state, {
      type: 'OPEN_TAB', tab: makeTab('chat', `c${i}`), activate: false,
    })
  }
  assert.equal(state.ws.panes.p0.tabs.length, 10)
  assert.equal(state.ws.panes.p0.tabs[0].id, 'c0')
  assert.equal(state.ws.panes.p0.tabs.at(-1).id, 'c9')
  assert.equal(state.undo, null)
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

test('reducer APPLY_PLACEMENT applies a workspace-level resolver and is undoable', () => {
  const seeded = paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('chat', 'b'), makeTab('app', 7)])
  const start = { ws: paneModel.setActiveTab(seeded, 'p0', 'chat:a'), undo: { ws: seeded, label: 'prior' } }
  assert.equal(start.ws.panes.p0.activeTabKey, 'chat:a')

  // The resolver is now workspace → workspace (the pane-aware path). A background
  // open (activate:false, focus:false) must not disturb the pane's active tab —
  // the reducer just runs it and snapshots undo.
  const applied = paneModel.workspaceReducer(start, {
    type: 'APPLY_PLACEMENT',
    resolve: (ws) => paneModel.openTab(ws, makeTab('app', 9), {
      paneId: 'p0', activate: false, focus: false,
    }),
  })
  assert.deepEqual(
    paneModel.flatten(applied.ws),
    [makeTab('chat', 'a'), makeTab('chat', 'b'), makeTab('app', 7), makeTab('app', 9)],
  )
  assert.equal(applied.ws.panes.p0.activeTabKey, 'chat:a', 'a background placement leaves the active tab alone')
  assert.equal(applied.undo.ws, start.ws, 'placement snapshots the pre-placement workspace (undoable)')
})

// ── Undo-slot IDENTITY binding (design §3.5) — the UI binds its toast to the
// slot object, so each mutation must mint a NEW slot with the right toast text
// so a stale toast's Undo can never revert a mutation it does not name.

test('every undoable mutation mints a fresh slot carrying its own toast text', () => {
  const start = paneModel.initialWorkspaceState(
    paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('chat', 'b')]),
  )
  const moved = paneModel.workspaceReducer(start, {
    type: 'MOVE_TAB', tabKey: 'chat:b', target: { paneId: 'p0', edge: 'right' }, label: 'Moved B',
  })
  assert.equal(moved.undo.toast, 'Moved B', 'a drag/move names itself')

  // A subsequent agent placement REPLACES the slot with a NEW identity and its
  // OWN toast — never inheriting the drag's "Moved B" (the honesty regression).
  const placed = paneModel.workspaceReducer(moved, {
    type: 'APPLY_PLACEMENT',
    resolve: (ws) => paneModel.openTab(ws, makeTab('app', 9), { paneId: moved.ws.focusedPaneId, activate: false, focus: false }),
  })
  assert.notEqual(placed.undo, moved.undo, 'the slot identity changed')
  assert.equal(placed.undo.toast, 'Agent arranged your workspace', 'the placement names itself, not the drag')

  // Undoing the placement must restore the post-move workspace, not the pre-move
  // one — the slot the toast pointed at.
  const undone = paneModel.workspaceReducer(placed, { type: 'UNDO_LAST' })
  assert.equal(undone.ws, moved.ws, 'Undo reverts the placement it named, not the earlier move')
})

test('a divider resize is undoable but SILENT (toast:null) so it retracts a move toast', () => {
  const start = paneModel.initialWorkspaceState(
    paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('chat', 'b')]),
  )
  const moved = paneModel.workspaceReducer(start, {
    type: 'MOVE_TAB', tabKey: 'chat:b', target: { paneId: 'p0', edge: 'right' }, label: 'Moved B',
  })
  assert.equal(moved.undo.toast, 'Moved B')
  const splitId = paneModel.projectLayout(moved.ws, 'wide', { x: 0, y: 0, w: 1400, h: 900 }).dividers[0].splitId
  const resized = paneModel.workspaceReducer(moved, { type: 'SET_RATIO', splitId, ratio: 0.6 })
  assert.notEqual(resized.undo, moved.undo, 'the resize replaced the slot')
  assert.equal(resized.undo.toast, null, 'a resize carries no toast — the move toast must retract')
})

test('CLOSE_PANE closes a whole pane, is undoable, and names itself', () => {
  let ws = paneModel.seedFromFlatTabs([makeTab('chat', 'a')])
  ws = paneModel.splitPaneWithTab(ws, makeTab('app', 42), { paneId: ws.focusedPaneId, edge: 'right' })
  const paneId = ws.focusedPaneId // the new app pane
  const start = paneModel.initialWorkspaceState(ws)
  const closed = paneModel.workspaceReducer(start, { type: 'CLOSE_PANE', paneId })
  assert.equal(Object.keys(closed.ws.panes).length, 1, 'the pane collapsed away')
  assert.equal(closed.undo.toast, 'Closed pane')
  const undone = paneModel.workspaceReducer(closed, { type: 'UNDO_LAST' })
  assert.equal(undone.ws, ws, 'Undo restores the closed pane and its tabs')
})

test('CLOSE_OTHER_TABS keeps only the clicked tab, is undoable, and names itself', () => {
  const ws = paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('app', 42), makeTab('chat', 'c')])
  const paneId = ws.focusedPaneId
  const start = paneModel.initialWorkspaceState(ws)
  const closed = paneModel.workspaceReducer(start, { type: 'CLOSE_OTHER_TABS', tabKey: 'app:42' })
  assert.deepEqual(closed.ws.panes[paneId].tabs.map(tabKey), ['app:42'], 'only the kept tab survives')
  assert.equal(closed.ws.panes[paneId].activeTabKey, 'app:42', 'the kept tab becomes active')
  assert.equal(closed.undo.toast, 'Closed other tabs')
  const undone = paneModel.workspaceReducer(closed, { type: 'UNDO_LAST' })
  assert.equal(undone.ws, ws, 'Undo restores every closed sibling at once')
  // Already alone or unknown tab: same reference — no undo slot burned.
  assert.equal(paneModel.workspaceReducer(closed, { type: 'CLOSE_OTHER_TABS', tabKey: 'app:42' }), closed)
  assert.equal(paneModel.closeOtherTabs(ws, 'chat:nope'), ws)
})

test('CLOSE_OTHER_TABS is pane-scoped — a sibling pane keeps its tabs', () => {
  let ws = paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('chat', 'b')])
  const leftPane = ws.focusedPaneId
  ws = paneModel.splitPaneWithTab(ws, makeTab('app', 42), { paneId: leftPane, edge: 'right' })
  const closed = paneModel.workspaceReducer(
    paneModel.initialWorkspaceState(ws),
    { type: 'CLOSE_OTHER_TABS', tabKey: 'chat:a' },
  )
  assert.deepEqual(closed.ws.panes[leftPane].tabs.map(tabKey), ['chat:a'])
  assert.ok(
    paneModel.flatten(closed.ws).map(tabKey).includes('app:42'),
    'the other pane is untouched',
  )
})

test('CLOSE_TABS_TO_RIGHT removes only later siblings and is undoable', () => {
  const ws = paneModel.seedFromFlatTabs([
    makeTab('chat', 'a'),
    makeTab('app', 42),
    makeTab('chat', 'c'),
    makeTab('chat', 'd'),
  ])
  const paneId = ws.focusedPaneId
  const start = paneModel.initialWorkspaceState(ws)
  const closed = paneModel.workspaceReducer(start, {
    type: 'CLOSE_TABS_TO_RIGHT',
    tabKey: 'app:42',
  })
  assert.deepEqual(closed.ws.panes[paneId].tabs.map(tabKey), ['chat:a', 'app:42'])
  assert.equal(closed.ws.panes[paneId].activeTabKey, 'app:42',
    'the menu tab becomes active when the prior active tab was closed')
  assert.equal(closed.undo.toast, 'Closed tabs to the right')
  assert.equal(paneModel.workspaceReducer(closed, {
    type: 'CLOSE_TABS_TO_RIGHT',
    tabKey: 'app:42',
  }), closed, 'the last tab has no right-side action')
  assert.equal(paneModel.workspaceReducer(closed, { type: 'UNDO_LAST' }).ws, ws)
})

test('closeTabsToRight preserves a surviving active tab and other panes', () => {
  let ws = paneModel.seedFromFlatTabs([
    makeTab('chat', 'a'),
    makeTab('chat', 'b'),
    makeTab('chat', 'c'),
  ])
  const leftPane = ws.focusedPaneId
  ws = paneModel.setActiveTab(ws, leftPane, 'chat:a')
  ws = paneModel.splitPaneWithTab(ws, makeTab('app', 42), {
    paneId: leftPane,
    edge: 'right',
  })
  const closed = paneModel.closeTabsToRight(ws, 'chat:b')
  assert.deepEqual(closed.panes[leftPane].tabs.map(tabKey), ['chat:a', 'chat:b'])
  assert.equal(closed.panes[leftPane].activeTabKey, 'chat:a')
  assert.ok(paneModel.flatten(closed).map(tabKey).includes('app:42'))
  assert.equal(paneModel.closeTabsToRight(ws, 'chat:nope'), ws)
})

test('reducer APPLY_PLACEMENT composes batched dispatches instead of clobbering', () => {
  // The former bug: two placements resolved against the same stale render
  // snapshot, so the second REPLACED the first. A resolve function run against
  // current reducer state makes the second see the first.
  const s0 = paneModel.initialWorkspaceState(paneModel.seedFromFlatTabs([makeTab('chat', 'home')]))
  const s1 = paneModel.workspaceReducer(s0, {
    type: 'APPLY_PLACEMENT', resolve: (ws) => paneModel.openTab(ws, makeTab('app', 1), { activate: false }),
  })
  const s2 = paneModel.workspaceReducer(s1, {
    type: 'APPLY_PLACEMENT', resolve: (ws) => paneModel.openTab(ws, makeTab('app', 2), { activate: false }),
  })
  const keys = paneModel.flatten(s2.ws).map(tabKey)
  assert.ok(keys.includes('app:1'), 'first placement survives the second (resolve runs on current state)')
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

// ── Stage-B pure helpers ────────────────────────────────────────────────────

test('focusedContentRoute derives the legacy triple from the focused pane', () => {
  const chatWs = paneModel.seedFromFlatTabs([makeTab('chat', 'c1')])
  assert.deepEqual(paneModel.focusedContentRoute(chatWs),
    { view: 'chat', chatId: 'c1', appId: null, paneId: 'p0' })

  // App ids come back numeric (through tabModel.tabNavTarget), never parsed here.
  const appWs = paneModel.seedFromFlatTabs([makeTab('app', 5)])
  assert.deepEqual(paneModel.focusedContentRoute(appWs),
    { view: 'canvas', chatId: null, appId: 5, paneId: 'p0' })

  // An empty focused pane resolves to the empty chat surface.
  const emptyWs = paneModel.seedFromFlatTabs([])
  assert.deepEqual(paneModel.focusedContentRoute(emptyWs),
    { view: 'chat', chatId: null, appId: null, paneId: 'p0' })
})

// A three-leaf tree row(col(p1,p3), p2), with p1/p2 apps and p3 a chat.
function threeLeafAppWs() {
  return paneModel.normalize({
    v: 1,
    layout: {
      id: 's1', dir: 'row', ratio: 0.5,
      a: { id: 's2', dir: 'col', ratio: 0.5, a: 'p1', b: 'p3' },
      b: 'p2',
    },
    panes: {
      p1: { id: 'p1', tabs: [makeTab('app', 5)], activeTabKey: tabKey(makeTab('app', 5)) },
      p2: { id: 'p2', tabs: [makeTab('chat', 'c')], activeTabKey: tabKey(makeTab('chat', 'c')) },
      p3: { id: 'p3', tabs: [makeTab('app', 9)], activeTabKey: tabKey(makeTab('app', 9)) },
    },
    focusedPaneId: 'p1',
    nextId: 4,
  })
}

test('visibleAppIds returns only apps that are the active tab of a visible leaf', () => {
  const ws = threeLeafAppWs()
  // p3 (app 9) is hidden when only p1+p2 are visible.
  assert.deepEqual([...paneModel.visibleAppIds(ws, ['p1', 'p2'])].sort(), ['5'])
  // All leaves: both app panes count; the chat pane never does.
  assert.deepEqual([...paneModel.visibleAppIds(ws)].sort(), ['5', '9'])
  // A chat-only visible set yields no app ids.
  assert.deepEqual([...paneModel.visibleAppIds(ws, ['p2'])], [])
})

test('projectLayout clamps a dragged ANCESTOR ratio against child SUBTREE minima', () => {
  // row(row(p1,p2), p3) at 1400x900. Dragging the root divider toward 0.1 must
  // NOT starve the inner leaves: the left subtree needs two MIN_PANE_W + a gap,
  // so p1/p2 stay ≥ 280 (finding E-i — a per-leaf clamp yields ~137px here).
  const ws = paneModel.normalize({
    v: 1,
    layout: {
      id: 's1', dir: 'row', ratio: 0.5,
      a: { id: 's2', dir: 'row', ratio: 0.5, a: 'p1', b: 'p2' },
      b: 'p3',
    },
    panes: {
      p1: { id: 'p1', tabs: [makeTab('chat', 'a')], activeTabKey: tabKey(makeTab('chat', 'a')) },
      p2: { id: 'p2', tabs: [makeTab('chat', 'b')], activeTabKey: tabKey(makeTab('chat', 'b')) },
      p3: { id: 'p3', tabs: [makeTab('chat', 'c')], activeTabKey: tabKey(makeTab('chat', 'c')) },
    },
    focusedPaneId: 'p1',
    nextId: 4,
  })
  const proj = paneModel.projectLayout(ws, 'wide', { x: 0, y: 0, w: 1400, h: 900 },
    { splitId: 's1', ratio: 0.1 })
  assert.ok(proj.rects.p1.w >= paneModel.MIN_PANE_W, `p1 ${proj.rects.p1.w} >= ${paneModel.MIN_PANE_W}`)
  assert.ok(proj.rects.p2.w >= paneModel.MIN_PANE_W, `p2 ${proj.rects.p2.w} >= ${paneModel.MIN_PANE_W}`)
})

test('canSplit judges the first split against the edge-to-edge post-split box', () => {
  const ws = paneModel.seedFromFlatTabs([makeTab('chat', 'a')])
  // With no outer inset, 407px is the exact first legal height:
  // (407 - 7px inter-pane gap) / 2 == MIN_PANE_H.
  assert.equal(paneModel.canSplit(ws, 'p0', 'top', 'phone', { w: 400, h: 406 }), false)
  assert.equal(paneModel.canSplit(ws, 'p0', 'bottom', 'phone', { w: 400, h: 407 }), true)
  assert.equal(paneModel.canSplit(ws, 'p0', 'bottom', 'phone', { w: 400, h: 520 }), true)
})

// ── nextId is a monotonic high-water mark (no id reuse after a collapse) ──────

test('normalize never reissues a freed pane/split id (nextId does not regress)', () => {
  let ws = paneModel.seedFromFlatTabs([makeTab('chat', 'a')])
  ws = paneModel.splitPaneWithTab(ws, makeTab('chat', 'b'), { paneId: 'p0', edge: 'right' })
  assert.ok(ws.panes.p1, 'the split minted p1')
  const highWater = ws.nextId
  // Collapse the new pane back to a single pane.
  ws = paneModel.closeTab(ws, 'chat:b')
  assert.deepEqual(Object.keys(ws.panes), ['p0'], 'p1 collapsed away')
  assert.ok(ws.nextId >= highWater, 'nextId did not regress below the freed ids')
  // The NEXT split must not reuse the freed p1 (a stale history hint for the dead
  // p1 must not suddenly match a live pane).
  ws = paneModel.splitPaneWithTab(ws, makeTab('chat', 'c'), { paneId: 'p0', edge: 'right' })
  assert.equal(ws.panes.p1, undefined, 'the freed p1 id was NOT reused')
})

test('normalize repairs a stored nextId that LAGS the live ids', () => {
  // A corrupt blob whose nextId trails the live max is bumped past it, so the
  // next mint cannot collide with an existing node.
  let ws = paneModel.seedFromFlatTabs([makeTab('chat', 'a')])
  ws = paneModel.splitPaneWithTab(ws, makeTab('chat', 'b'), { paneId: 'p0', edge: 'right' })
  const corrupt = { ...ws, nextId: 1 } // lags p1/s2 in the live tree
  const fixed = paneModel.normalize(corrupt)
  assert.ok(fixed.nextId > 2, 'nextId cleared the live max even from a lagging blob')
})

// ── wide-mode degrades rather than paint panes below the minimum ─────────────

test('a 4-pane wide tree degrades to the focused pair when the box cannot fit it', () => {
  let ws = paneModel.seedFromFlatTabs([makeTab('chat', 'a')])
  ws = paneModel.splitPaneWithTab(ws, makeTab('app', 1), { paneId: 'p0', edge: 'right' })
  ws = paneModel.splitPaneWithTab(ws, makeTab('app', 2), { paneId: 'p0', edge: 'right' })
  const rightRep = paneModel.paneOf(ws, 'app:1').id
  ws = paneModel.splitPaneWithTab(ws, makeTab('app', 3), { paneId: rightRep, edge: 'right' })
  assert.equal(paneModel.paneIdsInOrder(ws).length, 4, 'built a 4-leaf horizontal tree')

  // A roomy viewport shows all four.
  const wide = paneModel.projectLayout(ws, 'wide', { x: 0, y: 0, w: 1600, h: 900 })
  assert.equal(wide.visibleLeaves.length, 4)

  // 960px cannot fit 4×280 columns, so wide degrades to the compact focused pair
  // and never paints a pane below MIN_PANE_W.
  const narrow = paneModel.projectLayout(ws, 'wide', { x: 0, y: 0, w: 960, h: 900 })
  assert.equal(narrow.visibleLeaves.length, 2, 'degraded to the focused pair')
  for (const id of narrow.visibleLeaves) {
    assert.ok(narrow.rects[id].w >= paneModel.MIN_PANE_W, `${id} clears the width floor`)
  }
})

// ── sole-item self-split is a true no-op (not a rename) ──────────────────────

test('moving a pane sole tab onto its own edge is a no-op', () => {
  const ws = paneModel.seedFromFlatTabs([makeTab('chat', 'a')])
  assert.equal(paneModel.moveTab(ws, 'chat:a', { paneId: 'p0', edge: 'right' }), ws,
    'same-pane sole-tab edge split is refused')
  assert.equal(paneModel.moveTab(ws, 'chat:a', { root: true, edge: 'right' }), ws,
    'root-splitting the sole tab of the sole pane is refused')
})

test('a drawer drop of an already-open sole item onto its pane edge does not toast', () => {
  const state = paneModel.initialWorkspaceState(paneModel.seedFromFlatTabs([makeTab('chat', 'a')]))
  const dropped = paneModel.workspaceReducer(state, {
    type: 'OPEN_TAB_AT', tab: makeTab('chat', 'a'), target: { paneId: 'p0', edge: 'right' }, label: 'Moved Chat',
  })
  assert.equal(dropped, state, 'the no-op drop leaves state (and the undo slot) untouched — no false toast')
})

// ── route-pane reconciliation follows moves and degrades dead hints ──────────

test('reconcileRoutePanes points a hint at the pane that now holds its item', () => {
  const prev = paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('chat', 'b')])
  const next = paneModel.moveTab(prev, 'chat:b', { paneId: 'p0', edge: 'right' })
  const bPane = paneModel.paneOf(next, 'chat:b').id
  assert.notEqual(bPane, 'p0', 'b left p0 for a new pane')
  const routes = [{ view: 'chat', chatId: 'b', appId: null, paneId: 'p0' }]
  const rec = paneModel.reconcileRoutePanes(routes, prev, next)
  assert.equal(rec[0].paneId, bPane, 'the moved item route followed it (source pane survived)')
})

test('reconcileRoutePanes degrades a dead-pane hint for a closed item to the sibling', () => {
  const prev = paneModel.moveTab(
    paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('chat', 'b')]),
    'chat:b', { paneId: 'p0', edge: 'right' },
  )
  const bPane = paneModel.paneOf(prev, 'chat:b').id
  const next = paneModel.closeTab(prev, 'chat:b') // bPane collapses
  assert.equal(next.panes[bPane], undefined)
  const routes = [{ view: 'chat', chatId: 'b', appId: null, paneId: bPane }]
  const rec = paneModel.reconcileRoutePanes(routes, prev, next)
  assert.equal(rec[0].paneId, 'p0', 'the dead-pane hint degraded to the surviving sibling')
})

test('reconcileRoutePanes returns the SAME array when nothing changed', () => {
  const ws = paneModel.moveTab(
    paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('chat', 'b')]),
    'chat:b', { paneId: 'p0', edge: 'right' },
  )
  const bPane = paneModel.paneOf(ws, 'chat:b').id
  const routes = [{ view: 'chat', chatId: 'b', appId: null, paneId: bPane }] // already correct
  assert.equal(paneModel.reconcileRoutePanes(routes, ws, ws), routes)
})

// ── Maximized-pane ("full-screen pane") persistence (survives apply-on-idle reload)

// A storage stub with removeItem so the clear-on-null path is exercised.
function focusStorage(initial = null) {
  let value = initial
  return {
    getItem: () => value,
    setItem: (_k, v) => { value = String(v) },
    removeItem: () => { value = null },
    peek: () => value,
  }
}

const maxPaneWs = (focusedPaneId = 'p1') => ({
  viewMode: 'panes',
  panes: { p0: {}, p1: {} },
  focusedPaneId,
})

test('resolveInitialFocusedPaneView restores a valid maximized pane', () => {
  assert.equal(
    paneModel.resolveInitialFocusedPaneView(maxPaneWs('p1'), 'p1'), 'p1',
    'a multi-pane panes-world with the stored id === focusedPaneId restores the maximize',
  )
})

test('resolveInitialFocusedPaneView returns null for a single-pane workspace', () => {
  const ws = { viewMode: 'panes', panes: { p0: {} }, focusedPaneId: 'p0' }
  assert.equal(paneModel.resolveInitialFocusedPaneView(ws, 'p0'), null)
})

test('resolveInitialFocusedPaneView returns null in single (Standard) viewMode', () => {
  assert.equal(
    paneModel.resolveInitialFocusedPaneView({ ...maxPaneWs('p1'), viewMode: 'single' }, 'p1'),
    null, 'single mode has no maximize presentation to restore',
  )
})

test('resolveInitialFocusedPaneView returns null for a vanished pane id', () => {
  assert.equal(paneModel.resolveInitialFocusedPaneView(maxPaneWs('p1'), 'p9'), null)
})

test('resolveInitialFocusedPaneView enforces the id === focusedPaneId lockstep', () => {
  // p0 exists but is NOT the focused pane; restoring it would maximize one pane's
  // rectangle while a different pane's content is active. Reject it.
  assert.equal(paneModel.resolveInitialFocusedPaneView(maxPaneWs('p1'), 'p0'), null)
})

test('resolveInitialFocusedPaneView returns null when nothing was stored', () => {
  assert.equal(paneModel.resolveInitialFocusedPaneView(maxPaneWs('p1'), null), null)
})

test('writeFocusedPaneView round-trips through readFocusedPaneView and clears on null', () => {
  const storage = focusStorage()
  paneModel.writeFocusedPaneView('p1', storage)
  assert.equal(paneModel.readFocusedPaneView(storage), 'p1', 'a maximize persists')
  paneModel.writeFocusedPaneView(null, storage)
  assert.equal(paneModel.readFocusedPaneView(storage), null, 'un-maximizing removes the key')
  assert.equal(storage.peek(), null, 'the key is removed, not left stale')
})

test('readFocusedPaneView is forgiving of a throwing storage', () => {
  const throwing = { getItem: () => { throw new Error('SecurityError') } }
  assert.equal(paneModel.readFocusedPaneView(throwing), null)
})

test('resolveInitialFocusedPaneView round-trips a real maximized 2-pane workspace', () => {
  // Build a genuine 2-pane workspace through the model, focus the second pane
  // (what toggleFocusedPaneView does before maximizing), persist + restore.
  const seeded = paneModel.setViewMode(
    paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('chat', 'b')]),
    'panes',
  )
  const split = paneModel.moveTab(seeded, 'chat:b', { paneId: 'p0', edge: 'right' })
  const bPane = paneModel.paneOf(split, 'chat:b').id
  const focused = paneModel.workspaceReducer({ ws: split }, { type: 'FOCUS', paneId: bPane }).ws
  const storage = focusStorage()
  paneModel.writeFocusedPaneView(bPane, storage)
  const restored = paneModel.resolveInitialFocusedPaneView(
    paneModel.parseWorkspace(paneModel.serializeWorkspace(focused)),
    paneModel.readFocusedPaneView(storage),
  )
  assert.equal(restored, bPane, 'a maximized pane survives a serialize→parse→resolve round-trip')
})
