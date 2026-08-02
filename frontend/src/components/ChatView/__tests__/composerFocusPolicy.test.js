import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  beginTouchComposerFocusLease,
  focusComposerElement,
  releaseComposerFocusLease,
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

test('ordinary focus requests do not pop focus into embedded or touch-primary chats', () => {
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

test('an explicit touch focus request opens the matching shell composer', () => {
  assert.equal(shouldApplyComposerFocusRequest({
    focusRequest: { chatId: '42', token: 1, focus: true },
    chatId: 42,
    embedded: false,
    isTouchPrimary: true,
  }), true)

  assert.equal(shouldApplyComposerFocusRequest({
    focusRequest: { chatId: '41', token: 1, focus: true },
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

test('touch focus lease starts synchronously and preserves early typing', () => {
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

  el.value = 'typed while opening'
  assert.equal(el.value, 'typed while opening')
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
