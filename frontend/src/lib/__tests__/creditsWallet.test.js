/* Settings → Credits for a paired Möbius: the wallet apps spend from. */

import test from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import WalletSection, { WalletConnect } from '../../components/SettingsView/WalletSection.jsx'
import { activityLabel, checkoutPage, creditsSection, openStripeWindow, settleTopup, termsFor } from '../creditsWallet.js'

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

test('settings offers one-tap connect only to an unpaired Möbius launched from mobius.you', () => {
  assert.equal(creditsSection(SUMMARY), 'wallet')
  assert.equal(creditsSection({ ...SUMMARY, can_pair_with_account: true }), 'wallet')
  assert.equal(creditsSection({ mode: 'unpaired', can_pair_with_account: true }), 'connect')
  assert.equal(creditsSection({ mode: 'unpaired', can_pair_with_account: false }), null)
  // A server from before one-tap pairing does not say, so it is not offered.
  assert.equal(creditsSection({ mode: 'unpaired' }), null)
  assert.equal(creditsSection(null), null)
})

test('the connect card says why a connection was refused and holds while connecting', () => {
  const idle = renderToStaticMarkup(createElement(WalletConnect, {}))
  assert.match(idle, />Connect credits</)
  assert.doesNotMatch(idle, /role="alert"/)
  const busy = renderToStaticMarkup(createElement(WalletConnect, {
    busy: true, refusal: 'mobius.you could not connect your credits just now.',
  }))
  assert.match(busy, /Connecting…/)
  assert.match(busy, /disabled=""/)
  assert.match(busy, /role="alert">mobius\.you could not connect your credits just now\.</)
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

test('only an https Stripe Checkout address is ever opened', () => {
  const stripe = 'https://checkout.stripe.com/c/pay/cs_test_a1#fid=x'
  assert.equal(checkoutPage(stripe), stripe)
  for (const address of [
    'javascript:alert(1)', 'data:text/html,x', 'http://checkout.stripe.com/c', '', null, undefined,
    'https://checkout.stripe.com.evil.test/c', 'https://evil.test/checkout.stripe.com',
    'https://connect.stripe.com/c', 'https://user@checkout.stripe.com/c',
    'https://checkout.stripe.com:8443/c',
  ]) {
    assert.equal(checkoutPage(address), null, String(address))
  }
})

test('purchase terms show only under the pairing they were read with', () => {
  const terms = { ...TERMS, pairing: 'p-old' }
  assert.equal(termsFor(terms, { mode: 'customer', pairing: 'p-old' }), terms)
  // Paired again: the old service's currency, amounts and waiver are gone.
  assert.equal(termsFor(terms, { mode: 'customer', pairing: 'p-new' }), null)
  assert.equal(termsFor(terms, { mode: 'unpaired', pairing: null }), null)
  assert.equal(termsFor({ ...TERMS }, { mode: 'customer', pairing: undefined }), null)
  assert.equal(termsFor(null, { mode: 'customer', pairing: 'p-old' }), null)
})

test('the wallet uses shared Settings groups, rows, badges and buttons', () => {
  const html = render({ summary: { ...SUMMARY, balance: { balance: '-1.25' } } })
  for (const className of ['settings-group', 'settings-group__title', 'settings-list',
    'settings-item', 'settings-item__trail', 'settings-badge--warning', 'settings__btn']) {
    assert.ok(html.includes(className), className)
  }
  assert.doesNotMatch(html, /settings__section/)
  assert.match(html, /Credits wallet/)
  assert.match(html, /Pays for apps that charge per use/)
  assert.match(html, /You owe 1\.25 credits/)
  assert.match(html, /name="waiver"/)
  assert.match(html, /lose my right to cancel/)
})

test('wallet notices, refusals and unavailable balances remain readable', () => {
  assert.match(render({ summary: { error: { message: 'Offline' } } }), /Offline/)
  for (const notice of ['checking', 'paid', 'late', 'paused']) {
    assert.match(render({ notice }), /role="status"/)
  }
  const refused = render({ refusal: 'Please accept the cancellation terms first.' })
  assert.match(refused, /role="alert"/)
  assert.match(refused, /Please accept/)
})

test('a browser refusing opener access cannot throw or leave an unsafe window', t => {
  let closed = false
  const popup = { set opener(_value) { throw new Error('denied') }, close() { closed = true } }
  withWindowOpen(t, popup)
  assert.equal(openStripeWindow(), null)
  assert.equal(closed, true)
})

test('settlement cancellation stops before the next read', async () => {
  const controller = new AbortController()
  let reads = 0
  const status = await settleTopup('t', async () => {
    reads += 1
    return { status: 'pending' }
  }, { signal: controller.signal, sleep: async () => { controller.abort() } })
  assert.equal(status, 'pending')
  assert.equal(reads, 1)
})
