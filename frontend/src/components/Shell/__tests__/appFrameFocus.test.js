import assert from 'node:assert/strict'
import test from 'node:test'

import { releaseFocusFromHiddenAppFrame } from '../appFrameFocus.js'

function frameWithOwner(owner) {
  return {
    tagName: 'IFRAME',
    closest: selector => selector === '[data-app-frame-owner]' ? owner : null,
  }
}

test('a cached app frame releases focus once its workspace surface is hidden', () => {
  const calls = []
  const owner = { getAttribute: name => name === 'aria-hidden' ? 'true' : null }
  assert.equal(releaseFocusFromHiddenAppFrame({
    activeElement: frameWithOwner(owner),
    focusTarget: { focus: options => calls.push(options) },
  }), true)
  assert.deepEqual(calls, [{ preventScroll: true }])
})

test('the focused visible app and non-app focus owners stay untouched', () => {
  let focusCalls = 0
  const target = { focus: () => { focusCalls += 1 } }
  const visibleOwner = { getAttribute: () => null }

  assert.equal(releaseFocusFromHiddenAppFrame({
    activeElement: frameWithOwner(visibleOwner),
    focusTarget: target,
  }), false)
  assert.equal(releaseFocusFromHiddenAppFrame({
    activeElement: { tagName: 'BUTTON' },
    focusTarget: target,
  }), false)
  assert.equal(focusCalls, 0)
})
