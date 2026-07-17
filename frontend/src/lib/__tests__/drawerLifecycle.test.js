import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  DRAWER_CLOSE_FALLBACK_MS,
  isGeneratedTouchClick,
  isHorizontalDrawerSwipe,
  shouldSuppressDrawerSwipeClick,
  clearDrawerGestureStyles,
  drawerOpenBlockedByDrag,
} from '../drawerLifecycle.js'

test('the drawer open path stands down only while a drag is live', () => {
  // A live drag blocks the open (a left-edge tab drag must split, not open the
  // drawer); every non-live state — including an unset/absent ref — allows it.
  assert.equal(drawerOpenBlockedByDrag(true), true)
  assert.equal(drawerOpenBlockedByDrag(false), false)
  assert.equal(drawerOpenBlockedByDrag(undefined), false)
  assert.equal(drawerOpenBlockedByDrag(null), false)
})

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

test('drawer swipe classification rejects vertical and ambiguous movement', () => {
  assert.equal(isHorizontalDrawerSwipe(-25, 4), true)
  assert.equal(isHorizontalDrawerSwipe(-15, -120), false)
  assert.equal(isHorizontalDrawerSwipe(-8, 0), false)
})

test('cancelled drawer gestures never own a future click', () => {
  assert.equal(shouldSuppressDrawerSwipeClick({
    sawHorizontalMove: true,
    cancelled: true,
    dx: -30,
    dy: 0,
  }), false)
})

test('diagonal noise does not turn a completed vertical scroll into a swipe', () => {
  assert.equal(shouldSuppressDrawerSwipeClick({
    sawHorizontalMove: true,
    dx: -15,
    dy: -130,
  }), false)
})

test('only a normally completed horizontal swipe owns its generated click', () => {
  assert.equal(shouldSuppressDrawerSwipeClick({
    sawHorizontalMove: true,
    dx: -30,
    dy: 2,
  }), true)
  assert.equal(shouldSuppressDrawerSwipeClick({
    sawHorizontalMove: false,
    dx: -30,
    dy: 2,
  }), false)
})

test('the click guard fails open for keyboard and assistive activation', () => {
  assert.equal(isGeneratedTouchClick({ detail: 0 }), false)
  assert.equal(isGeneratedTouchClick({
    detail: 1,
    sourceCapabilities: { firesTouchEvents: false },
  }), false)
  assert.equal(isGeneratedTouchClick({
    detail: 0,
    sourceCapabilities: { firesTouchEvents: true },
  }), true)
  assert.equal(isGeneratedTouchClick({ detail: 1 }), true)
})
