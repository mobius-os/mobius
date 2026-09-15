import assert from 'node:assert/strict'
import test from 'node:test'

import { createMockChatRuntime } from './_mockChatRuntime.mjs'

test('chat detail and runtime routes share one ordered snapshot', () => {
  const runtime = createMockChatRuntime({
    running: true,
    pending_question_id: 'question-1',
  })

  const first = runtime.snapshot()
  assert.deepEqual(runtime.detail({ id: 'chat-1', running: false }), {
    id: 'chat-1',
    ...first,
  })
  assert.equal(first.runtime_revision, 0)
  assert.equal(first.run_id, 'fixture-run')
  assert.equal(first.run_status, 'running')

  const settled = runtime.update({
    running: false,
    pending_question_id: null,
  })
  assert.equal(settled.runtime_revision, 1)
  assert.equal(settled.run_id, first.run_id)
  assert.equal(settled.run_status, 'completed')
  assert.equal(runtime.snapshot().runtime_revision, 1)
})

test('an idle fixture has a complete production-shaped runtime projection', () => {
  const runtime = createMockChatRuntime()
  assert.deepEqual(runtime.snapshot(), {
    running: false,
    run_id: null,
    run_status: null,
    active_assistant_message_id: null,
    recovery_run_id: null,
    active_goal_objective: null,
    goal: null,
    pending_messages: [],
    pending_question_id: null,
    updated_at: null,
    waits: [],
    background_helpers: [],
    runtime_revision: 0,
  })
})
