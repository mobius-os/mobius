import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  beginTouchComposerFocusLease,
  composerFocusLeaseHandoff,
  releaseComposerFocusLease,
} from '../composerFocusLease.js'

test('touch focus lease starts synchronously at the end of its draft snapshot', () => {
  const calls = []
  const selections = []
  const el = {
    value: 'stale',
    focus: (...args) => calls.push(args),
    setSelectionRange: (...args) => selections.push(args),
  }
  const touchMedia = query => ({
    matches: query === '(hover: none) and (pointer: coarse)',
  })

  assert.equal(beginTouchComposerFocusLease(el, {
    matchMediaImpl: touchMedia,
    activeElement: null,
    initialValue: 'unfinished',
  }), true)
  assert.equal(el.value, 'unfinished')
  assert.deepEqual(calls, [[{ preventScroll: true }]])
  assert.deepEqual(selections, [[10, 10]])
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

test('early typing extends a resumed draft without dropping its attachments', () => {
  const attachment = {
    name: 'reference.png', size: 12, mime_type: 'image/png', status: 'done',
  }
  assert.deepEqual(composerFocusLeaseHandoff({
    initialValue: 'unfinished',
    leaseCandidate: {
      chatId: 'draft-chat',
      source: 'draft',
      draft: { input: 'unfinished', attachments: [attachment] },
    },
    leaseValue: 'unfinished thought',
    leased: true,
    resolvedChatId: 'draft-chat',
  }), {
    attachments: [attachment],
    autoSend: false,
    shouldStage: true,
    text: 'unfinished thought',
  })
})

test('an untouched resumed lease leaves the durable draft owner unchanged', () => {
  assert.deepEqual(composerFocusLeaseHandoff({
    initialValue: 'unfinished',
    leaseCandidate: {
      chatId: 'draft-chat',
      source: 'draft',
      draft: { input: 'unfinished', attachments: [] },
    },
    leaseValue: 'unfinished',
    leased: true,
    resolvedChatId: 'draft-chat',
  }), {
    attachments: [],
    autoSend: false,
    shouldStage: false,
    text: 'unfinished',
  })
})

test('clearing resumed text keeps its completed attachment snapshot', () => {
  const attachment = { name: 'reference.png', status: 'done' }
  assert.deepEqual(composerFocusLeaseHandoff({
    initialValue: 'remove this text',
    leaseCandidate: {
      chatId: 'draft-chat',
      source: 'draft',
      draft: { input: 'remove this text', attachments: [attachment] },
    },
    leaseValue: '',
    leased: true,
    resolvedChatId: 'draft-chat',
  }), {
    attachments: [attachment],
    autoSend: false,
    shouldStage: true,
    text: '',
  })
})
