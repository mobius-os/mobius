import assert from 'node:assert/strict'
import { test } from 'node:test'

import { readSafeAreaInsets, zeroInsets } from '../safeAreaInsets.js'

test('reads the four padding sides off the computed style', () => {
  const insets = readSafeAreaInsets({
    paddingTop: '47px',
    paddingRight: '0px',
    paddingBottom: '34px',
    paddingLeft: '0px',
  })
  assert.deepEqual(insets, {
    top: '47px', right: '0px', bottom: '34px', left: '0px',
  })
})

test('normalizes fractional px (real devices report sub-pixel insets)', () => {
  const insets = readSafeAreaInsets({
    paddingTop: '47.5px',
    paddingRight: '0px',
    paddingBottom: '20.25px',
    paddingLeft: '0px',
  })
  assert.equal(insets.top, '47.5px')
  assert.equal(insets.bottom, '20.25px')
})

test('a missing / undefined computed style yields all zeros, never null', () => {
  assert.deepEqual(readSafeAreaInsets(undefined), zeroInsets())
  assert.deepEqual(readSafeAreaInsets({}), zeroInsets())
})

test('non-px / auto / negative values clamp to 0px', () => {
  const insets = readSafeAreaInsets({
    // getComputedStyle always resolves env() to a concrete px length, but
    // guard the malformed cases anyway so a probe quirk can't push content
    // the wrong way. 'auto' / '' / negative / unparseable all -> 0px.
    paddingTop: 'auto',
    paddingRight: '',
    paddingBottom: '-5px',
    paddingLeft: 'calc(1px)',
  })
  assert.deepEqual(insets, zeroInsets())
})

test('zeroInsets is a fresh all-zero object', () => {
  assert.deepEqual(zeroInsets(), {
    top: '0px', right: '0px', bottom: '0px', left: '0px',
  })
  // Returns a new object each call so callers can mutate / forward safely.
  assert.notEqual(zeroInsets(), zeroInsets())
})
