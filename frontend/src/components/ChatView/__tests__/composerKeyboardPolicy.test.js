import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  shouldDismissComposerKeyboardOnSubmit,
} from '../composerKeyboardPolicy.js'

test('mobile queue-only send keeps the composer keyboard open', () => {
  assert.equal(shouldDismissComposerKeyboardOnSubmit({
    isTouchPrimary: true,
    queuesBehindActiveTurn: true,
  }), false)
})

test('mobile fresh send and explicit steer dismiss the composer keyboard', () => {
  assert.equal(shouldDismissComposerKeyboardOnSubmit({
    isTouchPrimary: true,
    queuesBehindActiveTurn: false,
  }), true)
  assert.equal(shouldDismissComposerKeyboardOnSubmit({
    isTouchPrimary: true,
    queuesBehindActiveTurn: true,
    steerAfterQueue: true,
  }), true)
})

test('desktop submits retain composer focus', () => {
  assert.equal(shouldDismissComposerKeyboardOnSubmit({
    isTouchPrimary: false,
    queuesBehindActiveTurn: false,
  }), false)
  assert.equal(shouldDismissComposerKeyboardOnSubmit({
    isTouchPrimary: false,
    queuesBehindActiveTurn: true,
    steerAfterQueue: true,
  }), false)
})
