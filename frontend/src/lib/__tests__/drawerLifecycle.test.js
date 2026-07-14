import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  DRAWER_CLOSE_FALLBACK_MS,
  clearDrawerGestureStyles,
} from '../drawerLifecycle.js'

test('closed drawer cleanup removes an interrupted swipe transform', () => {
  const removed = []
  const element = {
    classList: { remove: (...names) => removed.push(...names) },
    style: { transform: 'translateX(-25px)' },
  }

  clearDrawerGestureStyles(element)

  assert.deepEqual(removed, ['drawer--dragging'])
  assert.equal(element.style.transform, '')
})

test('drawer cleanup is safe before the panel ref mounts', () => {
  assert.doesNotThrow(() => clearDrawerGestureStyles(null))
})

test('close fallback outlasts the 250ms panel transition', () => {
  assert.ok(DRAWER_CLOSE_FALLBACK_MS > 250)
})
