import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { deriveActiveAssistantSelection } from '../activeAssistantSelection.js'

const chatViewSource = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')


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
  const promotionStart = chatViewSource.indexOf('function promoteStreamToMessages')
  const promotionEnd = chatViewSource.indexOf('\n  function ', promotionStart + 1)
  const promotionSource = chatViewSource.slice(
    promotionStart,
    promotionEnd >= 0 ? promotionEnd : undefined,
  )
  const retireAt = promotionSource.indexOf('retiredAssistantItemsRef.current = streamItems')
  const publishAt = promotionSource.indexOf('commitMessages(')

  assert.ok(retireAt >= 0, 'promotion must identify the currently painted live array')
  assert.ok(publishAt > retireAt,
    'the live surface must retire before the durable-row cache publish can render')
  assert.match(chatViewSource,
    /liveItemsRetired:\s*retiredAssistantItemsRef\.current === streamItems/)
})
