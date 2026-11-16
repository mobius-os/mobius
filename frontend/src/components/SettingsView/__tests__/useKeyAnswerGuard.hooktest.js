// The guard every key request card wears (useKeyAnswerGuard.js), driven
// through the handlers the card hands its elements: presses caught on the
// card, clicks checked on each answer. The rules themselves are in
// lib/__tests__/keyAnswerGuard.test.js; the card's asleep first render in
// ChatView/__tests__/keyRequestBlock.test.js.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../ChatView/hooks/__tests__/react-hook-shim.mjs'
import useKeyAnswerGuard, { earlyAnswerHint } from '../useKeyAnswerGuard.js'

const answer = { pressed: {}, textContent: ' Not now ' }
answer.contains = target => target === answer.pressed
const press = at => ({ timeStamp: at, target: answer.pressed })
const click = at => ({ timeStamp: at, detail: 1, currentTarget: answer })
const keyClick = at => ({ timeStamp: at, detail: 0, currentTarget: answer })

function guardShownAt() {
  const shown = performance.now()
  const hook = renderHook(() => useKeyAnswerGuard({ enabled: true }))
  return { hook, shown, guard: () => hook.result.current }
}

test('a card starts asleep, and a press that began while asleep never answers however late the click', () => {
  const { hook, shown, guard } = guardShownAt()
  assert.equal(guard().active, false)
  guard().onPressCapture(press(shown + 10))
  assert.equal(guard().counts(click(shown + 60_000)), false)
  hook.unmount()
})

test('a press after a quiet second answers; a second press within the second does not', () => {
  const { hook, shown, guard } = guardShownAt()
  const awake = shown + 1_100
  guard().onPressCapture(press(awake))
  assert.equal(guard().counts(click(awake + 50)), true)
  // Steady clicking never wears the wait down: each press starts it again.
  guard().onPressCapture(press(awake + 400))
  assert.equal(guard().counts(click(awake + 450)), false)
  hook.unmount()
})

test('a keyboard answer counts only once the card has been quiet for a second', () => {
  const { hook, shown, guard } = guardShownAt()
  guard().onPressCapture(press(shown + 500))
  assert.equal(guard().counts(keyClick(shown + 1_400)), false)
  assert.equal(guard().counts(keyClick(shown + 1_500)), true)
  hook.unmount()
})

test('an ignored click says why and which button to tap again; a counted one says nothing', () => {
  const { hook, shown, guard } = guardShownAt()
  guard().onPressCapture(press(shown + 10))
  assert.equal(guard().counts(click(shown + 20)), false)
  assert.equal(guard().early, 'Not now')
  assert.equal(earlyAnswerHint(guard().early), 'Wait a second, then tap “Not now” again.')
  hook.unmount()
  const again = guardShownAt()
  again.guard().onPressCapture(press(again.shown + 1_100))
  assert.equal(again.guard().counts(click(again.shown + 1_150)), true)
  assert.equal(again.guard().early, null)
  again.hook.unmount()
})
