import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  PRESS_ARM_MS, WAITING_PAYMENTS_POLL_MS, WAITING_PAYMENTS_SAFETY_POLL_MS, answerRefusal,
  answerRefusalSheet, armPay, armingWait, paymentAmount, paysWithCredits, pressCounts,
  restartArming, startArming, waitingPaymentNotice, waitingPaymentsRefresh, withoutPayment,
} from '../creditsPayment.js'

test('Pay ignores a press while the sheet is still arming', () => {
  assert.equal(pressCounts({ armedAt: null, pressStartedAt: 5, keyboard: false }), false)
  assert.equal(pressCounts({ armedAt: null, pressStartedAt: null, keyboard: true }), false)
})

test('a tap already down when the sheet appeared never pays, even lifted after it armed', () => {
  // The finger went down at 100 (aimed at the app), the button armed at 800,
  // the finger lifted at 900: the click lands on an armed Pay but is not counted.
  assert.equal(pressCounts({ armedAt: 800, pressStartedAt: 100, keyboard: false }), false)
  assert.equal(pressCounts({ armedAt: 800, pressStartedAt: null, keyboard: false }), false)
})

test('a fresh press that starts on an armed Pay counts', () => {
  assert.equal(pressCounts({ armedAt: 800, pressStartedAt: 800, keyboard: false }), true)
  assert.equal(pressCounts({ armedAt: 800, pressStartedAt: 1200, keyboard: false }), true)
})

test('a keyboard press counts once Pay has armed', () => {
  assert.equal(pressCounts({ armedAt: 800, pressStartedAt: null, keyboard: true }), true)
})

// Plays taps on the sheet as the dialog sees them: the arming timer has fired
// by each tap's time, the tap restarts a wait still running, and its press
// counts as the click lands. Returns how many taps paid.
function tapsThatPay(shownAt, tapTimes) {
  let arming = startArming(shownAt)
  let paid = 0
  for (const at of tapTimes) {
    arming = armPay(arming, at)
    arming = restartArming(arming, at)
    if (pressCounts({ armedAt: arming.armedAt, pressStartedAt: at, keyboard: false })) paid += 1
  }
  return paid
}

test('continuous tapping through the sheet never arms Pay', () => {
  // An app's rapid-tap screen: the owner taps every 150 ms for ten seconds,
  // well past the arming pause, and every tap lands where Pay sits.
  const taps = Array.from({ length: 67 }, (_, i) => 50 + i * 150)
  assert.equal(tapsThatPay(0, taps), 0)
  // A tap only just inside the pause still restarts it.
  assert.equal(tapsThatPay(0, [PRESS_ARM_MS - 1, 2 * PRESS_ARM_MS - 2]), 0)
})

test('one deliberate tap after a quiet pause pays', () => {
  const taps = Array.from({ length: 20 }, (_, i) => 50 + i * 150)
  const lastTap = taps.at(-1)
  assert.equal(tapsThatPay(0, [...taps, lastTap + PRESS_ARM_MS]), 1)
  assert.equal(tapsThatPay(0, [PRESS_ARM_MS]), 1)
})

test('Pay waits out the rest of its quiet pause, and stays armed once armed', () => {
  const arming = restartArming(startArming(0), 300)
  assert.equal(armingWait(arming, 400), PRESS_ARM_MS - 100)
  assert.equal(armPay(arming, 999), arming)
  const armed = armPay(arming, 300 + PRESS_ARM_MS)
  assert.equal(armed.armedAt, 300 + PRESS_ARM_MS)
  assert.equal(armingWait(armed, 0), 0)
  // The deliberate press itself must not turn Pay off again.
  assert.equal(restartArming(armed, 5000), armed)
})

test('a credits payment shows the creator price and the app ceiling when higher', () => {
  const shown = paymentAmount({
    price_micro: 400_000, price: '0.40', max_price_micro: 500_000, max_price: '0.50',
  })
  assert.deepEqual(shown, { figure: '0.40', ceiling: '0.50' })
  assert.equal(paymentAmount({
    price_micro: 500_000, price: '0.50', max_price_micro: 500_000, max_price: '0.50',
  }).ceiling, null)
})

test('a free action shows its zero price, not the ceiling', () => {
  const shown = paymentAmount({
    price_micro: 0, price: '0.00', max_price_micro: 500_000, max_price: '0.50',
  })
  assert.equal(shown.figure, '0.00')
  assert.equal(shown.ceiling, '0.50')
})

