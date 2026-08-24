import { test } from 'node:test'
import assert from 'node:assert/strict'

import { modeAfterQuestionResponseStart } from '../useScrollMode.js'

test('first post-answer activity resumes only the follow mode that submitted it', () => {
  const follow = { kind: 'FOLLOW_BOTTOM' }
  const heldMode = {
    kind: 'ANCHOR_AT',
    key: 'assistant-with-question',
    offset: 60,
    questionSubmitBaseMode: follow,
  }
  const submission = { mode: heldMode, readerIntentVersion: 7 }

  assert.equal(modeAfterQuestionResponseStart({
    currentMode: heldMode,
    submission,
    currentReaderIntentVersion: 7,
  }), follow)
  assert.equal(modeAfterQuestionResponseStart({
    currentMode: follow,
    submission,
    currentReaderIntentVersion: 7,
  }), follow, 'an already-restored follow remains stable')

  const readerHold = { kind: 'ANCHOR_AT', key: 'older-row', offset: 20 }
  assert.equal(modeAfterQuestionResponseStart({
    currentMode: readerHold,
    submission,
    currentReaderIntentVersion: 8,
  }), readerHold, 'a reader scroll during submission cancels follow restoration')
  assert.equal(modeAfterQuestionResponseStart({
    currentMode: readerHold,
    submission,
    currentReaderIntentVersion: 7,
  }), readerHold, 'a newer semantic location is never overwritten')
})

test('post-answer activity keeps a pre-submit reading hold', () => {
  const baseMode = { kind: 'ANCHOR_AT', key: 'older-row', offset: 20 }
  const submittedMode = {
    kind: 'ANCHOR_AT',
    key: 'assistant-with-question',
    offset: 60,
    questionSubmitBaseMode: baseMode,
  }
  assert.equal(modeAfterQuestionResponseStart({
    currentMode: submittedMode,
    submission: { mode: submittedMode, readerIntentVersion: 3 },
    currentReaderIntentVersion: 3,
  }), submittedMode)
})
