import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  focusComposerElement,
  shouldApplyComposerFocusRequest,
} from '../composerFocusPolicy.js'

test('focus request applies to the matching desktop shell chat', () => {
  assert.equal(shouldApplyComposerFocusRequest({
    focusRequest: { chatId: '42', token: 1 },
    chatId: 42,
    embedded: false,
    isTouchPrimary: false,
  }), true)
})

test('focus request ignores unrelated chats and missing requests', () => {
  assert.equal(shouldApplyComposerFocusRequest({
    focusRequest: null,
    chatId: 42,
  }), false)
  assert.equal(shouldApplyComposerFocusRequest({
    focusRequest: { chatId: '41', token: 1 },
    chatId: 42,
  }), false)
})

test('focus request does not pop focus into embedded or touch-primary chats', () => {
  assert.equal(shouldApplyComposerFocusRequest({
    focusRequest: { chatId: 42, token: 1 },
    chatId: 42,
    embedded: true,
    isTouchPrimary: false,
  }), false)
  assert.equal(shouldApplyComposerFocusRequest({
    focusRequest: { chatId: 42, token: 1 },
    chatId: 42,
    embedded: false,
    isTouchPrimary: true,
  }), false)
})

test('focusComposerElement preserves scroll when the browser supports it', () => {
  const calls = []
  const el = { focus: (...args) => calls.push(args) }
  assert.equal(focusComposerElement(el), true)
  assert.deepEqual(calls, [[{ preventScroll: true }]])
})

test('focusComposerElement falls back for older focus implementations', () => {
  const calls = []
  const el = {
    focus: (...args) => {
      calls.push(args)
      if (args.length) throw new Error('no options')
    },
  }
  assert.equal(focusComposerElement(el), true)
  assert.deepEqual(calls, [[{ preventScroll: true }], []])
})
