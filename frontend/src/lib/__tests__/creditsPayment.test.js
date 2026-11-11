import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  WAITING_PAYMENTS_POLL_MS, answerRefusal, paymentAmount, paysWithCredits, pressCounts,
  waitingPaymentNotice, waitingPaymentsRefresh,
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

test('a credits payment shows the creator price and the app ceiling when higher', () => {
  const shown = paymentAmount({
    method: 'credits', price_micro: 400_000, price: '0.40', max_price_micro: 500_000, max_price: '0.50',
  })
  assert.deepEqual(shown, { figure: '0.40', ceiling: '0.50' })
  assert.equal(paymentAmount({
    method: 'credits', price_micro: 500_000, price: '0.50', max_price_micro: 500_000, max_price: '0.50',
  }).ceiling, null)
})

test('a free action shows its zero price, not the ceiling', () => {
  const shown = paymentAmount({
    method: 'credits', price_micro: 0, price: '0.00', max_price_micro: 500_000, max_price: '0.50',
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

test('the shell reads waiting payments on its events, not by polling', () => {
  const shell = waitingPaymentsRefresh({ pushed: true, visible: true })
  assert.equal(shell.refetchInterval, false)
  assert.equal(shell.refetchOnWindowFocus, true)
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
