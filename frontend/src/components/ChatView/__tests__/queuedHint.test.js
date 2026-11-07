import test from 'node:test'
import assert from 'node:assert/strict'

import { queuedHint } from '../queuedHint.js'

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

test('a queue a Stop held waits for the owner, during a turn too', () => {
  assert.equal(queuedHint({ turnActive: false, held: 2, total: 2 }), 'Waiting for you — sends when you send')
  assert.equal(queuedHint({ turnActive: true, held: 2, total: 2 }), 'Waiting for you — sends when you send')
})


test('with held and new rows the caption tells them apart', () => {
  assert.equal(queuedHint({ turnActive: true, held: 1, total: 2 }),
    '1 waiting for you; the rest send after this turn')
  assert.equal(queuedHint({ turnActive: false, held: 1, total: 2 }), '1 waiting for you')
})
