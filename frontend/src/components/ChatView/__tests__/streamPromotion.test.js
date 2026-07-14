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
  chooseActiveAssistantSurface,
  assistantMessageText,
  findTrailingAssistantPartialIndex,
  streamItemsHaveRenderableContent,
  streamItemToBlock,
  assistantBlockKey,
  chooseActiveAssistantMirrorIndex,
  chooseActiveAssistantDataKey,
} from '../streamPromotion.js'

// Lever 2a: the live tool item carries tool_use_id, and streamItemToBlock
// spreads ...item, so the identity flows onto the promoted DB-shaped block for
// free — MsgContent then keys the persisted tool block by the same id the live
// stream used, so a promote (or reopen) reuses the ToolBlock by identity.
test('streamItemToBlock carries tool_use_id onto the promoted tool block', () => {
  const block = streamItemToBlock({
    type: 'tool', tool: 'Bash', input: 'ls', output: 'a', status: 'done',
    tool_use_id: 'toolu_42',
  })
  assert.equal(block.type, 'tool')
  assert.equal(block.tool_use_id, 'toolu_42',
    'tool_use_id must flow through the promotion so live and persisted keys agree')
})

test('streamItemToBlock leaves tool_use_id absent for a legacy tokenless item', () => {
  const block = streamItemToBlock({
    type: 'tool', tool: 'Bash', input: 'ls', output: 'a', status: 'running',
  })
  assert.equal(block.tool_use_id, undefined, 'no id on the item → none on the block; ordinal key applies')
  assert.equal(block.status, 'done', 'running promotes to done as before')
})

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

test('source switch preserves the active answer block key namespace', () => {
  const dbBlocks = [
    { type: 'text', content: 'Inspecting' },
    { type: 'tool', tool: 'Bash', status: 'running', tool_use_id: 'toolu_same' },
    { type: 'text', content: 'Done' },
    { type: 'tool', tool: 'Read', status: 'running' },
  ]
  const liveBlocks = streamItemsToAssistantPayload(dbBlocks, { finalize: false }).blocks

  assert.deepEqual(
    liveBlocks.map(assistantBlockKey),
    dbBlocks.map(assistantBlockKey),
    'DB partial → live SSE must retain tool ids and ordinal text/tool fallbacks so React reuses every block slot',
  )
  assert.deepEqual(
    liveBlocks.map(assistantBlockKey),
    [0, 'toolu_same', 2, 't-3'],
    'tools use tool_use_id ?? ordinal while text stays ordinal',
  )
})

