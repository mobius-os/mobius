import test from 'node:test'
import assert from 'node:assert/strict'
import {
  commitAssistantPromotion,
  deriveActiveAssistantSelection,
} from '../activeAssistantSelection.js'


test('an idle chat exposes no active assistant surface', () => {
  const result = deriveActiveAssistantSelection({
    turnActive: false,
    messages: [],
    streamItems: [],
    findBridgeIndex: () => {
      throw new Error('idle selection must not probe a bridge')
    },
  })
  assert.equal(result.showActiveAssistantSurface, false)
  assert.equal(result.activeAssistantIsStreaming, false)
  assert.equal(result.activeMirrorMsg, null)
})

test('a live-only answer selects the streaming surface', () => {
  const result = deriveActiveAssistantSelection({
    turnActive: true,
    messages: [{ role: 'user', content: 'hello', ts: 1 }],
    streamItems: [{ type: 'text', content: 'hi' }],
    findBridgeIndex: () => -1,
  })
  assert.equal(result.showActiveAssistantSurface, true)
  assert.equal(result.activeAssistantIsStreaming, true)
  assert.equal(result.useDbActivePayload, false)
})

test('a durable pending question outranks a richer stale stream snapshot', () => {
  const pendingQuestionId = 'question-restart'
  const durableReply = {
    role: 'assistant',
    ts: 2,
    content: 'I checked the packaged game.',
    blocks: [
      { type: 'text', content: 'I checked the packaged game.' },
      {
        type: 'question',
        question_id: pendingQuestionId,
        questions: [{ question: 'Restart now?', options: [{ label: 'Restarted' }] }],
      },
    ],
  }
  const staleStreamItems = [
    { type: 'text', content: 'I checked the packaged game.' },
    {
      type: 'tool',
      tool: 'Bash',
      status: 'done',
      input: 'run a long pre-question inspection',
      output: 'detailed output that made the raw snapshot look richer',
    },
  ]

  const result = deriveActiveAssistantSelection({
    turnActive: true,
    messages: [
      { role: 'user', content: 'test the game', ts: 1 },
      durableReply,
    ],
    streamItems: staleStreamItems,
    findBridgeIndex: () => 1,
  })

  assert.equal(result.activeMirrorMsg, durableReply)
  assert.equal(result.useDbActivePayload, true)
  assert.equal(result.showActiveAssistantSurface, true)
})

test('a source-rich promoted answer is not painted again from its retired live array', () => {
  const messages = [
    { role: 'user', content: 'initial request', ts: 1 },
    {
      role: 'assistant',
      content: 'I checked the current behavior.',
      ts: 2,
      blocks: [
        { type: 'text', content: 'I checked the current behavior.' },
        {
          type: 'tool', tool: 'WebSearch', status: 'done', input: 'current behavior',
          output: 'results', tool_use_id: 'search-1',
          sources: [{ title: 'Reference', url: 'https://example.com/reference' }],
        },
      ],
    },
    { role: 'user', content: 'steer the active turn', ts: 3 },
  ]
  const streamItems = [
    { type: 'text', content: 'I checked the current behavior.' },
    {
      type: 'tool', tool: 'WebSearch', status: 'running', input: 'current behavior',
      output: 'results', tool_use_id: 'search-1',
      // Catch-up can temporarily lack the source metadata already persisted.
    },
  ]

  const result = deriveActiveAssistantSelection({
    turnActive: true,
    messages,
    streamItems,
    liveItemsRetired: true,
    findBridgeIndex: () => -1,
  })

  assert.equal(result.staleLiveAssistantAfterPromotion, true)
  assert.equal(result.showActiveAssistantSurface, false,
    'the durable row remains in order and the stale live copy is suppressed')
})

test('a current post-steer answer still renders even when it repeats the sealed answer', () => {
  const result = deriveActiveAssistantSelection({
    turnActive: true,
    messages: [
      { role: 'user', content: 'initial request', ts: 1 },
      { role: 'assistant', content: 'sealed answer', ts: 2 },
      { role: 'user', content: 'steer', ts: 3 },
    ],
    streamItems: [{ type: 'text', content: 'sealed answer' }],
    liveItemsRetired: false,
    findBridgeIndex: () => -1,
  })

  assert.equal(result.staleLiveAssistantAfterPromotion, false)
  assert.equal(result.showActiveAssistantSurface, true)
  assert.equal(result.activeAssistantIsStreaming, true)
})

test('promotion retires the exact painted array before publishing the durable row', () => {
  const paintedItems = [{ type: 'text', content: 'painted answer' }]
  const promotedItems = [{ type: 'text', content: 'durable answer' }]
  const retiredItemsRef = { current: null }
  let publishedMessages = null

  commitAssistantPromotion({
    retiredItemsRef,
    paintedItems,
    promotedItems,
    bridgeTs: null,
    commitMessages(updater, unused, options) {
      assert.equal(retiredItemsRef.current, paintedItems,
        'the painted live array must retire before the cache publish')
      assert.equal(unused, undefined)
      assert.deepEqual(options, { force: true })
      publishedMessages = updater([{ role: 'user', content: 'hello' }])
    },
  })

  assert.equal(retiredItemsRef.current, paintedItems)
  assert.equal(publishedMessages.at(-1).content, 'durable answer')
})
