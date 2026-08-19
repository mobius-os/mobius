import test from 'node:test'
import assert from 'node:assert/strict'
import { modeForQuestionEditingViewportChange } from '../scroll/geometry.js'

test('question editing rebases only an ordinary held viewport to native caret movement', () => {
  const staleHold = { kind: 'ANCHOR_AT', key: 'before-edit', offset: 20 }
  const caretHold = { kind: 'ANCHOR_AT', key: 'question-row', offset: 84 }
  assert.equal(modeForQuestionEditingViewportChange(staleHold, caretHold), caretHold)

  for (const strongerMode of [
    { kind: 'PIN_USER_MSG', cid: 'c-1' },
    { kind: 'FOLLOW_BOTTOM' },
    {
      kind: 'ANCHOR_AT',
      key: 'question-row',
      offset: 84,
      questionSubmitViewportH: 600,
      questionSubmitBaseMode: { kind: 'FOLLOW_BOTTOM' },
    },
  ]) {
    assert.equal(modeForQuestionEditingViewportChange(strongerMode, caretHold), strongerMode)
  }
  assert.equal(modeForQuestionEditingViewportChange(staleHold, null), staleHold)
  assert.equal(
    modeForQuestionEditingViewportChange(caretHold, { ...caretHold }),
    caretHold,
    'an unchanged caret hold does not manufacture a mode transition',
  )
})
