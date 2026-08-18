import test from 'node:test'
import assert from 'node:assert/strict'
import {
  withChatListRowPatch,
  withChatOwnerActivity,
  withChatPendingQuestion,
  withChatRename,
  withChatRunState,
} from '../chatListProjection.js'

const rows = [
  { id: 'a', title: 'A', activity_at: '2026-08-01T10:00:00Z', has_messages: false, running: false },
  { id: 'b', title: 'B', activity_at: '2026-08-01T11:00:00Z', has_messages: true, running: false },
]

test('exact list patches preserve every unrelated row identity', () => {
  const next = withChatListRowPatch(rows, 'a', { running: true })
  assert.notEqual(next, rows)
  assert.notEqual(next[0], rows[0])
  assert.equal(next[1], rows[1])
  assert.equal(withChatListRowPatch(next, 'a', { running: true }), next)
})

test('owner activity advances recency without a complete list refetch', () => {
  const next = withChatOwnerActivity(rows, 'a', '2026-08-01T12:00:00Z')
  assert.equal(next[0].has_messages, true)
  assert.equal(next[0].activity_at, '2026-08-01T12:00:00Z')
  assert.equal(
    withChatOwnerActivity(next, 'a', '2026-08-01T09:00:00Z'),
    next,
    'an older duplicate event is a referential no-op',
  )
})

test('run and rename events project only the committed fields they carry', () => {
  const running = withChatRunState(rows, 'a', true)
  assert.equal(running[0].running, true)
  assert.equal(running[0].has_messages, true)
  const renamed = withChatRename(running, 'a', {
    title: 'Current topic',
    updatedAt: '2026-08-01T12:30:00Z',
  })
  assert.equal(renamed[0].title, 'Current topic')
  assert.equal(renamed[0].updated_at, '2026-08-01T12:30:00Z')
})

test('pending-question events project the durable owner-input marker', () => {
  const waiting = withChatPendingQuestion(rows, 'a', 'question-1')
  assert.equal(waiting[0].pending_question_id, 'question-1')
  assert.equal(waiting[1], rows[1])

  const answered = withChatPendingQuestion(waiting, 'a', null)
  assert.equal(answered[0].pending_question_id, null)
})
