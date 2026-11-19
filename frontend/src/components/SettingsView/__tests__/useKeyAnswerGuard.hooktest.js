// The guard every key request card wears (useKeyAnswerGuard.js), driven
// through the handlers the card hands its elements: presses and keys caught
// on the card, clicks checked on each answer, and the page telling it the
// card may have been out of sight. The rules themselves are in
// lib/__tests__/keyAnswerGuard.test.js; the card's asleep first render in
// Shell/__tests__/appKeyRequestCard.test.js.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../ChatView/hooks/__tests__/react-hook-shim.mjs'
import useKeyAnswerGuard, { earlyAnswerHint } from '../useKeyAnswerGuard.js'

// The page the card lives on: the guard listens to it for the card being
// revealed, and reads its clock when that happens.
globalThis.document = Object.assign(new EventTarget(), { visibilityState: 'visible' })
globalThis.window = new EventTarget()
let now = 1_000
performance.now = () => now

const answer = { pressed: {}, textContent: ' Not now ' }
answer.contains = target => target === answer.pressed
const press = at => ({ timeStamp: at, target: answer.pressed })
const keyDown = (at, extra = {}) => ({ timeStamp: at, target: answer.pressed, key: 'Enter', ...extra })
const click = at => ({ timeStamp: at, detail: 1, currentTarget: answer })
const keyClick = at => ({ timeStamp: at, detail: 0, currentTarget: answer })

function guardShownAt() {
  now += 100_000
  const hook = renderHook(() => useKeyAnswerGuard({ enabled: true }))
  return { hook, shown: now, guard: () => hook.result.current }
}

test('a card starts asleep, and a press that began while asleep never answers however late the click', () => {
  const { hook, shown, guard } = guardShownAt()
  assert.equal(guard().active, false)
  guard().cardProps.onPointerDownCapture(press(shown + 10))
  assert.equal(guard().counts(click(shown + 60_000)), false)
  hook.unmount()
})

test('a press after a quiet second answers; a second press within the second does not', () => {
  const { hook, shown, guard } = guardShownAt()
  const awake = shown + 1_100
  guard().cardProps.onPointerDownCapture(press(awake))
  assert.equal(guard().counts(click(awake + 50)), true)
  // Steady clicking never wears the wait down: each press starts it again.
  guard().cardProps.onPointerDownCapture(press(awake + 400))
  assert.equal(guard().counts(click(awake + 450)), false)
  hook.unmount()
})

test('a key pressed on the card is a press: keys in a hurry never answer, Enter after a quiet second does', () => {
  const { hook, shown, guard } = guardShownAt()
  // Tab, Tab, Enter: each key down starts the wait again.
  guard().cardProps.onKeyDownCapture(keyDown(shown + 1_500, { key: 'Tab' }))
  guard().cardProps.onKeyDownCapture(keyDown(shown + 1_700, { key: 'Tab' }))
  guard().cardProps.onKeyDownCapture(keyDown(shown + 1_900))
  assert.equal(guard().counts(keyClick(shown + 1_900)), false)
  guard().cardProps.onKeyDownCapture(keyDown(shown + 3_000))
  assert.equal(guard().counts(keyClick(shown + 3_000)), true)
  hook.unmount()
})

test("a held key's repeats are not new presses", () => {
  const { hook, shown, guard } = guardShownAt()
  guard().cardProps.onKeyDownCapture(keyDown(shown + 1_500))
  guard().cardProps.onKeyDownCapture(keyDown(shown + 1_550, { repeat: true }))
  assert.equal(guard().counts(keyClick(shown + 1_550)), true)
  hook.unmount()
})

test('a card brought back into view waits again, so the first tap after returning to the tab is refused', () => {
  const { hook, shown, guard } = guardShownAt()
  document.visibilityState = 'hidden'
  document.dispatchEvent(new Event('visibilitychange'))
  // Hidden long enough that the card would otherwise be awake.
  now = shown + 30_000
  document.visibilityState = 'visible'
  document.dispatchEvent(new Event('visibilitychange'))
  guard().cardProps.onPointerDownCapture(press(now + 30))
  assert.equal(guard().counts(click(now + 85)), false)
  // A second later the card answers as usual.
  guard().cardProps.onPointerDownCapture(press(now + 1_200))
  assert.equal(guard().counts(click(now + 1_250)), true)
  hook.unmount()
})

test('entering or leaving fullscreen, or a page restored from the back-forward cache, also wait again', () => {
  for (const [target, type] of [[document, 'fullscreenchange'], [window, 'pageshow']]) {
    const { hook, shown, guard } = guardShownAt()
    now = shown + 30_000
    target.dispatchEvent(new Event(type))
    guard().cardProps.onPointerDownCapture(press(now + 30))
    assert.equal(guard().counts(click(now + 85)), false, type)
    hook.unmount()
  }
})

test('a card gone from the page stops listening to it', () => {
  const { hook, shown, guard } = guardShownAt()
  hook.unmount()
  now = shown + 30_000
  document.dispatchEvent(new Event('fullscreenchange'))
  guard().cardProps.onPointerDownCapture(press(now + 30))
  // Its wait was not restarted by a page it no longer shows on.
  assert.equal(guard().counts(click(now + 85)), true)
})

test('an ignored click says why and which button to tap again; a counted one says nothing', () => {
  const { hook, shown, guard } = guardShownAt()
  guard().cardProps.onPointerDownCapture(press(shown + 10))
  assert.equal(guard().counts(click(shown + 20)), false)
  assert.equal(guard().early, 'Not now')
  assert.equal(earlyAnswerHint(guard().early), 'Wait a second, then tap “Not now” again.')
  hook.unmount()
  const again = guardShownAt()
  again.guard().cardProps.onPointerDownCapture(press(again.shown + 1_100))
  assert.equal(again.guard().counts(click(again.shown + 1_150)), true)
  assert.equal(again.guard().early, null)
  again.hook.unmount()
})
