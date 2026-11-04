/* Settings → Credits for a paired Möbius: the wallet apps spend from. */

import test from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import WalletSection from '../../components/SettingsView/WalletSection.jsx'
import { activityLabel, openStripeWindow, settleTopup } from '../creditsWallet.js'

const SUMMARY = {
  mode: 'customer',
  service_host: 'www.mobius.you',
  balance: { balance: '2.25', available: '2.25', state: 'healthy' },
}
const TERMS = { currency: 'usd', amounts_cents: [500, 1000], waiver_required: true, stripe_mode: 'test' }

const render = props => renderToStaticMarkup(createElement(WalletSection, {
  summary: SUMMARY, terms: TERMS, activity: [], ...props,
}))

test('the wallet balance is the headline and names where it is kept', () => {
  const html = render({})
  assert.match(html, /2\.25/)
  assert.match(html, /Kept by www\.mobius\.you/)
  assert.match(html, /\$5/)
})

test('a paid app run shows in recent activity with who was paid', () => {
  const html = render({ activity: [{
    id: 't1', at: '2026-11-03T15:25:58Z', kind: 'app_call', amount: '-0.50',
    description: 'Used ana/prompts/run-659eab7c',
  }] })
  assert.match(html, /Paid @ana in prompts/)
  assert.match(html, /-0\.50/)
})

test('without the service terms no amounts are offered', () => {
  assert.doesNotMatch(render({ terms: null }), /Buy /)
})

test('activity labels fall back to the service wording', () => {
  assert.equal(activityLabel({ kind: 'topup', description: 'Bought credits (test payment)' }), 'Bought credits')
  assert.equal(activityLabel({ kind: 'other', description: 'Moved' }), 'Moved')
})

test('settling stops at the first settled status and survives a failed read', async () => {
  const answers = [new Error('busy'), { status: 'pending' }, { status: 'paid' }, { status: 'never read' }]
  let reads = 0
  const status = await settleTopup('t', async () => {
    const answer = answers[reads++]
    if (answer instanceof Error) throw answer
    return answer
  }, { sleep: async () => {} })
  assert.equal(status, 'paid')
  assert.equal(reads, 3)
})

test('an unsettled purchase reports pending rather than failure', async () => {
  const status = await settleTopup('t', async () => ({ status: 'pending' }), { attempts: 2, sleep: async () => {} })
  assert.equal(status, 'pending')
})

/** A browser whose window.open returns `popup`, for one test. */
function withWindowOpen(t, popup) {
  const before = Object.getOwnPropertyDescriptor(globalThis, 'window')
  globalThis.window = { open: () => popup }
  t.after(() => {
    if (before) Object.defineProperty(globalThis, 'window', before)
    else delete globalThis.window
  })
}

test('the Stripe window cannot reach back into Möbius', t => {
  const popup = { opener: {}, document: { title: '', body: {} } }
  withWindowOpen(t, popup)
  assert.equal(openStripeWindow(), popup)
  assert.equal(popup.opener, null)
})

test('a blocked Stripe window is reported, not written into', t => {
  withWindowOpen(t, null)
  assert.equal(openStripeWindow(), null)
})
