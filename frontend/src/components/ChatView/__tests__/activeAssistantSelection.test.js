import test from 'node:test'
import assert from 'node:assert/strict'
import { deriveActiveAssistantSelection } from '../activeAssistantSelection.js'


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
