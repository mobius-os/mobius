// A key request card's answers wait a moment before they count, as a
// browser's permission prompt does: a card that appears under the pointer
// (over an app, streamed into a chat) must not take a click the owner aimed
// at what was there before. The card's disabled first render is checked in
// ChatView/__tests__/keyRequestBlock.test.js.
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createSsrRenderServer } from './ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
const { KEY_ANSWER_ARM_MS, keyAnswerWaitMs, keyAnswerClickCounts } = await vite.ssrLoadModule('/src/lib/keyAnswerGuard.js')
after(() => vite.close())

const SHOWN = 5000
const ACTIVE = SHOWN + KEY_ANSWER_ARM_MS

test('the answers stay inactive for about a second after the card appears', () => {
  assert.equal(KEY_ANSWER_ARM_MS, 1000)
  assert.equal(keyAnswerWaitMs(SHOWN, SHOWN), 1000)
  assert.equal(keyAnswerWaitMs(SHOWN, SHOWN + 400), 600)
  assert.equal(keyAnswerWaitMs(SHOWN, ACTIVE + 1), 0)
})

test('a click whose press began before the card appeared never answers, however long it was held', () => {
  assert.equal(keyAnswerClickCounts({ shownAt: SHOWN, clickAt: ACTIVE + 5000, pressAt: SHOWN - 10, byPointer: true }), false)
  // No press seen on the answer itself: it began elsewhere, before the card.
  assert.equal(keyAnswerClickCounts({ shownAt: SHOWN, clickAt: ACTIVE + 5000, pressAt: null, byPointer: true }), false)
})

test('a press that began while the answers were still inactive does not count either', () => {
  assert.equal(keyAnswerClickCounts({ shownAt: SHOWN, clickAt: ACTIVE + 200, pressAt: SHOWN + 900, byPointer: true }), false)
})

test('a deliberate press and release after the wait answers', () => {
  assert.equal(keyAnswerClickCounts({ shownAt: SHOWN, clickAt: ACTIVE + 120, pressAt: ACTIVE + 50, byPointer: true }), true)
})

test('a keyboard answer counts once the card is active, not before', () => {
  assert.equal(keyAnswerClickCounts({ shownAt: SHOWN, clickAt: SHOWN + 300, byPointer: false }), false)
  assert.equal(keyAnswerClickCounts({ shownAt: SHOWN, clickAt: ACTIVE, byPointer: false }), true)
})
