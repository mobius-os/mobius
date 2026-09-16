import test from 'node:test'
import assert from 'node:assert/strict'
import { modeForQuestionEditingViewportChange } from '../scroll/geometry.js'

test('question editing retires passive follow and rebases a held viewport to the editor', () => {
  const staleHold = { kind: 'ANCHOR_AT', key: 'before-edit', offset: 20 }
  const caretHold = { kind: 'ANCHOR_AT', key: 'question-row', offset: 84 }
  assert.equal(modeForQuestionEditingViewportChange(staleHold, caretHold), caretHold)
  assert.equal(
    modeForQuestionEditingViewportChange({ kind: 'FOLLOW_BOTTOM' }, caretHold),
    caretHold,
  )

  for (const strongerMode of [
    { kind: 'PIN_USER_MSG', cid: 'c-1' },
    {
      kind: 'ANCHOR_AT',
      key: 'question-row',
      offset: 84,
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
