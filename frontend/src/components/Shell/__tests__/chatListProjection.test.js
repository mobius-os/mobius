import test from 'node:test'
import assert from 'node:assert/strict'
import {
  ownerInputChangeFromEvent,
  withChatListRowPatch,
  withChatOwnerActivity,
  withChatOwnerInput,
  withChatRename,
  withChatRunState,
  withoutSettledLocalChatRuns,
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

test('fresh reconnect truth retires only settled optimistic run markers', () => {
  const local = new Set(['settled', 'running', 'not-listed', 'uncertain'])
  const reconciled = withoutSettledLocalChatRuns(local, [
    { id: 'settled', running: false },
    { id: 'running', running: true },
    { id: 'uncertain' },
  ])

  assert.deepEqual([...reconciled], ['running', 'not-listed', 'uncertain'])
  assert.deepEqual([...local], ['settled', 'running', 'not-listed', 'uncertain'],
    'reconciliation must not mutate the previous React state')
  assert.equal(
    withoutSettledLocalChatRuns(reconciled, [
      { id: 'running', running: true },
    ]),
    reconciled,
    'a no-op reconciliation preserves Set identity',
  )
})

test('owner-input events project kind while only questions update their durable id', () => {
  const waiting = withChatOwnerInput(rows, 'a', {
    kind: 'question',
    questionId: 'question-1',
  })
  assert.equal(waiting[0].owner_input_kind, 'question')
  assert.equal(waiting[0].pending_question_id, 'question-1')
  assert.equal(waiting[1], rows[1])

  const secure = withChatOwnerInput(waiting, 'a', { kind: 'secure_input' })
  assert.equal(secure[0].owner_input_kind, 'secure_input')
  assert.equal(secure[0].pending_question_id, 'question-1')

  const answered = withChatOwnerInput(waiting, 'a', {
    kind: null,
    questionId: null,
  })
  assert.equal(answered[0].owner_input_kind, null)
  assert.equal(answered[0].pending_question_id, null)
})

test('owner-input event normalization supports both shell generations', () => {
  assert.deepEqual(ownerInputChangeFromEvent({
    inputKind: 'secure_input',
  }), { kind: 'secure_input' })
  assert.deepEqual(ownerInputChangeFromEvent({
    inputKind: 'question',
    questionId: 'question-1',
  }), { kind: 'question', questionId: 'question-1' })
  assert.deepEqual(ownerInputChangeFromEvent({
    questionId: 'legacy-question',
  }), { kind: 'question', questionId: 'legacy-question' })
  assert.deepEqual(ownerInputChangeFromEvent({
    questionId: null,
  }), { kind: null, questionId: null })
})
