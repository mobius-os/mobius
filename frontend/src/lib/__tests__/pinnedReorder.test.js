import test from 'node:test'
import assert from 'node:assert/strict'

import { computePinnedDrag } from '../../components/Drawer/pinnedReorder.js'

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
