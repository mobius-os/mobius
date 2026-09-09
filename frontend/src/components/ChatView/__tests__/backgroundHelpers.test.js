import { test } from 'node:test'
import assert from 'node:assert/strict'

import { chatHasSelfResumingHandoff } from '../chatRuntimeCache.js'
import {
  helperPresentation,
  isResourcePause,
  resourcePausePresentation,
  waitPresentation,
} from '../waitingPresentation.js'

test('idle waking helpers own one visible automatic Waiting handoff', () => {
  const helpers = {
    count: 2,
    items: [{ task_key: 'review_copy' }, { task_key: 'verify-build' }],
  }
  assert.equal(chatHasSelfResumingHandoff({ backgroundHelpers: helpers }), true)
  assert.deepEqual(helperPresentation(helpers), {
    count: 2,
    tasks: ['Review copy', 'Verify build'],
    summary: 'Waiting on 2 helpers',
    owner: '2 helper agents',
    usage: 'Helpers use their own turns · no separate monitor is polling',
  })
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

test('live parent work suppresses the helper handoff without erasing it', () => {
  const backgroundHelpers = { count: 1, items: [] }
  assert.equal(chatHasSelfResumingHandoff({ backgroundHelpers }), true)
  assert.equal(chatHasSelfResumingHandoff({
    turnActive: true,
    backgroundHelpers,
  }), false)
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
  assert.equal(chatHasSelfResumingHandoff({ resourcePause }), true)
  const presented = resourcePausePresentation(resourcePause)
  assert.equal(presented.summary, 'Waiting for storage headroom')
  assert.equal(presented.owner, 'Möbius resource monitor')
  assert.equal(
    presented.wakeUp,
    'This chat resumes automatically when pressure clears',
  )
})
