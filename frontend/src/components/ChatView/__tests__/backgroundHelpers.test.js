import { test } from 'node:test'
import assert from 'node:assert/strict'

import { classifyChatHandoff } from '../chatRuntimeCache.js'
import {
  isResourcePause,
  resourcePausePresentation,
  untilLabel,
  waitPresentation,
} from '../waitingPresentation.js'

test('declared waits stay compact and disclose ownership, deadline, and cost', () => {
  const presented = waitPresentation({
    kind: 'command',
    condition_owner: 'GitHub merge queue',
    interval_secs: 300,
    checks_count: 3,
    next_check_at: '2026-09-02T12:30:00',
    last_checked_at: '2026-09-02T12:25:00',
    deadline_at: '2026-09-02T13:00:00',
  }, Date.parse('2026-09-02T12:26:40Z'))
  assert.equal(presented.owner, 'GitHub merge queue')
  assert.match(presented.summary, /^next check in 4 min · \S/)
  assert.match(presented.checker, /^Möbius · every 5 minutes · next in 4 min · \S/)
  assert.match(presented.activity, /^3 checks · last at/)
  assert.match(presented.timeout, /^This chat wakes to investigate in 34 min · \S/)
  assert.equal(presented.usage,
    'No model tokens while checking · one turn when it wakes')
})

test('wait countdowns read in calm whole units and never count past due', () => {
  const now = Date.parse('2026-09-02T12:00:00Z')
  assert.equal(untilLabel(null, now), null)
  assert.equal(untilLabel('2026-09-02T11:59:00', now), 'now')
  assert.equal(untilLabel('2026-09-02T12:00:30', now), 'in under a minute')
  assert.equal(untilLabel('2026-09-02T12:02:01', now), 'in 3 min')
  assert.equal(untilLabel('2026-09-02T14:00:00+00:00', now), 'in 2 h')
  assert.equal(untilLabel('2026-09-02T14:05:00Z', now), 'in 2 h 5 min')
  assert.equal(untilLabel('2026-09-05T12:00:00', now), 'in 3 days')
  const timer = waitPresentation({ kind: 'timer', due_at: '2026-09-02T12:17:00' }, now)
  assert.match(timer.summary, /^resumes in 17 min · \S/)
})

test('waits without timestamps keep their ordinary fallback labels', () => {
  const timer = waitPresentation({ kind: 'timer' })
  assert.equal(timer.summary, 'resumes later')
  assert.equal(timer.timeout, 'This chat wakes to investigate at not set')

  const command = waitPresentation({ kind: 'command', interval_secs: 300 })
  assert.equal(command.summary, 'every 5 minutes')
  assert.equal(command.checker, 'Möbius · every 5 minutes')

  const resource = resourcePausePresentation({ pause: { kind: 'memory' } })
  assert.equal(resource.next, 'checks again automatically')
})

test('live parent work suppresses the automatic handoff', () => {
  assert.equal(classifyChatHandoff({ authoritativeHandoff: { kind: 'automatic' } }), 'automatic')
  assert.equal(classifyChatHandoff({
    turnActive: true,
    authoritativeHandoff: { kind: 'automatic' },
  }), 'working')
})

test('a platform resource park uses the same visible Waiting handoff', () => {
  const resourcePause = {
    type: 'error',
    resumable: true,
    pause: {
      kind: 'storage',
      resets_at: '2026-09-04T18:50:00Z',
    },
  }
  assert.equal(isResourcePause(resourcePause), true)
  assert.equal(classifyChatHandoff({ resourcePause }), 'automatic')
  const presented = resourcePausePresentation(resourcePause)
  assert.equal(presented.summary, 'Waiting for storage headroom')
  assert.equal(presented.owner, 'Möbius resource monitor')
  assert.equal(
    presented.wakeUp,
    'This chat resumes automatically when pressure clears',
  )
})

