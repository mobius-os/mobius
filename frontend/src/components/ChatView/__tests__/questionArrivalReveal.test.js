/* Contract R5c: a newly pending owner decision comes into view once, unless it
 * is already readable above the composer or live follow is carrying it. */

import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  QUESTION_ARRIVAL_MIN_VISIBLE_PX,
  questionArrivalNeedsReveal,
} from '../scroll/policy.js'

// A 900px scroll box whose bottom 140px is covered by the overlaid composer.
const USABLE = { visibleTop: 0, visibleBottom: 760 }
const HOLD = { kind: 'ANCHOR_AT', key: 'm-12', offset: -40 }

test('a decision far below a held reading position needs its reveal', () => {
  assert.equal(questionArrivalNeedsReveal({
    mode: HOLD, cardTop: 2400, cardBottom: 2640, ...USABLE,
  }), true)
})

test('a decision hidden behind the overlaid composer is not treated as seen', () => {
  // Inside the scroll box (the offscreen nudge would stay hidden) but wholly
  // under the composer, where the reader cannot see it.
  assert.equal(questionArrivalNeedsReveal({
    mode: HOLD, cardTop: 780, cardBottom: 1020, ...USABLE,
  }), true)
})

test('a sliver peeking above the composer still gets the reveal', () => {
  assert.equal(questionArrivalNeedsReveal({
    mode: HOLD,
    cardTop: USABLE.visibleBottom - (QUESTION_ARRIVAL_MIN_VISIBLE_PX - 1),
    cardBottom: 1000,
    ...USABLE,
  }), true)
})

test('a readable decision is left exactly where the reader already sees it', () => {
  assert.equal(questionArrivalNeedsReveal({
    mode: HOLD,
    cardTop: USABLE.visibleBottom - QUESTION_ARRIVAL_MIN_VISIBLE_PX,
    cardBottom: 1000,
    ...USABLE,
  }), false)
  // The reader scrolled so only the card's lower part (its options) shows.
  assert.equal(questionArrivalNeedsReveal({
    mode: HOLD, cardTop: -300, cardBottom: 200, ...USABLE,
  }), false)
})

test('a short decision card counts as readable once it is fully shown', () => {
  assert.equal(questionArrivalNeedsReveal({
    mode: HOLD, cardTop: 700, cardBottom: 730, ...USABLE,
  }), false)
})

test('live follow, an armed send pin, and a follow-based answer never move for a new decision', () => {
  const offscreen = { cardTop: 2400, cardBottom: 2640, ...USABLE }
  assert.equal(questionArrivalNeedsReveal({
    mode: { kind: 'FOLLOW_BOTTOM' }, ...offscreen,
  }), false)
  assert.equal(questionArrivalNeedsReveal({
    mode: { kind: 'PIN_USER_MSG', cid: 'c-1', followWhenFilled: true }, ...offscreen,
  }), false)
  assert.equal(questionArrivalNeedsReveal({
    mode: {
      ...HOLD,
      questionSubmitBaseMode: { kind: 'FOLLOW_BOTTOM' },
    },
    ...offscreen,
  }), false)
})

test('held positions that follow nothing reveal an offscreen decision', () => {
  const offscreen = { cardTop: 2400, cardBottom: 2640, ...USABLE }
  // A settled pin no longer hands off to follow, so later output can bury it.
  assert.equal(questionArrivalNeedsReveal({
    mode: { kind: 'PIN_USER_MSG', cid: 'c-1' }, ...offscreen,
  }), true)
  // An answer submitted from a reading hold stays a hold.
  assert.equal(questionArrivalNeedsReveal({
    mode: { ...HOLD, questionSubmitBaseMode: HOLD }, ...offscreen,
  }), true)
})

test('an unrestored mount lands on the waiting decision', () => {
  assert.equal(questionArrivalNeedsReveal({
    mode: { kind: 'INITIAL' }, cardTop: 100, cardBottom: 300, ...USABLE,
  }), true)
})

test('unreadable geometry reveals rather than silently hiding the decision', () => {
  assert.equal(questionArrivalNeedsReveal({
    mode: HOLD, cardTop: Number.NaN, cardBottom: 300, ...USABLE,
  }), true)
})
