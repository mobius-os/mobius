import test from 'node:test'
import assert from 'node:assert/strict'

import {
  questionAnswerHandoffReady,
  questionAnswerHandoffReducer,
} from '../questionAnswerHandoff.js'

const submission = { mode: { kind: 'ANCHOR_AT' }, readerIntentVersion: 3 }

function submitted() {
  return questionAnswerHandoffReducer(null, {
    type: 'submitted',
    submission,
    questionKey: 'question_id:q-1',
  })
}

test('answer acceptance alone never readies the question follow handoff', () => {
  const accepted = questionAnswerHandoffReducer(submitted(), {
    type: 'accepted',
    submission,
  })
  assert.equal(questionAnswerHandoffReady(accepted), false)
})

test('response activity alone never readies the question follow handoff', () => {
  const active = questionAnswerHandoffReducer(submitted(), {
    type: 'response_activity',
    questionKey: 'question_id:q-1',
  })
  assert.equal(questionAnswerHandoffReady(active), false)
})

test('acceptance then response activity readies the handoff', () => {
  const accepted = questionAnswerHandoffReducer(submitted(), {
    type: 'accepted',
    submission,
  })
  const ready = questionAnswerHandoffReducer(accepted, {
    type: 'response_activity',
    questionKey: 'question_id:q-1',
  })
  assert.equal(questionAnswerHandoffReady(ready), true)
})

test('response activity then acceptance readies the handoff', () => {
  const active = questionAnswerHandoffReducer(submitted(), {
    type: 'response_activity',
    questionKey: 'question_id:q-1',
  })
  const ready = questionAnswerHandoffReducer(active, {
    type: 'accepted',
    submission,
  })
  assert.equal(questionAnswerHandoffReady(ready), true)
})

test('foreign response activity and stale acceptance cannot release a card', () => {
  const current = submitted()
  const foreignActivity = questionAnswerHandoffReducer(current, {
    type: 'response_activity',
    questionKey: 'question_id:q-2',
  })
  const staleAcceptance = questionAnswerHandoffReducer(foreignActivity, {
    type: 'accepted',
    submission: { ...submission },
  })
  assert.equal(staleAcceptance, current)
})

test('stream end cancels a silent handoff but preserves a terminal response flush', () => {
  assert.equal(questionAnswerHandoffReducer(submitted(), {
    type: 'stream_ended',
  }), null)

  const active = questionAnswerHandoffReducer(submitted(), {
    type: 'response_activity',
    questionKey: 'question_id:q-1',
  })
  assert.equal(questionAnswerHandoffReducer(active, {
    type: 'stream_ended',
  }), active)
})

test('only the owning submission can cancel or release the handoff', () => {
  const current = submitted()
  assert.equal(questionAnswerHandoffReducer(current, {
    type: 'cancelled',
    submission: { ...submission },
  }), current)
  assert.equal(questionAnswerHandoffReducer(current, {
    type: 'released',
    submission,
  }), null)
})
