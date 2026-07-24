import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  TEXT_REVEAL_MIN_COMMIT_MS,
  textRevealBudget,
} from '../streamCadence.js'

test('text reveal keeps the existing three-character nominal 60Hz cadence', () => {
  assert.deepEqual(
    textRevealBudget({
      elapsedMs: 1000 / 60,
      bufferLength: 100,
    }),
    { count: 3, carry: 0 },
  )
})

test('text reveal speed follows elapsed time instead of display refresh rate', () => {
  const at30Hz = textRevealBudget({ elapsedMs: 1000 / 30, bufferLength: 100 })
  const at60Hz = textRevealBudget({ elapsedMs: 1000 / 60, bufferLength: 100 })

  assert.equal(at30Hz.count, 6)
  assert.equal(at60Hz.count, 3)
  assert.ok(TEXT_REVEAL_MIN_COMMIT_MS > 1000 / 120,
    '120Hz frames should coalesce instead of causing 120 Markdown commits/sec')
  assert.ok(TEXT_REVEAL_MIN_COMMIT_MS < 1000 / 60,
    'ordinary 60Hz frames should retain the current visual cadence')
})

test('text reveal carries fractional credit and caps wake-from-background bursts', () => {
  const first = textRevealBudget({
    elapsedMs: 16.6,
    bufferLength: 100,
  })
  const second = textRevealBudget({
    elapsedMs: 16.6,
    carry: first.carry,
    bufferLength: 100,
  })
  const afterWake = textRevealBudget({
    elapsedMs: 5000,
    bufferLength: 1000,
  })

  assert.equal(first.count, 2)
  assert.equal(second.count, 3)
  assert.equal(afterWake.count, 9,
    'returning to a visible tab must not dump seconds of buffered text at once')
})

test('text reveal never spends beyond the buffer and clears stale credit', () => {
  assert.deepEqual(
    textRevealBudget({
      elapsedMs: 50,
      carry: 0.8,
      bufferLength: 2,
    }),
    { count: 2, carry: 0 },
  )
  assert.deepEqual(
    textRevealBudget({
      elapsedMs: 50,
      carry: 0.8,
      bufferLength: 0,
    }),
    { count: 0, carry: 0 },
  )
})
