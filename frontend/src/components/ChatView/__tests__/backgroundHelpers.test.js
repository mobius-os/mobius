import { test } from 'node:test'
import assert from 'node:assert/strict'

import { classifyChatHandoff } from '../chatRuntimeCache.js'
import {
  helperPresentation,
  isResourcePause,
  resourcePausePresentation,
  waitPresentation,
} from '../waitingPresentation.js'

test('idle waking helpers own one visible automatic Waiting handoff', () => {
  const helpers = {
    count: 2,
    items: [{ title: 'Review copy' }, { title: 'Verify build' }],
  }
  assert.equal(classifyChatHandoff({ backgroundHelpers: helpers, authoritativeHandoff: { kind: 'automatic' } }), 'automatic')
  assert.deepEqual(helperPresentation(helpers, { kind: 'automatic' }), {
    count: 2,
    tasks: ['Review copy', 'Verify build'],
    summary: 'Waiting on 2 helpers',
    owner: '2 helper agents',
    automatic: true,
    usage: 'Usage unknown',
  })
  assert.equal(helperPresentation(helpers, { kind: 'recovery' }).automatic, false)
})

test('declared waits stay compact and disclose ownership, deadline, and cost', () => {
  const presented = waitPresentation({
    kind: 'command',
    condition_owner: 'GitHub merge queue',
    interval_secs: 300,
    checks_count: 3,
    next_check_at: '2026-09-02T12:30:00',
    last_checked_at: '2026-09-02T12:25:00',
    deadline_at: '2026-09-02T13:00:00',
  })
  assert.equal(presented.owner, 'GitHub merge queue')
  assert.match(presented.checker, /Möbius · every 5 minutes · next at/)
  assert.match(presented.activity, /^3 checks · last at/)
  assert.match(presented.timeout, /^This chat wakes to investigate at/)
  assert.equal(presented.usage,
    'No model tokens while checking · one turn when it wakes')
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

test('live parent work suppresses the helper handoff without erasing it', () => {
  const backgroundHelpers = { count: 1, items: [] }
  assert.equal(classifyChatHandoff({ backgroundHelpers, authoritativeHandoff: { kind: 'automatic' } }), 'automatic')
  assert.equal(classifyChatHandoff({
    turnActive: true,
    backgroundHelpers,
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

test('only eligible handoffs claim an automatic Goal wake', async () => {
  for (const blocker of ['manual_resume', 'resume_failed', 'restart']) {
    assert.equal(classifyChatHandoff({ waits: [{ delivery_pending: true, resume_blocker: blocker }] }), 'recovery')
  }
  assert.equal(classifyChatHandoff({ waits: [{ delivery_pending: true, resume_blocker: 'owner_input' }] }), 'owner_input')
  assert.equal(classifyChatHandoff({ waits: [{ kind: 'timer', delivery_pending: false }] }), 'automatic')
  assert.equal(classifyChatHandoff({ resourcePause: { pause: { kind: 'model_capacity' } } }), 'none')
  assert.equal(classifyChatHandoff({ resourcePause: { pause: { kind: 'rate_limit' } } }), 'none')
  assert.equal(classifyChatHandoff({ resourcePause: { pause: { kind: 'rate_limit' } }, autoResumeEnabled: true }), 'automatic')
  assert.equal(classifyChatHandoff({ backgroundHelpers: { count: 1 }, turnActive: true }), 'working')
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
  assert.equal(classifyChatHandoff({ backgroundHelpers: { count: 3, items: [] } }), 'none')
  assert.equal(helperPresentation({ count: 3 }).automatic, false)
})