test('live payload conversion preserves running tools and thinking clock anchors', () => {
  const payload = streamItemsToAssistantPayload([
    { type: 'tool', tool: 'Bash', status: 'running', tool_use_id: 'toolu_live' },
    {
      type: 'thinking', content: 'checking', duration_ms: 1200,
      startedAt: 100, lastAt: 200,
    },
  ], { finalize: false })

  assert.equal(payload.blocks[0].status, 'running', 'the unified live renderer keeps its spinner')
  assert.equal(payload.blocks[1].startedAt, 100)
  assert.equal(payload.blocks[1].lastAt, 200, 'the unified live renderer keeps its timer anchor')
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

test('same-turn answer settlement keeps the question and all pre/post-answer output in one row', () => {
  const messages = [{
    role: 'assistant',
    ts: 9,
    content: 'Before the question',
    blocks: [
      { type: 'text', content: 'Before the question' },
      { type: 'tool', tool: 'Bash', status: 'done', output: 'before-output' },
      {
        type: 'question',
        question_id: 'q-1',
        questions: [{ id: 'choice', question: 'Continue?' }],
        answers: { 'Continue?': 'Yes' },
      },
    ],
  }]
  const items = [
    { type: 'text', content: 'Before the question' },
    { type: 'tool', tool: 'Bash', status: 'done', output: 'before-output' },
    {
      type: 'question',
      question_id: 'q-1',
      questions: [{ id: 'choice', question: 'Continue?' }],
    },
    { type: 'tool', tool: 'Read', status: 'done', output: 'after-output' },
    { type: 'text', content: 'After the answer' },
  ]

  const next = promoteAssistantStream(messages, { items, bridgeTs: 9 })

  assert.equal(next.length, 1, 'same-turn settlement replaces the active row')
  assert.deepEqual(next[0].blocks.map(block => block.type), [
    'text', 'tool', 'question', 'tool', 'text',
  ])
  assert.deepEqual(next[0].blocks[2].answers, { 'Continue?': 'Yes' })
  assert.equal(next[0].blocks[1].output, 'before-output')
  assert.equal(next[0].blocks[3].output, 'after-output')
  assert.equal(next[0].blocks[4].content, 'After the answer')
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

test('chooseActiveAssistantSurface hides DB partial when live replay is same turn but block metadata drifted', () => {
  const msg = {
    role: 'assistant',
    ts: 2,
    content: 'I’ll inspect the component and patch the active flow.',
    blocks: [
      { type: 'text', content: 'I’ll inspect the component and patch the active flow.' },
      { type: 'tool', tool: 'Bash', status: 'done', input: 'sed -n 1,200p app.jsx' },
      { type: 'thinking', content: 'I' },
    ],
  }
  const items = [
    { type: 'text', content: 'I’ll inspect the component and patch the active flow.' },
    // Same live turn, but catch-up/tool summaries can temporarily differ.
    { type: 'tool', tool: 'Bash', status: 'running', input: 'grep -n lookup app.jsx' },
    { type: 'thinking', content: 'I’m checking the active renderer path now.' },
  ]

  assert.equal(assistantStreamCoversMessage(msg, items), false)
  assert.equal(messageCoversAssistantStream(msg, items), false)
  assert.deepEqual(chooseActiveAssistantSurface(msg, items), {
    hideMessage: true,
    suppressStream: false,
  })
})

test('chooseActiveAssistantSurface suppresses under-caught-up stream when DB partial is richer', () => {
  const msg = {
    role: 'assistant',
    ts: 2,
    content: 'I’ll inspect the component and patch the active flow.',
    blocks: [
      { type: 'text', content: 'I’ll inspect the component and patch the active flow.' },
      { type: 'tool', tool: 'Bash', status: 'running', input: 'python3 very-long-inspection.py --all' },
      { type: 'thinking', content: 'I’m already well into the inspection.' },
    ],
  }
  const items = [
    { type: 'text', content: 'I’ll inspect the component and patch the active flow.' },
    { type: 'tool', tool: 'Bash', status: 'running', input: '' },
  ]

  assert.equal(assistantStreamCoversMessage(msg, items), false)
  assert.equal(messageCoversAssistantStream(msg, items), false)
  assert.deepEqual(chooseActiveAssistantSurface(msg, items), {
    hideMessage: false,
    suppressStream: true,
  })
})

test('chooseActiveAssistantSurface does not collapse unrelated assistant and stream surfaces', () => {
  const msg = {
    role: 'assistant',
    ts: 2,
    content: 'Previous answer',
    blocks: [{ type: 'text', content: 'Previous answer' }],
  }
  const items = [{ type: 'text', content: 'New active answer' }]

  assert.deepEqual(chooseActiveAssistantSurface(msg, items), {
    hideMessage: false,
    suppressStream: false,
  })
})

test('active trailing DB row stays mirrored across an empty to related-live source switch', () => {
  const emptyStreamIdx = chooseActiveAssistantMirrorIndex({
    bridgeMsgIdx: -1,
    trailingAssistantPartialIdx: 4,
    hasLivePayload: false,
    surface: { hideMessage: false, suppressStream: false },
  })
  const liveStreamIdx = chooseActiveAssistantMirrorIndex({
    bridgeMsgIdx: -1,
    trailingAssistantPartialIdx: 4,
    hasLivePayload: true,
    surface: { hideMessage: true, suppressStream: false },
  })

  assert.equal(emptyStreamIdx, 4)
  assert.equal(liveStreamIdx, 4,
    'the same row index keeps the active shell mounted when reconnect/question catch-up resumes')
  assert.equal(chooseActiveAssistantMirrorIndex({
    bridgeMsgIdx: -1,
    trailingAssistantPartialIdx: 4,
    hasLivePayload: true,
    surface: { hideMessage: false, suppressStream: false },
  }), -1, 'an unrelated prior assistant remains ordinary history')
})

test('stale mount bridge cannot hide a completed prior reply when a new turn streams', () => {
  const previousReply = {
    role: 'assistant',
    ts: 2,
    blocks: [
      { type: 'question', question_id: 'q-1', answers: { Continue: 'Yes' } },
      { type: 'text', content: 'The completed continuation after the answer' },
    ],
  }
  const currentPartial = {
    role: 'assistant',
    ts: 4,
    blocks: [{ type: 'text', content: 'Working on the new request' }],
  }
  const liveItems = [{ type: 'text', content: 'Working on the new request now' }]

  const bridgeSurface = chooseActiveAssistantSurface(previousReply, liveItems)
  const trailingSurface = chooseActiveAssistantSurface(currentPartial, liveItems)
  assert.deepEqual(bridgeSurface, { hideMessage: false, suppressStream: false },
    'the old answer is unrelated to the current stream')
  assert.deepEqual(trailingSurface, { hideMessage: true, suppressStream: false },
    'the current DB partial is covered by the current stream')
  assert.equal(chooseActiveAssistantMirrorIndex({
    bridgeMsgIdx: 1,
    trailingAssistantPartialIdx: 3,
    hasLivePayload: true,
    bridgeSurface,
    surface: trailingSurface,
  }), 3, 'only the current partial may be suppressed; the completed prior reply stays visible')
})

test('unrelated stale mount bridge remains history when the new turn has no DB partial', () => {
  assert.equal(chooseActiveAssistantMirrorIndex({
    bridgeMsgIdx: 1,
    trailingAssistantPartialIdx: -1,
    hasLivePayload: true,
    bridgeSurface: { hideMessage: false, suppressStream: false },
    surface: { hideMessage: false, suppressStream: false },
  }), -1)
})

test('active row data-key is latched for both live-first and DB-first source switches', () => {
  const synthetic = chooseActiveAssistantDataKey({
    latched: null,
    mirroredMsg: null,
    mirrorIndex: -1,
    hasLivePayload: true,
    chatId: 'chat-1',
  })
  assert.equal(synthetic, 'streaming-chat-1')
  assert.equal(chooseActiveAssistantDataKey({
    latched: { key: synthetic, mirrorKey: null },
    mirroredMsg: { role: 'assistant', ts: 9 },
    mirrorIndex: 2,
    hasLivePayload: true,
    chatId: 'chat-1',
  }), synthetic, 'live-first DB adoption must keep the mounted synthetic anchor')

  const dbFirst = chooseActiveAssistantDataKey({
    latched: null,
    mirroredMsg: { role: 'assistant', ts: 9 },
    mirrorIndex: 2,
    hasLivePayload: false,
    chatId: 'chat-1',
  })
  assert.equal(dbFirst, 'assistant-9')
  assert.equal(chooseActiveAssistantDataKey({
    latched: { key: dbFirst, mirrorKey: dbFirst },
    mirroredMsg: { role: 'assistant', ts: 9 },
    mirrorIndex: 2,
    hasLivePayload: true,
    chatId: 'chat-1',
  }), dbFirst, 'DB-first bridge must keep its durable anchor when live data wins')

  const released = chooseActiveAssistantDataKey({
    latched: { key: dbFirst, mirrorKey: dbFirst },
    mirroredMsg: null,
    mirrorIndex: -1,
    hasLivePayload: true,
    chatId: 'chat-1',
  })
  assert.equal(released, synthetic)
  assert.notEqual(released, dbFirst,
    'an unrelated live answer must not duplicate the restored history row data-key')
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

test('streamItemsHaveRenderableContent: empty/whitespace-only pre-steer partial is NOT renderable (card 166)', () => {
  // A steer landing after only an empty or whitespace token streamed must not
  // seal a stray empty assistant bubble before the steered user row.
  assert.equal(streamItemsHaveRenderableContent([]), false)
  assert.equal(streamItemsHaveRenderableContent([{ type: 'text', content: '' }]), false)
  assert.equal(streamItemsHaveRenderableContent([{ type: 'text', content: '   ' }]), false)
  assert.equal(streamItemsHaveRenderableContent([{ type: 'text', content: '\n' }]), false)
})

test('streamItemsHaveRenderableContent: a single REAL token is kept', () => {
  // Owner contract: a real "I " emitted before the steer is valid output —
  // keep it (placed before the steered user row), do not blanket-discard.
  assert.equal(streamItemsHaveRenderableContent([{ type: 'text', content: 'I ' }]), true)
  assert.equal(streamItemsHaveRenderableContent([{ type: 'text', content: 'I' }]), true)
})

test('streamItemsHaveRenderableContent: any non-text block is renderable', () => {
  assert.equal(
    streamItemsHaveRenderableContent([{ type: 'tool', tool: 'Bash', status: 'running' }]),
    true,
  )
  // Whitespace text + a real tool block is still renderable (the tool counts).
  assert.equal(
    streamItemsHaveRenderableContent([
      { type: 'text', content: ' ' },
      { type: 'tool', tool: 'Bash', status: 'done' },
    ]),
    true,
  )
})
