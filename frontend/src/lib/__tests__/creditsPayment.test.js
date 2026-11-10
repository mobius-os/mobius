import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  answerRefusal, paymentAmount, paysWithCredits, pressCounts,
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
  assert.deepEqual(shown, { figure: '0.40', unit: 'credits', ceiling: '0.50' })
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

test('a card payment is shown in the currency the service sells in, not always dollars', () => {
  const base = { method: 'card', price_micro: 400_000, max_price_micro: 400_000 }
  assert.equal(paymentAmount({ ...base, currency: 'gbp' }).figure, '£0.40')
  assert.equal(paymentAmount({ ...base, currency: 'eur' }).figure, '€0.40')
  assert.equal(paymentAmount({ ...base, currency: 'usd' }).figure, '$0.40')
  assert.equal(paymentAmount({ ...base, currency: 'gbp' }).unit, 'by card')
})

test('a refused payment tells the owner the server reason', () => {
  assert.equal(answerRefusal({ detail: { code: 'card_minimum', message: 'Card payments start at £0.50.' } }),
    'Card payments start at £0.50.')
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
