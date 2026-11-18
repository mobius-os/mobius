// A key request card's answers count only after the card has been still and
// untouched for a moment, as a browser's permission prompt does: a card that
// appears or moves under the pointer (over an app, streamed or pushed into
// place in a chat, sliding up in Settings) must not take a click the owner
// aimed at what was there before, and steady clicking must not wear the wait
// down. The card's handlers are driven in
// SettingsView/__tests__/useKeyAnswerGuard.hooktest.js, and its asleep first
// render is checked in Shell/__tests__/appKeyRequestCard.test.js.
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createSsrRenderServer } from './ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
const {
  KEY_ANSWER_ARM_MS, keyAnswerShown, keyAnswerActiveAt, keyAnswerPressed, keyAnswerMoved, keyAnswerClickCounts,
  keyAnswerPlace, keyAnswerPlaceChanged,
} = await vite.ssrLoadModule('/src/lib/keyAnswerGuard.js')
after(() => vite.close())

const SHOWN = 5000
const allow = { name: 'Allow', contains: node => node === allow }
const field = { name: 'paste field', contains: node => node === field }
// Event-shaped stand-ins: the guard reads the event's own time, never the clock.
const press = (at, target = allow) => ({ timeStamp: at, target })
const tap = at => ({ timeStamp: at, detail: 1, currentTarget: allow })
const key = at => ({ timeStamp: at, detail: 0, currentTarget: allow })

const pressAndTap = (state, at) => {
  const pressed = keyAnswerPressed(state, press(at))
  return { state: pressed, counts: keyAnswerClickCounts(pressed, tap(at + 80)) }
}

test('the answers sleep for about a second after the card appears', () => {
  assert.equal(KEY_ANSWER_ARM_MS, 1000)
  assert.equal(keyAnswerActiveAt(keyAnswerShown(SHOWN)), SHOWN + 1000)
  assert.equal(pressAndTap(keyAnswerShown(SHOWN), SHOWN + 400).counts, false)
  assert.equal(pressAndTap(keyAnswerShown(SHOWN), SHOWN + 1000).counts, true)
})

test('steady clicking never answers: every press on the card starts the wait again', () => {
  let state = keyAnswerShown(SHOWN)
  for (let at = SHOWN + 300; at < SHOWN + 10000; at += 700) {
    const result = pressAndTap(state, at)
    assert.equal(result.counts, false, `press at +${at - SHOWN}ms`)
    state = result.state
  }
  assert.equal(keyAnswerActiveAt(state), SHOWN + 9400 + 1000)
})

test('a press anywhere on the card, such as in the paste field, also starts the wait again', () => {
  const pressed = keyAnswerPressed(keyAnswerShown(SHOWN), press(SHOWN + 3000, field))
  assert.equal(keyAnswerActiveAt(pressed), SHOWN + 4000)
  assert.equal(pressAndTap(pressed, SHOWN + 3500).counts, false)
  assert.equal(pressAndTap(pressed, SHOWN + 4000).counts, true)
})

test('a click whose press began before the card appeared, or on something else, never answers', () => {
  const quiet = keyAnswerShown(SHOWN)
  // No press seen on the card: it began elsewhere, before the card.
  assert.equal(keyAnswerClickCounts(quiet, tap(SHOWN + 9000)), false)
  // Pressed in the field, released on Allow.
  assert.equal(keyAnswerClickCounts(keyAnswerPressed(quiet, press(SHOWN + 5000, field)), tap(SHOWN + 5100)), false)
})

test('a card that moved under the pointer waits again, and a press already down no longer answers', () => {
  const moved = keyAnswerMoved(keyAnswerShown(SHOWN), SHOWN + 6000)
  assert.equal(keyAnswerActiveAt(moved), SHOWN + 7000)
  assert.equal(pressAndTap(moved, SHOWN + 6500).counts, false)
  const down = keyAnswerPressed(keyAnswerShown(SHOWN), press(SHOWN + 3000))
  assert.equal(keyAnswerClickCounts(keyAnswerMoved(down, SHOWN + 3050), tap(SHOWN + 3100)), false)
  // An earlier move never shortens the wait.
  assert.equal(keyAnswerActiveAt(keyAnswerMoved(moved, SHOWN + 100)), SHOWN + 7000)
})

test('a keyboard answer needs an awake key press on that answer, so Tab then Enter in a hurry never answers', () => {
  // Each key down on the card is a press: Tab to reach Allow, then Enter on it.
  const tabbed = keyAnswerPressed(keyAnswerShown(SHOWN), press(SHOWN + 2000, field))
  const entered = keyAnswerPressed(tabbed, press(SHOWN + 2300))
  assert.equal(keyAnswerClickCounts(entered, key(SHOWN + 2300)), false)
  // Enter after a quiet second answers.
  const later = keyAnswerPressed(entered, press(SHOWN + 3400))
  assert.equal(keyAnswerClickCounts(later, key(SHOWN + 3400)), true)
  // A key pressed on something else never answers Allow.
  assert.equal(keyAnswerClickCounts(keyAnswerPressed(keyAnswerShown(SHOWN), press(SHOWN + 5000, field)), key(SHOWN + 5000)), false)
})

test('assistive technology, which clicks with no press, is held to the wait alone', () => {
  assert.equal(keyAnswerClickCounts(keyAnswerShown(SHOWN), key(SHOWN + 300)), false)
  assert.equal(keyAnswerClickCounts(keyAnswerShown(SHOWN), key(SHOWN + 1000)), true)
  assert.equal(keyAnswerClickCounts(keyAnswerMoved(keyAnswerShown(SHOWN), SHOWN + 3000), key(SHOWN + 3400)), false)
})

test("a card's place changes when it moves or resizes; the first sighting is not a move", () => {
  const here = keyAnswerPlace({ top: 100, left: 20, width: 300, height: 180, bottom: 280, right: 320 })
  assert.deepEqual(here, { top: 100, left: 20, width: 300, height: 180 })
  assert.equal(keyAnswerPlaceChanged(null, here), false)
  assert.equal(keyAnswerPlaceChanged(here, { ...here }), false)
  assert.equal(keyAnswerPlaceChanged(here, { ...here, top: 60 }), true)
  assert.equal(keyAnswerPlaceChanged(here, { ...here, height: 200 }), true)
})
