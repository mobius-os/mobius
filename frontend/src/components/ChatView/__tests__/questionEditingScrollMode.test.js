import test from 'node:test'
import assert from 'node:assert/strict'
import {
  modeForInlineEditorGrowth,
  modeForQuestionEditingViewportChange,
  modeForQuestionEditorGrowth,
} from '../scroll/geometry.js'

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

// A minimal laid-out transcript: one keyed row holding a question card whose
// answer field is the editor. Client coordinates follow scrollTop the way a
// real scroll container's would; the scroller's own top is client 0.
function bottomCardTranscript({ scrollTop, cardBottom, editorBottom, editorHeight = 40 }) {
  const scrollEl = {
    scrollTop,
    scrollHeight: 4000,
    clientHeight: 800,
    clientWidth: 400,
    currentCSSZoom: 1,
    getBoundingClientRect: () => ({ top: 0, bottom: 800, height: 800, width: 400, left: 0 }),
  }
  const row = {
    dataset: { key: 'question-row' },
    offsetTop: 2000,
    offsetHeight: 1200,
    children: [],
    getBoundingClientRect: () => ({ top: 2000 - scrollEl.scrollTop, height: 1200 }),
  }
  scrollEl.querySelectorAll = () => [row]
  const card = {
    getBoundingClientRect: () => ({ top: cardBottom - 300, bottom: cardBottom }),
  }
  const editor = {
    closest: selector => (selector === '[data-question-id]' ? card : null),
    getBoundingClientRect: () => ({ top: editorBottom - editorHeight, bottom: editorBottom, height: editorHeight }),
  }
  return { scrollEl, editor }
}

test('an answer typed at the bottom of the chat grows upward, like the composer', () => {
  // The card rests just above the composer (usable bottom 740); the field has
  // just grown by one 20px line, so its lower edge was at 660 before.
  const { scrollEl, editor } = bottomCardTranscript({ scrollTop: 1500, cardBottom: 706, editorBottom: 680 })
  const hold = modeForInlineEditorGrowth({
    scrollEl, editor, growth: 20, visibleTop: 0, visibleBottom: 740, bottomBand: 70,
  })
  // The hold scrolls by exactly the growth: the field's lower edge, and the
  // Submit row under it, stay where they were while content above moves up.
  assert.deepEqual(hold, { kind: 'ANCHOR_AT', key: 'question-row', offset: 2000 - 1520 })

  // Deleting a line moves the conversation back down by the same amount.
  const shrink = modeForInlineEditorGrowth({
    scrollEl, editor, growth: -20, visibleTop: 0, visibleBottom: 740, bottomBand: 70,
  })
  assert.equal(shrink.offset, 2000 - 1480)

  // A phone keyboard can leave the card's Submit row under the composer while
  // the field itself is visible: still the bottom, still upward growth.
  const keyboard = bottomCardTranscript({ scrollTop: 1500, cardBottom: 790, editorBottom: 730 })
  assert.ok(modeForInlineEditorGrowth({
    ...keyboard, growth: 20, visibleTop: 0, visibleBottom: 740, bottomBand: 70,
  }))
})

test('an answer card higher up grows down, and a held-away reader is not moved', () => {
  // Room below the card: the field grows into it with no scroll.
  const roomy = bottomCardTranscript({ scrollTop: 1500, cardBottom: 420, editorBottom: 394 })
  assert.equal(modeForInlineEditorGrowth({
    ...roomy, growth: 20, visibleTop: 0, visibleBottom: 740, bottomBand: 70,
  }), null)

  // The reader scrolled the field out of view under the composer: their
  // reading hold wins and the ordinary caret reveal decides instead.
  const hidden = bottomCardTranscript({ scrollTop: 1500, cardBottom: 900, editorBottom: 870 })
  assert.equal(modeForInlineEditorGrowth({
    ...hidden, growth: 20, visibleTop: 0, visibleBottom: 740, bottomBand: 70,
  }), null)

  // No size change (a width-only observation, the first observation) is no hold.
  const still = bottomCardTranscript({ scrollTop: 1500, cardBottom: 706, editorBottom: 680 })
  assert.equal(modeForInlineEditorGrowth({
    ...still, growth: 0, visibleTop: 0, visibleBottom: 740, bottomBand: 70,
  }), null)
})

test('upward answer growth is adopted by reading holds and settled pins only', () => {
  const hold = { kind: 'ANCHOR_AT', key: 'question-row', offset: 480 }
  assert.equal(modeForQuestionEditorGrowth({ kind: 'ANCHOR_AT', key: 'x', offset: 0 }, hold), hold)
  assert.equal(modeForQuestionEditorGrowth({ kind: 'PIN_USER_MSG', cid: 'c-1' }, hold), hold)

  for (const owned of [
    // FOLLOW_BOTTOM already keeps the tail, which is the same upward growth.
    { kind: 'FOLLOW_BOTTOM' },
    // An armed pin keeps its reservation handoff to FOLLOW_BOTTOM.
    { kind: 'PIN_USER_MSG', cid: 'c-1', followWhenFilled: true },
    // A submitted answer's frozen position is not reopened by layout.
    { kind: 'ANCHOR_AT', key: 'q', offset: 84, questionSubmitBaseMode: { kind: 'FOLLOW_BOTTOM' } },
  ]) {
    assert.equal(modeForQuestionEditorGrowth(owned, hold), owned)
  }
  const anchor = { kind: 'ANCHOR_AT', key: 'x', offset: 0 }
  assert.equal(modeForQuestionEditorGrowth(anchor, null), anchor)
})
