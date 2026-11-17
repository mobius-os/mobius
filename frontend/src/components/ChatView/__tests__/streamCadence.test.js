import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  TEXT_REVEAL_MIN_COMMIT_MS,
  textRevealBudget,
  TextRevealPacer,
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

test('text reveal carries fractional credit and catches up after a delayed frame', () => {
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
  assert.equal(afterWake.count, 900,
    'render stalls and tab wake-ups must retain their elapsed reveal credit')
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

test('reveal follows sustained incoming speed without jumping to each arrival batch', () => {
  const slow = new TextRevealPacer()
  const fast = new TextRevealPacer()
  slow.receive(0, 0)
  fast.receive(0, 0)
  for (let now = 120; now <= 2400; now += 120) {
    slow.receive(6, now)
    fast.receive(72, now)
    slow.rate(8, 120, now)
    fast.rate(85, 120, now)
  }
  assert.ok(slow.speed < 100)
  assert.ok(fast.speed > 500)
  const before = fast.speed
  fast.receive(1000, 2401)
  fast.rate(1000, 16, 2401)
  assert.ok(fast.speed > before)
  assert.ok(fast.speed < before + 200, 'a burst changes speed gradually')
})

test('a large backlog catches up without capping delayed frame credit', () => {
  const pacer = new TextRevealPacer()
  pacer.receive(10000, 0)
  const charsPerSecond = pacer.rate(10000, 5000, 5000)
  const budget = textRevealBudget({ elapsedMs: 5000, bufferLength: 10000, charsPerSecond })
  assert.equal(budget.count, 10000)
})
