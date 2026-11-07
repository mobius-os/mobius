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
// answer field is the editor. The rects are the ones observed after the edit
// was laid out (including any scroll the browser applied during it); the
// scroller's own top is client 0.
function bottomCardTranscript({
  scrollTop, cardBottom, editorBottom, editorHeight = 40, scrollHeight = 4000,
}) {
  const scrollEl = {
    scrollTop,
    scrollHeight,
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

const bottomViewport = { visibleTop: 0, visibleBottom: 740, bottomBand: 70 }

test('an answer typed at the bottom of the chat grows upward, like the composer', () => {
  // The card rests just above the composer (usable bottom 740); the field has
  // just grown by one 20px line, so its lower edge was at 660 before.
  const { scrollEl, editor } = bottomCardTranscript({ scrollTop: 1500, cardBottom: 706, editorBottom: 680 })
  const hold = modeForInlineEditorGrowth({
    scrollEl, editor, growth: 20, scrollTopBefore: 1500, ...bottomViewport,
  })
  // The hold scrolls by exactly the growth: the field's lower edge, and the
  // Submit row under it, stay where they were while content above moves up.
  assert.deepEqual(hold, { kind: 'ANCHOR_AT', key: 'question-row', offset: 2000 - 1520 })

  // A phone keyboard can leave the card's Submit row under the composer while
  // the field itself is visible: still the bottom, still upward growth.
  const keyboard = bottomCardTranscript({ scrollTop: 1500, cardBottom: 790, editorBottom: 730 })
  assert.ok(modeForInlineEditorGrowth({
    ...keyboard, growth: 20, scrollTopBefore: 1500, ...bottomViewport,
  }))
})

test('growth the browser already scrolled for is not applied a second time', () => {
  // The native caret reveal scrolled 10px while the field grew 20px, so the
  // lower edge observed at 670 was at 660 before the edit. The hold lands at
  // the pre-edit position plus the growth, not the revealed position plus it.
  const revealed = bottomCardTranscript({ scrollTop: 1510, cardBottom: 696, editorBottom: 670 })
  const hold = modeForInlineEditorGrowth({
    ...revealed, growth: 20, scrollTopBefore: 1500, ...bottomViewport,
  })
  assert.equal(hold.offset, 2000 - 1520)
})

test('a shrinking answer moves the conversation back down by its own height', () => {
  // With scroll range to spare, deleting a 20px line leaves scrollTop alone,
  // so the hold moves the conversation down by the line.
  const spare = bottomCardTranscript({ scrollTop: 1000, cardBottom: 720, editorBottom: 694 })
  const hold = modeForInlineEditorGrowth({
    ...spare, growth: -20, scrollTopBefore: 1000, ...bottomViewport,
  })
  assert.equal(hold.offset, 2000 - 980)
})

test('a deletion at the physical tail keeps the clamp the browser already applied', () => {
  // scrollTop sat at its maximum 1500; the content shrank by 20px and the
  // browser clamped scrollTop to the new maximum 1480 during layout, which
  // already kept the card's bottom at 740. Adding the shrink again would
  // write 1460 and slide the card 20px under the composer.
  const tail = bottomCardTranscript({
    scrollTop: 1480, scrollHeight: 2280, cardBottom: 740, editorBottom: 714,
  })
  assert.equal(modeForInlineEditorGrowth({
    ...tail, growth: -20, scrollTopBefore: 1500, ...bottomViewport,
  }), null, 'the clamp already holds the field: no second move')

  // Part of the shrink was absorbed by the clamp (scrollTop 1490 -> 1480) and
  // the rest is still owed: the hold completes it to 1470, not 1460.
  const partial = bottomCardTranscript({
    scrollTop: 1480, scrollHeight: 2280, cardBottom: 730, editorBottom: 704,
  })
  const hold = modeForInlineEditorGrowth({
    ...partial, scrollEl: Object.assign(partial.scrollEl, { scrollHeight: 2270 }),
    growth: -20, scrollTopBefore: 1490, ...bottomViewport,
  })
  assert.equal(hold.offset, 2000 - 1470)
})

test('an answer card higher up grows down, and a held-away reader is not moved', () => {
  // Room below the card: the field grows into it with no scroll.
  const roomy = bottomCardTranscript({ scrollTop: 1500, cardBottom: 420, editorBottom: 394 })
  assert.equal(modeForInlineEditorGrowth({
    ...roomy, growth: 20, scrollTopBefore: 1500, ...bottomViewport,
  }), null)

  // The reader scrolled the field out of view under the composer: their
  // reading hold wins and the ordinary caret reveal decides instead.
  const hidden = bottomCardTranscript({ scrollTop: 1500, cardBottom: 900, editorBottom: 870 })
  assert.equal(modeForInlineEditorGrowth({
    ...hidden, growth: 20, scrollTopBefore: 1500, ...bottomViewport,
  }), null)

  // No size change (a width-only observation, the first observation) is no hold.
  const still = bottomCardTranscript({ scrollTop: 1500, cardBottom: 706, editorBottom: 680 })
  assert.equal(modeForInlineEditorGrowth({
    ...still, growth: 0, scrollTopBefore: 1500, ...bottomViewport,
  }), null)

  // A resize no edit drove has no pre-edit position to hold against.
  assert.equal(modeForInlineEditorGrowth({
    ...still, growth: 20, scrollTopBefore: null, ...bottomViewport,
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
