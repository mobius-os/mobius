import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  requestSearchReveal,
  peekSearchReveal,
  takeSearchReveal,
  subscribeSearchReveal,
} from '../searchReveal.js'
import { applyMode } from '../useScrollMode.js'

test('request → peek is non-destructive, take consumes once', () => {
  requestSearchReveal('chat-A', 'user-1700000000000', ['budget'])
  assert.deepEqual(peekSearchReveal('chat-A'), { key: 'user-1700000000000', terms: ['budget'] })
  // Peek again: still there.
  assert.deepEqual(peekSearchReveal('chat-A'), { key: 'user-1700000000000', terms: ['budget'] })
  assert.deepEqual(takeSearchReveal('chat-A'), { key: 'user-1700000000000', terms: ['budget'] })
  // Consumed.
  assert.equal(peekSearchReveal('chat-A'), null)
  assert.equal(takeSearchReveal('chat-A'), null)
})

test('intents are isolated per chat id, terms default to empty', () => {
  requestSearchReveal('chat-B', 'assistant-22')
  requestSearchReveal('chat-C', 'user-33')
  assert.deepEqual(takeSearchReveal('chat-C'), { key: 'user-33', terms: [] })
  // B untouched by C's consume.
  assert.deepEqual(takeSearchReveal('chat-B'), { key: 'assistant-22', terms: [] })
})

test('a null/empty key is ignored (title-only hit)', () => {
  requestSearchReveal('chat-D', null)
  requestSearchReveal('chat-D', '')
  assert.equal(peekSearchReveal('chat-D'), null)
})

test('subscribers fire with the target id for the already-open case', () => {
  const seen = []
  const unsub = subscribeSearchReveal((id) => seen.push(id))
  requestSearchReveal('chat-E', 'user-5')
  requestSearchReveal('chat-F', 'user-6')
  unsub()
  requestSearchReveal('chat-G', 'user-7') // after unsub: not observed
  assert.deepEqual(seen, ['chat-E', 'chat-F'])
  takeSearchReveal('chat-E'); takeSearchReveal('chat-F'); takeSearchReveal('chat-G')
})

test('a listener that throws does not drop the intent', () => {
  const unsub = subscribeSearchReveal(() => { throw new Error('boom') })
  requestSearchReveal('chat-H', 'user-8')
  assert.deepEqual(peekSearchReveal('chat-H'), { key: 'user-8', terms: [] })
  unsub()
  takeSearchReveal('chat-H')
})

test('reveal ANCHOR_AT lands the matched row below the viewport top', () => {
  // The mechanism revealAnchor drives: applyMode positions the data-key row so
  // it sits `offset` px below the top (a little context above the match).
  const row = { offsetTop: 1200 }
  const scrollEl = {
    scrollTop: 0,
    scrollHeight: 5000,
    clientHeight: 800,
    querySelector(sel) {
      return sel.includes('data-key') ? row : null
    },
  }
  applyMode(scrollEl, { kind: 'ANCHOR_AT', key: 'user-42', offset: 96 })
  assert.equal(scrollEl.scrollTop, 1200 - 96)
})
