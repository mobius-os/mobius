import test from 'node:test'
import assert from 'node:assert/strict'

import {
  computePinnedDrag,
  observePinnedOrderHandoff,
  pinnedEntriesMatchRanks,
  pinnedOrderHandoffStatus,
  projectPinnedEntries,
} from '../../components/Drawer/pinnedReorder.js'

// Four uniform 40px rows stacked from top 0.
function uniformRows() {
  return ['a', 'b', 'c', 'd'].map((key, i) => ({
    key,
    top: i * 40,
    height: 40,
    center: i * 40 + 20,
  }))
}

test('dragging a row down lands it below the rows it passed', () => {
  // Drag row b (index 1) down 60px: its center 60 -> 120, past c's center 100.
  const { above, changed, finalKeys, slotDelta, shifts } = computePinnedDrag(uniformRows(), 1, 60)
  assert.equal(changed, true)
  assert.equal(above, 2)
  assert.deepEqual(finalKeys, ['a', 'c', 'b', 'd'])
  // b rests one slot lower; c rises into b's vacated slot; a and d hold.
  assert.equal(slotDelta, 40)
  assert.equal(shifts.get('c'), -40)
  assert.equal(shifts.get('a'), 0)
  assert.equal(shifts.get('d'), 0)
})

test('dragging a row up lands it above the rows it passed', () => {
  // Drag row d (index 3) up 80px: center 140 -> 60, above b's center 60.
  const { above, changed, finalKeys, slotDelta, shifts } = computePinnedDrag(uniformRows(), 3, -80)
  assert.equal(changed, true)
  assert.equal(above, 1)
  assert.deepEqual(finalKeys, ['a', 'd', 'b', 'c'])
  assert.equal(slotDelta, -80)
  assert.equal(shifts.get('b'), 40)
  assert.equal(shifts.get('c'), 40)
  assert.equal(shifts.get('a'), 0)
})

test('a tiny wobble that stays in the same slot is a no-op', () => {
  const { changed, above, finalKeys } = computePinnedDrag(uniformRows(), 1, 5)
  assert.equal(changed, false)
  assert.equal(above, 1)
  assert.deepEqual(finalKeys, ['a', 'b', 'c', 'd'])
})

test('previewed positions equal final natural positions with non-uniform heights', () => {
  // Chat rows 40px, an app row 56px. Dragging the app (index 0) to the bottom
  // must leave every row exactly where the committed list will render it — the
  // property that makes the drop seamless.
  const rows = [
    { key: 'app', top: 0, height: 56, center: 28 },
    { key: 'c1', top: 56, height: 40, center: 76 },
    { key: 'c2', top: 96, height: 40, center: 116 },
  ]
  const { finalKeys, slotDelta, shifts } = computePinnedDrag(rows, 0, 200)
  assert.deepEqual(finalKeys, ['c1', 'c2', 'app'])

  // Reconstruct previewed tops (natural top + applied transform) and compare to
  // the natural layout of the committed order.
  const previewTop = {
    app: rows[0].top + slotDelta,
    c1: rows[1].top + shifts.get('c1'),
    c2: rows[2].top + shifts.get('c2'),
  }
  let y = rows[0].top
  const finalTop = {}
  for (const key of finalKeys) {
    finalTop[key] = y
    y += rows.find((r) => r.key === key).height
  }
  assert.deepEqual(previewTop, finalTop)
})

test('pinned order handoff distinguishes pending, committed, and superseded lists', () => {
  const expected = ['a', 'c', 'b']
  assert.equal(pinnedOrderHandoffStatus(['a', 'b', 'c'], expected), 'pending')
  assert.equal(pinnedOrderHandoffStatus(['a', 'c', 'b'], expected), 'committed')
  assert.equal(pinnedOrderHandoffStatus(['a', 'b'], expected), 'superseded')
  assert.equal(pinnedOrderHandoffStatus(['a', 'b', 'd'], expected), 'superseded')
  assert.equal(pinnedOrderHandoffStatus(['a', 'a', 'b'], expected), 'superseded')
})

test('mixed chat/app refreshes cannot override the visible pinned order', () => {
  const entry = (kind, id, pinnedAt) => ({
    kind,
    item: { id, pinned_at: pinnedAt },
  })
  const mixedSnapshot = [
    entry('chat', 'c1', '2026-07-30T02:22:25.951'),
    entry('app', 39, '2026-07-30T02:22:25.996'),
    entry('app', 80, '2026-07-30T02:22:26.044'),
  ]
  const visibleKeys = ['app:39', 'app:80', 'chat:c1']
  assert.deepEqual(
    projectPinnedEntries(mixedSnapshot, visibleKeys)
      .map(({ kind, item }) => `${kind}:${item.id}`),
    visibleKeys,
  )
})

test('pinned handoff waits for exact atomic server ranks from both queries', () => {
  const entries = [
    { kind: 'app', item: { id: 39, pinned_at: 'server-a' } },
    { kind: 'chat', item: { id: 'c1', pinned_at: 'client-c' } },
  ]
  const expected = [
    { key: 'app:39', pinnedAt: 'server-a' },
    { key: 'chat:c1', pinnedAt: 'server-c' },
  ]
  assert.equal(pinnedEntriesMatchRanks(entries, expected), false)
  entries[1].item.pinned_at = 'server-c'
  assert.equal(pinnedEntriesMatchRanks(entries, expected), true)
})

test('pinned preview stays held until the keyed DOM order commits', () => {
  let keys = ['a', 'b', 'c']
  let observerCallback = null
  let disconnects = 0
  const root = {
    querySelectorAll() {
      return keys.map(pinnedKey => ({ dataset: { pinnedKey } }))
    },
  }
  class ObserverStub {
    constructor(callback) { observerCallback = callback }
    observe() {}
    disconnect() { disconnects += 1 }
  }
  const settled = []
  const cancel = observePinnedOrderHandoff(
    root,
    ['a', 'c', 'b'],
    status => settled.push(status),
    ObserverStub,
  )

  assert.deepEqual(settled, [], 'the old DOM order keeps preview transforms held')
  observerCallback()
  assert.deepEqual(settled, [], 'unrelated mutation before reorder remains pending')
  keys = ['a', 'c', 'b']
  observerCallback()
  assert.deepEqual(settled, ['committed'])
  assert.equal(disconnects, 1)
  cancel()
  assert.equal(disconnects, 1, 'cancellation stays idempotent after settlement')
})

test('a concurrent pin-set change safely supersedes the preview handoff', () => {
  const root = {
    querySelectorAll() {
      return ['a', 'b', 'new'].map(pinnedKey => ({ dataset: { pinnedKey } }))
    },
  }
  const settled = []
  observePinnedOrderHandoff(
    root,
    ['a', 'c', 'b'],
    status => settled.push(status),
    class ObserverStub { observe() {} disconnect() {} },
  )
  assert.deepEqual(settled, ['superseded'])
})
