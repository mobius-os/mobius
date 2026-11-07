import test from 'node:test'
import assert from 'node:assert/strict'

import {
  APPLY_ADMISSION_GRACE_MS,
  APPLY_OBSERVE_INTERVAL_MS,
  APPLY_UNREACHABLE_LIMIT_MS,
  observedApplyResult,
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

test('a finished record of this plan is not this attempt when the baseline read failed', async () => {
  const h = harness(() => ({ plan_id: PLAN, updated_at: 40, active: false, phase: 'complete', error: null }))
  const outcome = await waitForApplyOutcome({ planId: PLAN, after: null, ...h })
  assert.deepEqual(outcome, { settled: false, reason: 'not_started' })
})

test('with no baseline, a record newer than the first one read is this attempt', async () => {
  const h = harness(read => (read < 2
    ? { plan_id: PLAN, updated_at: 40, active: false, phase: 'complete', error: null }
    : { plan_id: PLAN, updated_at: 60, active: false, phase: 'failed', error: 'new failure' }))
  const outcome = await waitForApplyOutcome({ planId: PLAN, after: null, ...h })
  assert.equal(outcome.settled, true)
  assert.equal(outcome.progress.error, 'new failure')
})

test('with no baseline, this plan\'s update still running is followed to its end', async () => {
  const h = harness(read => ({
    plan_id: PLAN, updated_at: 50 + read, active: read < 3, phase: read < 3 ? 'testing' : 'complete', error: null,
  }))
  const outcome = await waitForApplyOutcome({ planId: PLAN, after: null, ...h })
  assert.equal(outcome.settled, true)
  assert.equal(outcome.progress.phase, 'complete')
})

test('an update replaced by another plan before its end is read ends as superseded', async () => {
  const h = harness(read => (read < 2
    ? { plan_id: PLAN, updated_at: 50 + read, active: true, phase: 'testing' }
    : { plan_id: 'other-plan', updated_at: 100 + read, active: read < 10_000, phase: 'testing' }))
  const outcome = await waitForApplyOutcome({ planId: PLAN, after: 40, ...h })
  assert.deepEqual(outcome, { settled: false, reason: 'superseded' })
  assert.equal(h.reads(), 3)
})

const COMPLETE = { settled: true, progress: { phase: 'complete', error: null } }

test('a completed update reports the status read after it', () => {
  const result = observedApplyResult(COMPLETE, { state: 'restart_needed' })
  assert.deepEqual(result, { ok: true, state: 'restart_needed', error: '', errorCode: '', notice: '' })
})

test('a completed update stays a success when the status read after it fails, and says so', () => {
  const result = observedApplyResult(COMPLETE, null)
  assert.equal(result.ok, true)
  assert.equal(result.state, undefined)
  assert.equal(result.error, '')
  assert.match(result.notice, /finished, but its new status could not be read/)
})

test('a failed update keeps its own error and is not a success', () => {
  const failed = { settled: true, progress: { phase: 'failed', error: 'tests regressed' } }
  const result = observedApplyResult(failed, { state: 'rolled_back' })
  assert.equal(result.ok, false)
  assert.equal(result.error, 'tests regressed')
  assert.equal(result.notice, '')
})

test('an unconfirmed outcome asks for a fresh review instead of claiming success', () => {
  const result = observedApplyResult({ settled: false, reason: 'superseded' }, null)
  assert.equal(result.ok, false)
  assert.equal(result.errorCode, 'update_plan_stale')
  assert.equal(result.notice, '')
})
