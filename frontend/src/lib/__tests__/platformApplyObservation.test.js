import test from 'node:test'
import assert from 'node:assert/strict'

import {
  APPLY_ADMISSION_GRACE_MS,
  APPLY_OBSERVE_INTERVAL_MS,
  APPLY_UNREACHABLE_LIMIT_MS,
  waitForApplyOutcome,
} from '../platformApplyObservation.js'

// A fake clock whose sleep advances time, so long waits run instantly.
function harness(readings) {
  let clock = 1_000
  let reads = 0
  return {
    now: () => clock,
    sleep: async ms => { clock += ms },
    readProgress: async () => {
      const next = readings(reads++, clock)
      if (next instanceof Error) throw next
      return next
    },
    reads: () => reads,
  }
}

const PLAN = 'plan-a'

test('a lost Apply reply settles with the completed record of this attempt', async () => {
  const h = harness(read => ({
    plan_id: PLAN, updated_at: 50 + read, active: read < 3, phase: read < 3 ? 'testing' : 'complete', error: null,
  }))
  const outcome = await waitForApplyOutcome({ planId: PLAN, after: 40, ...h })
  assert.equal(outcome.settled, true)
  assert.equal(outcome.progress.phase, 'complete')
  assert.equal(h.reads(), 4)
})

test('an earlier finished attempt of the same plan is not mistaken for this one', async () => {
  const h = harness(read => (read < 2
    ? { plan_id: PLAN, updated_at: 40, active: false, phase: 'failed', error: 'old failure' }
    : { plan_id: PLAN, updated_at: 60, active: false, phase: 'complete', error: null }))
  const outcome = await waitForApplyOutcome({ planId: PLAN, after: 40, ...h })
  assert.equal(outcome.progress.phase, 'complete')
  assert.equal(outcome.progress.error, null)
})

test('a long update keeps being followed while the server reports it active', async () => {
  const activeReads = Math.ceil((APPLY_UNREACHABLE_LIMIT_MS * 4) / APPLY_OBSERVE_INTERVAL_MS)
  const h = harness(read => ({
    plan_id: PLAN, updated_at: 50, active: read < activeReads, phase: read < activeReads ? 'testing' : 'complete',
  }))
  const outcome = await waitForApplyOutcome({ planId: PLAN, after: 40, ...h })
  assert.equal(outcome.settled, true)
})

test('an update that never appears is reported as not started', async () => {
  const h = harness(() => ({ plan_id: 'other-plan', updated_at: 10, active: false, phase: 'complete' }))
  const outcome = await waitForApplyOutcome({ planId: PLAN, after: 40, ...h })
  assert.deepEqual(outcome, { settled: false, reason: 'not_started' })
  assert.ok(h.now() - 1_000 >= APPLY_ADMISSION_GRACE_MS)
})

test('a server that stays unreachable ends the wait instead of spinning forever', async () => {
  const h = harness(() => new Error('offline'))
  const outcome = await waitForApplyOutcome({ planId: PLAN, after: 40, ...h })
  assert.deepEqual(outcome, { settled: false, reason: 'unreachable' })
})
