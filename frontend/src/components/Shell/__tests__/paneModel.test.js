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

// Assert every workspace-wide invariant, so both the op tests and the property
// suite can lean on one checker.
function assertInvariants(ws) {
  assert.equal(ws.v, 1)
  const ids = paneIdsOf(ws.layout)
  assert.ok(ids.length >= 1, 'at least one leaf')
  assert.ok(ids.length <= paneModel.MAX_PANES, 'leaf count within MAX_PANES')
  assert.ok(splitDepth(ws.layout) <= paneModel.MAX_DEPTH, 'depth within MAX_DEPTH')

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

test('openTab evicts the oldest background tab at the cap and protects the active one', () => {
  let ws = paneModel.seedFromFlatTabs([])
  for (let i = 0; i < paneModel.MAX_PANE_TABS; i += 1) {
    ws = paneModel.openTab(ws, makeTab('chat', `c${i}`), { activate: false })
  }
  // Make the OLDEST tab (c0) the active one to prove it is protected.
  ws = paneModel.setActiveTab(ws, 'p0', 'chat:c0')
  const before = paneModel.flatten(ws).map(tabKey)
  assert.equal(before.length, paneModel.MAX_PANE_TABS)

  ws = paneModel.openTab(ws, makeTab('chat', 'new'))
  const after = paneModel.flatten(ws).map(tabKey)
  assert.equal(after.length, paneModel.MAX_PANE_TABS, 'stays at the cap')
  assert.ok(after.includes('chat:c0'), 'the active tab is never evicted')
  assert.ok(after.includes('chat:new'), 'the newcomer is present')
  assert.ok(!after.includes('chat:c1'), 'the oldest non-active tab was evicted')
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

test('reducer APPLY_FLAT preserves the active tab and is not undoable', () => {
  const seeded = paneModel.seedFromFlatTabs([makeTab('chat', 'a'), makeTab('chat', 'b'), makeTab('app', 7)])
  const start = { ws: paneModel.setActiveTab(seeded, 'p0', 'chat:a'), undo: { ws: seeded, label: 'prior' } }
  assert.equal(start.ws.panes.p0.activeTabKey, 'chat:a')

  const applied = paneModel.workspaceReducer(start, {
    type: 'APPLY_FLAT',
    tabs: [makeTab('chat', 'a'), makeTab('app', 7), makeTab('app', 9)],
  })
  assert.deepEqual(
    paneModel.flatten(applied.ws),
    [makeTab('chat', 'a'), makeTab('app', 7), makeTab('app', 9)],
  )
  assert.equal(applied.ws.panes.p0.activeTabKey, 'chat:a', 'the surviving active tab is kept')
  assert.equal(applied.undo, start.undo, 'APPLY_FLAT carries the existing slot forward untouched')
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
