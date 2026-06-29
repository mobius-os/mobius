/*
 * Tests for pure stream-to-message promotion helpers.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  streamItemsToAssistantPayload,
  promoteAssistantStream,
  assistantStreamCoversMessage,
  messageCoversAssistantStream,
  assistantMessageText,
  findTrailingAssistantPartialIndex,
} from '../streamPromotion.js'

test('streamItemsToAssistantPayload preserves text boundaries in legacy content', () => {
  const payload = streamItemsToAssistantPayload([
    { type: 'text', content: 'first' },
    { type: 'tool', tool: 'Bash', status: 'running', input: 'ls' },
    { type: 'text', content: 'second' },
  ])

  assert.equal(payload.content, 'first\n\nsecond')
  assert.deepEqual(payload.blocks.map(b => b.type), ['text', 'tool', 'text'])
  assert.equal(payload.blocks[1].status, 'done')
})

test('promoteAssistantStream appends when there is no bridge partial', () => {
  const messages = [{ role: 'user', ts: 1, content: 'hi' }]
  const next = promoteAssistantStream(messages, {
    items: [{ type: 'text', content: 'hello' }],
  })

  assert.equal(next.length, 2)
  assert.equal(next[1].role, 'assistant')
  assert.equal(next[1].content, 'hello')
})

test('promoteAssistantStream replaces mounted partial even after steered user row', () => {
  const messages = [
    { role: 'user', ts: 1, content: 'q1' },
    { role: 'assistant', ts: 2, content: 'partial', blocks: [{ type: 'text', content: 'partial' }] },
    { role: 'user', ts: 3, content: 'fast-forwarded q2' },
  ]

  const next = promoteAssistantStream(messages, {
    bridgeTs: 2,
    items: [{ type: 'text', content: 'updated live stream' }],
  })

  assert.equal(next.length, 3)
  assert.equal(next[1].role, 'assistant')
  assert.equal(next[1].ts, 2)
  assert.equal(next[1].content, 'updated live stream')
  assert.equal(next[2].content, 'fast-forwarded q2')
})

test('promoteAssistantStream carries persisted question answers by identity', () => {
  const messages = [{
    role: 'assistant',
    ts: 9,
    content: '',
    blocks: [{
      type: 'question',
      question_id: 'q-1',
      questions: [{ id: 'choice', question: 'Pick?' }],
      answers: { Pick: 'A' },
    }],
  }]

  const next = promoteAssistantStream(messages, {
    bridgeTs: 9,
    items: [{
      type: 'question',
      question_id: 'q-1',
      questions: [{ id: 'choice', question: 'Pick?' }],
    }],
  })

  assert.deepEqual(next[0].blocks[0].answers, { Pick: 'A' })
})


test('assistantMessageText reads persisted text blocks', () => {
  assert.equal(assistantMessageText({
    role: 'assistant',
    content: 'legacy',
    blocks: [
      { type: 'text', content: 'first' },
      { type: 'tool', tool: 'Bash' },
      { type: 'text', content: 'second' },
    ],
  }), 'first\n\nsecond')
})

test('assistantStreamCoversMessage treats a tiny persisted partial as covered by a richer live stream', () => {
  assert.equal(assistantStreamCoversMessage(
    { role: 'assistant', ts: 2, content: 'I', blocks: [{ type: 'text', content: 'I' }] },
    [{ type: 'text', content: 'I’ll continue from here' }],
  ), true)
})

test('assistantStreamCoversMessage rejects unrelated prior assistant text', () => {
  assert.equal(assistantStreamCoversMessage(
    { role: 'assistant', ts: 2, content: 'Previous answer', blocks: [{ type: 'text', content: 'Previous answer' }] },
    [{ type: 'text', content: 'New active answer' }],
  ), false)
})

test('assistantStreamCoversMessage matches tool-only persisted partials conservatively', () => {
  assert.equal(assistantStreamCoversMessage(
    { role: 'assistant', ts: 2, blocks: [{ type: 'tool', tool: 'Bash', status: 'done' }] },
    [{ type: 'tool', tool: 'Bash', status: 'running' }],
  ), true)
  assert.equal(assistantStreamCoversMessage(
    { role: 'assistant', ts: 2, blocks: [{ type: 'tool', tool: 'Bash', status: 'running', input: 'ls' }] },
    [{ type: 'tool', tool: 'Bash', status: 'running', input: 'pwd' }],
  ), false)
})


test('messageCoversAssistantStream prefers a richer DB partial over a stale one-letter stream', () => {
  const msg = {
    role: 'assistant',
    ts: 2,
    content: 'I can help with that',
    blocks: [{ type: 'text', content: 'I can help with that' }],
  }
  const items = [{ type: 'text', content: 'I' }]
  assert.equal(assistantStreamCoversMessage(msg, items), false)
  assert.equal(messageCoversAssistantStream(msg, items), true)
})

test('text prefix does not hide a persisted tool block when stream lacks it', () => {
  const msg = {
    role: 'assistant',
    ts: 2,
    blocks: [
      { type: 'text', content: 'Checking' },
      { type: 'tool', tool: 'Bash', status: 'done', input: 'ls' },
    ],
  }
  assert.equal(assistantStreamCoversMessage(msg, [{ type: 'text', content: 'Checking' }]), false)
})

test('findTrailingAssistantPartialIndex returns only the latest visible assistant row', () => {
  assert.equal(findTrailingAssistantPartialIndex([
    { role: 'user', ts: 1 },
    { role: 'assistant', ts: 2 },
  ]), 1)
  assert.equal(findTrailingAssistantPartialIndex([
    { role: 'assistant', ts: 2 },
    { role: 'user', ts: 3 },
  ]), -1)
  assert.equal(findTrailingAssistantPartialIndex([
    { role: 'user', ts: 1 },
    { role: 'assistant', ts: 2 },
    { role: 'user', hidden: true, ts: 3 },
  ]), 1)
})
