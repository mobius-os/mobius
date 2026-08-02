import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  beginTouchComposerFocusLease,
  releaseComposerFocusLease,
} from '../composerFocusLease.js'

test('touch focus lease starts synchronously with a clean control', () => {
  const calls = []
  const el = {
    value: 'stale',
    focus: (...args) => calls.push(args),
  }
  const touchMedia = query => ({
    matches: query === '(hover: none) and (pointer: coarse)',
  })

  assert.equal(beginTouchComposerFocusLease(el, {
    matchMediaImpl: touchMedia,
    activeElement: null,
  }), true)
  assert.equal(el.value, '')
  assert.deepEqual(calls, [[{ preventScroll: true }]])
})

test('touch focus lease stays inert on desktop and releases failed handoffs', () => {
  let blurred = 0
  const el = {
    value: 'draft',
    focus() {},
    blur() { blurred += 1 },
  }
  assert.equal(beginTouchComposerFocusLease(el, {
    matchMediaImpl: () => ({ matches: false }),
    activeElement: null,
  }), false)
  assert.equal(el.value, 'draft')

  releaseComposerFocusLease(el, { activeElement: el })
  assert.equal(blurred, 1)
  assert.equal(el.value, '')
})
