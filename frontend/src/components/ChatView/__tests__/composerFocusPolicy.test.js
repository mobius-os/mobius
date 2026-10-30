import { test } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ChatInputBar from '../ChatInputBar.jsx'

import {
  focusComposerElement,
  placeCaretAtTextEnd,
  shouldApplyComposerFocusRequest,
} from '../composerFocusPolicy.js'

test('explicit focus request applies to the matching shell chat', () => {
  assert.equal(shouldApplyComposerFocusRequest({
    focusRequest: { chatId: '42', token: 1, focus: true },
    chatId: 42,
    embedded: false,
  }), true)
})

test('focus request ignores unrelated chats and missing requests', () => {
  assert.equal(shouldApplyComposerFocusRequest({
    focusRequest: null,
    chatId: 42,
  }), false)
  assert.equal(shouldApplyComposerFocusRequest({
    focusRequest: { chatId: '41', token: 1, focus: true },
    chatId: 42,
  }), false)
})

test('draft-only and embedded requests do not focus the composer', () => {
  assert.equal(shouldApplyComposerFocusRequest({
    focusRequest: { chatId: 42, token: 1 },
    chatId: 42,
  }), false)
  assert.equal(shouldApplyComposerFocusRequest({
    focusRequest: { chatId: 42, token: 1, focus: true },
    chatId: 42,
    embedded: true,
  }), false)
})

test('app-driven composer focus preserves scroll and places the caret at the end', () => {
  const calls = []
  const selections = []
  const el = {
    value: 'saved draft',
    focus: (...args) => calls.push(args),
    setSelectionRange: (...args) => selections.push(args),
  }
  assert.equal(focusComposerElement(el), true)
  assert.deepEqual(calls, [[{ preventScroll: true }]])
  assert.deepEqual(selections, [[11, 11]])
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

test('explicit caret placement still lands at the end for restored text controls', () => {
  const selections = []
  const el = {
    value: 'custom answer',
    setSelectionRange: (...args) => selections.push(args),
  }

  assert.equal(placeCaretAtTextEnd(el), true)
  assert.deepEqual(selections, [[13, 13]])
  assert.equal(placeCaretAtTextEnd({ value: 'unsupported' }), false)
})

test('the composer opts out of browser spelling popups without disabling keyboard autocorrection', () => {
  const client = new QueryClient()
  try {
    const html = renderToStaticMarkup(React.createElement(
      QueryClientProvider, { client },
      React.createElement(ChatInputBar, {
        chatId: 'correction-test', input: 'Review lesssons carefully',
        inputRef: { current: null }, onInputChange() {},
      }),
    ))
    const textarea = html.match(/<textarea\b[^>]*>/)?.[0]
    assert.ok(textarea)
    assert.match(textarea, /spellCheck="false"/i)
    assert.doesNotMatch(textarea, /autoCorrect="off"/i)
  } finally {
    client.clear()
  }
})
