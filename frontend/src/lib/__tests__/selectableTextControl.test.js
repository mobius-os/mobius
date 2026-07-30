import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  pointerSelectionChangedWithin,
  textSelectionSnapshot,
} from '../selectableTextControl.js'

function selection({
  anchorNode = {},
  anchorOffset = 0,
  focusNode = {},
  focusOffset = 1,
  intersects = true,
  isCollapsed = false,
  rangeCount = 1,
} = {}) {
  return {
    anchorNode,
    anchorOffset,
    focusNode,
    focusOffset,
    isCollapsed,
    rangeCount,
    getRangeAt: () => ({ intersectsNode: () => intersects }),
  }
}

test('textSelectionSnapshot ignores an absent or collapsed selection', () => {
  assert.equal(textSelectionSnapshot(null), null)
  assert.equal(textSelectionSnapshot(selection({ isCollapsed: true })), null)
  assert.equal(textSelectionSnapshot(selection({ rangeCount: 0 })), null)
})

test('pointerSelectionChangedWithin detects a new selection in its control', () => {
  const current = selection()

  assert.equal(pointerSelectionChangedWithin(null, {}, current), true)
  assert.equal(
    pointerSelectionChangedWithin(textSelectionSnapshot(current), {}, current),
    false,
    'an unchanged selection must not suppress a later ordinary click',
  )
})

test('pointerSelectionChangedWithin ignores selections outside the control', () => {
  assert.equal(
    pointerSelectionChangedWithin(null, {}, selection({ intersects: false })),
    false,
  )
  assert.equal(pointerSelectionChangedWithin(null, null, selection()), false)
})