test('a resource wait does not show the check time saved on its card', () => {
  // The server keeps one memory/storage card and moves the next check later
  // while headroom stays short, so the saved time would soon be in the past.
  const pause = { check_at: '2026-09-04T18:50:00Z' }
  for (const kind of ['storage', 'memory']) {
    assert.equal(
      resourcePausePresentation({ pause: { ...pause, kind } }).next,
      'checks again automatically',
    )
  }
  const model = resourcePausePresentation(
    { pause: { ...pause, kind: 'model_capacity' } },
    { kind: 'automatic', reason: 'model_capacity' },
  )
  assert.match(model.next, /^checks again (?!automatically)/)
})

test('only eligible handoffs claim an automatic Goal wake', async () => {
  for (const blocker of ['manual_resume', 'resume_failed', 'restart']) {
    assert.equal(classifyChatHandoff({ waits: [{ delivery_pending: true, resume_blocker: blocker }] }), 'recovery')
  }
  assert.equal(classifyChatHandoff({ waits: [{ delivery_pending: true, resume_blocker: 'owner_input' }] }), 'owner_input')
  assert.equal(classifyChatHandoff({ waits: [{ kind: 'timer', delivery_pending: false }] }), 'automatic')
  assert.equal(classifyChatHandoff({ resourcePause: { pause: { kind: 'model_capacity' } } }), 'none')
  assert.equal(classifyChatHandoff({ resourcePause: { pause: { kind: 'rate_limit' } } }), 'none')
  assert.equal(classifyChatHandoff({ resourcePause: { pause: { kind: 'rate_limit' } }, autoResumeEnabled: true }), 'automatic')
  assert.equal(classifyChatHandoff({ turnActive: true }), 'working')
})

test('a delivered wait with no blocker is eligible, but server eligibility wins', async () => {
  const fired = { delivery_pending: true, resume_blocker: null }
  assert.equal(classifyChatHandoff({ waits: [fired] }), 'automatic')
  assert.equal(classifyChatHandoff({
    waits: [fired], authoritativeHandoff: { kind: 'recovery', reason: 'manual_resume' },
  }), 'recovery')
  assert.equal(classifyChatHandoff({
    waits: [fired], authoritativeHandoff: { kind: 'automatic', reason: null },
  }), 'automatic')
  assert.equal(classifyChatHandoff({
    waits: [fired], ownerInput: true,
    authoritativeHandoff: { kind: 'automatic', reason: null },
  }), 'owner_input')
})

test('resource card promises only the backend-confirmed park recovery', () => {
  const model = { pause: { kind: 'model_capacity' } }
  assert.match(resourcePausePresentation(model).wakeUp, /choose another model and Resume/)
  assert.match(resourcePausePresentation(model, {
    kind: 'automatic', reason: 'model_capacity',
  }).wakeUp, /retries this model automatically/)
  assert.match(resourcePausePresentation({ pause: { kind: 'memory' } }, {
    kind: 'owner_input', reason: 'saved_card',
  }).wakeUp, /Answer the saved card/)
  const limit = { pause: { kind: 'rate_limit' } }
  assert.match(resourcePausePresentation(limit, {
    kind: 'recovery', reason: 'app_attributed_work',
  }).wakeUp, /blocked/)
})

for (const kind of ['model_capacity', 'rate_limit', 'memory', 'storage']) {
  test(`${kind} recovery reports platform restoration instead of a model workaround`, () => {
    const block = { pause: { kind } }
    const restoring = resourcePausePresentation(block, { kind: 'automatic', reason: 'restoring_edits' })
    assert.equal(restoring.manual, false)
    assert.equal(restoring.next, 'restoring local work')
    assert.match(restoring.wakeUp, /restor/)
    assert.doesNotMatch(restoring.wakeUp, /choose another model|unavailable/)
    const restart = resourcePausePresentation(block, { kind: 'recovery', reason: 'restart_required' })
    assert.equal(restart.manual, true)
    assert.equal(restart.next, 'server restart needed')
    assert.match(restart.wakeUp, /server restart/)
    assert.doesNotMatch(restart.wakeUp, /choose another model/)
  })
}

test('helper count alone cannot promise an automatic executor', () => {
  assert.equal(classifyChatHandoff({}), 'none')
})
