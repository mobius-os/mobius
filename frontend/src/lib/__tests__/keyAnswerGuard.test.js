// A key request card's answers count only after the card has been still and
// untouched for a moment, as a browser's permission prompt does: a card that
// appears or moves under the pointer (over an app, streamed or pushed into
// place in a chat, sliding up in Settings) must not take a click the owner
// aimed at what was there before, and steady clicking must not wear the wait
// down. The card's asleep first render is checked in
// ChatView/__tests__/keyRequestBlock.test.js.
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
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

test('a keyboard answer is held to the same wait', () => {
  assert.equal(keyAnswerClickCounts(keyAnswerShown(SHOWN), key(SHOWN + 300)), false)
  assert.equal(keyAnswerClickCounts(keyAnswerShown(SHOWN), key(SHOWN + 1000)), true)
  const pressed = keyAnswerPressed(keyAnswerShown(SHOWN), press(SHOWN + 3000, field))
  assert.equal(keyAnswerClickCounts(pressed, key(SHOWN + 3400)), false)
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

test('every card hands the guard its real events: presses caught on the card, clicks on each answer', () => {
  const card = readFileSync(new URL('../../components/SettingsView/KeyRequest.jsx', import.meta.url), 'utf8')
  assert.match(card, /onPointerDownCapture=\{guard\.onPressCapture\}/)
  assert.match(card, /state\.current = keyAnswerPressed\(state\.current, event\)/)
  assert.match(card, /counts: event => keyAnswerClickCounts\(state\.current, event\)/)
  assert.equal(card.match(/onClick=\{event => \{ if \(guard\.counts\(event\)\) answer\('(deny|approve)'\) \}\}/g)?.length, 2)
  // Asleep is aria-disabled, not disabled: the buttons keep hearing presses.
  assert.equal(card.match(/aria-disabled=\{guard\.active \? undefined : 'true'\}/g)?.length, 2)
  assert.doesNotMatch(card, /\sdisabled=\{[^}]*guard\.active/)
})
