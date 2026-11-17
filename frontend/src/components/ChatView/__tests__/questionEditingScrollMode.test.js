import test from 'node:test'
import assert from 'node:assert/strict'
import {
  modeForInlineEditorGrowth,
  modeForQuestionEditingViewportChange,
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
// was laid out (including any scroll the browser or an anchor repair applied
// during it); the scroller's own top is client 0.
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
  // just grown by one 20px line, so its lower edge moved from 660 to 680.
  const { scrollEl, editor } = bottomCardTranscript({ scrollTop: 1500, cardBottom: 706, editorBottom: 680 })
  const hold = modeForInlineEditorGrowth({
    scrollEl, editor, editorBottomBefore: 660, ...bottomViewport,
  })
  // The hold scrolls by exactly the growth: the field's lower edge, and the
  // Submit row under it, stay where they were while content above moves up.
  assert.deepEqual(hold, { kind: 'ANCHOR_AT', key: 'question-row', offset: 2000 - 1520 })

  // A phone keyboard can leave the card's Submit row under the composer while
  // the field itself is visible: still the bottom, still upward growth.
  const keyboard = bottomCardTranscript({ scrollTop: 1500, cardBottom: 790, editorBottom: 730 })
  assert.ok(modeForInlineEditorGrowth({
    ...keyboard, editorBottomBefore: 710, ...bottomViewport,
  }))
})

test('only the displacement still owed is held, whatever else moved the view', () => {
  // The native caret reveal scrolled 10px while the field grew 20px: the
  // edge sits 10px below where it was, so the hold adds only those 10px.
  const revealed = bottomCardTranscript({ scrollTop: 1510, cardBottom: 696, editorBottom: 670 })
  assert.equal(modeForInlineEditorGrowth({
    ...revealed, editorBottomBefore: 660, ...bottomViewport,
  }).offset, 2000 - 1520)

  // An anchor repair for content above moved scrollTop 1500 -> 1550 in the
  // same pass, keeping the field in place, and the field grew 20px. The hold
  // keeps the repair and adds the growth: 1570, not 1520 (a 50px jump).
  const repaired = bottomCardTranscript({ scrollTop: 1550, cardBottom: 706, editorBottom: 680 })
  assert.equal(modeForInlineEditorGrowth({
    ...repaired, editorBottomBefore: 660, ...bottomViewport,
  }).offset, 2000 - 1570)
})

test('a shrinking answer moves the conversation back down by its own height', () => {
  // With scroll range to spare, deleting a 20px line leaves scrollTop alone,
  // so the hold moves the conversation down by the line.
  const spare = bottomCardTranscript({ scrollTop: 1000, cardBottom: 720, editorBottom: 694 })
  const hold = modeForInlineEditorGrowth({
    ...spare, editorBottomBefore: 714, ...bottomViewport,
  })
  assert.equal(hold.offset, 2000 - 980)
})

test('a deletion at the physical tail keeps the clamp the browser already applied', () => {
  // scrollTop sat at its maximum 1500; the content shrank by 20px and the
  // browser clamped scrollTop to the new maximum 1480 during layout, which
  // already kept the field's edge at 714. Nothing is owed.
  const tail = bottomCardTranscript({
    scrollTop: 1480, scrollHeight: 2280, cardBottom: 740, editorBottom: 714,
  })
  assert.equal(modeForInlineEditorGrowth({
    ...tail, editorBottomBefore: 714, ...bottomViewport,
  }), null, 'the clamp already holds the field: no second move')

  // Part of the shrink was absorbed by the clamp (scrollTop 1490 -> 1480) and
  // the rest is still owed: the hold completes it to 1470, not 1460.
  const partial = bottomCardTranscript({
    scrollTop: 1480, scrollHeight: 2280, cardBottom: 730, editorBottom: 704,
  })
  const hold = modeForInlineEditorGrowth({
    ...partial, editorBottomBefore: 714, ...bottomViewport,
  })
  assert.equal(hold.offset, 2000 - 1470)
})

test('an answer card higher up grows down, and a held-away reader is not moved', () => {
  // Room below the card: the field grows into it with no scroll.
  const roomy = bottomCardTranscript({ scrollTop: 1500, cardBottom: 420, editorBottom: 394 })
  assert.equal(modeForInlineEditorGrowth({
    ...roomy, editorBottomBefore: 374, ...bottomViewport,
  }), null)

  // The reader scrolled the field out of view under the composer: their
  // reading hold wins and the ordinary caret reveal decides instead.
  const hidden = bottomCardTranscript({ scrollTop: 1500, cardBottom: 900, editorBottom: 870 })
  assert.equal(modeForInlineEditorGrowth({
    ...hidden, editorBottomBefore: 850, ...bottomViewport,
  }), null)

  // An edge that did not move (a width-only observation) is no hold.
  const still = bottomCardTranscript({ scrollTop: 1500, cardBottom: 706, editorBottom: 680 })
  assert.equal(modeForInlineEditorGrowth({
    ...still, editorBottomBefore: 680, ...bottomViewport,
  }), null)

  // A resize no edit drove has no pre-edit edge to hold.
  assert.equal(modeForInlineEditorGrowth({
    ...still, editorBottomBefore: null, ...bottomViewport,
  }), null)
})

test('a card taller than the view grows down when answered from its top', () => {
  // A grouped card: the field being typed in sits near the top of the view
  // and the card's lower edge is far below the scroller. Its bottom is not at
  // the chat bottom, so the field grows down and keeps the caret reveal.
  const tall = bottomCardTranscript({ scrollTop: 1500, cardBottom: 1800, editorBottom: 120 })
  assert.equal(modeForInlineEditorGrowth({
    ...tall, editorBottomBefore: 100, ...bottomViewport,
  }), null)
})
