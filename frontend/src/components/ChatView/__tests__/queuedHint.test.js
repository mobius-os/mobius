import test from 'node:test'
import assert from 'node:assert/strict'

import { queuedHint, steerPendingHint } from '../queuedHint.js'

test('a delivered mid-turn steer says when the agent reads it, never that it is still waiting to send', () => {
  const hint = steerPendingHint()
  assert.equal(hint, 'Agent will read this after its current step')
  assert.doesNotMatch(hint, /send|turn finishes/i)
})

test('a live turn keeps the original "after the current turn finishes" copy', () => {
  assert.equal(
    queuedHint({ turnActive: true, online: true, restarting: false }),
    'Will send after the current turn finishes',
  )
  // turnActive dominates even while offline/restarting — a real turn IS running.
  assert.equal(
    queuedHint({ turnActive: true, online: false, restarting: true }),
    'Will send after the current turn finishes',
  )
})

test('no turn while restarting or offline promises delivery on reconnect, not a phantom turn', () => {
  assert.equal(
    queuedHint({ turnActive: false, online: true, restarting: true }),
    'Will send when you reconnect',
  )
  assert.equal(
    queuedHint({ turnActive: false, online: false, restarting: false }),
    'Will send when you reconnect',
  )
})

test('idle + online + no turn is neutral, never the misleading turn copy', () => {
  assert.equal(
    queuedHint({ turnActive: false, online: true, restarting: false }),
    'Queued to send',
  )
})