test('a refused payment tells the owner the server reason', () => {
  assert.equal(answerRefusal({ detail: { code: 'payment_gone', message: 'That payment is no longer waiting.' } }),
    'That payment is no longer waiting.')
  assert.equal(answerRefusal({ detail: 'Not allowed.' }), 'Not allowed.')
  assert.equal(answerRefusal({}), 'That could not be saved. Try again.')
  assert.equal(answerRefusal(null), 'That could not be saved. Try again.')
})

test('a payment answered already says so, never that it failed', () => {
  // A double tap, or a second device: the payment is still charged, so the
  // sheet must not send the owner to buy it again.
  const paid = answerRefusalSheet({ detail: {
    code: 'payment_already_answered', state: 'confirmed', message: 'You already paid for this.',
  } })
  assert.deepEqual(paid, { title: 'Already paid', message: 'You already paid for this.', failed: false })
  const cancelled = answerRefusalSheet({ detail: {
    code: 'payment_already_answered', state: 'declined', message: 'You already cancelled this payment.',
  } })
  assert.equal(cancelled.title, 'Already cancelled')
  assert.equal(cancelled.failed, false)
})

test('a lapsed or unknown payment did not go through', () => {
  const gone = answerRefusalSheet({ detail: {
    code: 'payment_gone', message: 'That payment is no longer waiting. Try it again in the app.',
  } })
  assert.deepEqual(gone, {
    title: 'This payment didn’t go through',
    message: 'That payment is no longer waiting. Try it again in the app.',
    failed: true,
  })
  assert.equal(answerRefusalSheet(null).failed, true)
})

test('an answered payment leaves the waiting list at once', () => {
  const data = { payments: [{ id: 'a1' }, { id: 'b2' }] }
  assert.deepEqual(withoutPayment(data, 'a1'), { payments: [{ id: 'b2' }] })
  assert.equal(withoutPayment(data, 'zz'), data)
  assert.equal(withoutPayment(undefined, 'a1'), undefined)
  assert.deepEqual(withoutPayment({ payments: [{ id: 'a1' }] }, 'a1'), { payments: [] })
})

test('only an app whose contract may spend credits reads its waiting payments', () => {
  assert.equal(paysWithCredits(null), false)
  assert.equal(paysWithCredits({ data: {} }), false)
  assert.equal(paysWithCredits({ data: { spend_credits: [] } }), false)
  // Managing credits raises no payment, and a local app never manages them.
  assert.equal(paysWithCredits({ data: { credits_manage: true } }), false)
  assert.equal(paysWithCredits({ data: { spend_credits: ['*/prompts'] } }), true)
})

test('an app on its own page polls its waiting payments while on screen', () => {
  // No shell stream tells this page a charge arrived, so it must ask.
  const shown = waitingPaymentsRefresh({ pushed: false, visible: true })
  assert.equal(shown.refetchInterval, WAITING_PAYMENTS_POLL_MS)
  assert.equal(shown.staleTime, 0)
  assert.equal(shown.refetchOnWindowFocus, true)
  assert.equal(waitingPaymentsRefresh({ pushed: false, visible: false }).refetchInterval, false)
})

test('the shell reads waiting payments on its events, with a slow safety read', () => {
  // Each payment is announced once: one lost while the stream was down must
  // still bring up its Pay sheet while the app is on screen.
  const shell = waitingPaymentsRefresh({ pushed: true, visible: true })
  assert.equal(shell.refetchInterval, WAITING_PAYMENTS_SAFETY_POLL_MS)
  assert.ok(WAITING_PAYMENTS_SAFETY_POLL_MS > WAITING_PAYMENTS_POLL_MS)
  assert.equal(shell.refetchOnWindowFocus, true)
  assert.equal(waitingPaymentsRefresh({ pushed: true, visible: false }).refetchInterval, false)
})

test('a payment from an app off screen raises a shell notice naming it', () => {
  const apps = [{ id: 7, name: 'Market' }, { id: 8, name: 'Notes' }]
  assert.deepEqual(waitingPaymentNotice({ appId: '7', visibleAppIds: ['8'], apps }), {
    message: 'Market is asking you to confirm a payment. Open it to Pay or Cancel.',
    noticeKey: 'credits-payment:7',
  })
  assert.equal(waitingPaymentNotice({ appId: '9', visibleAppIds: new Set(), apps }).message,
    'An app is asking you to confirm a payment. Open it to Pay or Cancel.')
})

test('a payment from an app on screen raises no notice: its Pay sheet shows', () => {
  const apps = [{ id: 7, name: 'Market' }]
  assert.equal(waitingPaymentNotice({ appId: '7', visibleAppIds: new Set([7]), apps }), null)
  assert.equal(waitingPaymentNotice({ appId: null, visibleAppIds: [], apps }), null)
})
