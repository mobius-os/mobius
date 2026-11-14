/* The Credits page for a paired Möbius: the wallet apps spend from, and its history. */

import test from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import WalletSection, { ActivityRow, WalletBuyForm, WalletConnect } from '../../components/SettingsView/WalletSection.jsx'
import ActivityWindow, { ACTIVITY_KEY, RecentActivity } from '../../components/SettingsView/ActivityWindow.jsx'
import {
  activityDays, activityLabel, activityTime, canReconnect, checkoutPage, creditsSection,
  openStripeWindow, settleTopup, termsFor, walletGlance,
} from '../creditsWallet.js'

const SUMMARY = {
  mode: 'customer',
  service_host: 'www.mobius.you',
  balance: { balance: '2.25', available: '2.25', state: 'healthy' },
}
const TERMS = { currency: 'usd', amounts_cents: [500, 1000], waiver_required: true, stripe_mode: 'test' }

const render = props => renderToStaticMarkup(createElement(WalletSection, {
  summary: SUMMARY, terms: TERMS, ...props,
}))

test('the wallet balance is the headline and names where it is kept', () => {
  const html = render({})
  assert.match(html, /2\.25/)
  assert.match(html, /Kept by www\.mobius\.you/)
  // Amounts open under the balance only once Add credits is pressed.
  assert.match(html, /<button[^>]*aria-expanded="false"[^>]*>Add credits/)
  assert.doesNotMatch(html, /\$5/)
})

test('adding credits offers the service amounts, the test-mode note and the waiver', () => {
  const html = renderToStaticMarkup(createElement(WalletBuyForm, {
    terms: TERMS, busy: false, onBuy: () => {}, onCancel: () => {},
  }))
  assert.match(html, /\$5/)
  assert.match(html, /\$10/)
  assert.match(html, /test mode — no real money moves/)
  assert.match(html, /name="waiver"/)
  assert.match(html, /lose my right to cancel/)
  assert.match(html, />Buy \$5 of credits</)
  assert.match(html, />Cancel</)
})

test('a paid app run shows in recent activity with who was paid', () => {
  const html = renderToStaticMarkup(createElement(ActivityRow, { when: '15:25', entry: {
    id: 't1', at: '2026-11-03T15:25:58Z', kind: 'app_call', amount: '-0.50',
    description: 'Used ana/prompts/run-659eab7c',
  } }))
  assert.match(html, /Paid @ana in prompts/)
  assert.match(html, /−0\.50/)
  assert.doesNotMatch(html, /wallet-activity__icon--in/)
})

test('recent activity is a scrolling window of the whole history, grouped by day, top-ups marked', () => {
  const client = new QueryClient()
  const at = d => new Date(2026, 10, d, 12).toISOString()
  client.setQueryData([ACTIVITY_KEY, 'p1'], {
    pageParams: [null],
    pages: [{ next_cursor: 'c', transactions: [
      { id: 'a', at: at(10), kind: 'topup', amount: '5.00', description: 'Bought credits' },
      { id: 'b', at: at(10), kind: 'app_call', amount: '-0.10', description: 'Used ana/prompts/run' },
      { id: 'c', at: at(9), kind: 'app_call', amount: '-5.00', description: 'Used ana/prompts/run' },
    ] }],
  })
  const window = renderToStaticMarkup(createElement(QueryClientProvider, { client },
    createElement(ActivityWindow, { pairing: 'p1' })))
  assert.match(window, /class="settings-list wallet-window"/)
  assert.equal((window.match(/class="wallet-day__title"/g) || []).length, 2)
  assert.equal((window.match(/<li class="settings-item"/g) || []).length, 3)
  assert.match(window, /wallet-activity__icon--in/)
  assert.match(window, /\+5\.00/)
  // More history exists, so the window watches for the end of its scroll.
  assert.match(window, /wallet-activity__sentinel/)

  assert.match(renderToStaticMarkup(createElement(RecentActivity, null, createElement('div', null, 'history'))),
    /Recent activity[^]*history/)
  assert.doesNotMatch(render(), /Recent activity/)
})

test('a failed older page waits for Try again instead of retrying in a loop', () => {
  const client = new QueryClient()
  const query = client.getQueryCache().build(client, { queryKey: [ACTIVITY_KEY, 'p1'] })
  query.setState({
    status: 'error',
    error: new Error('service unavailable'),
    data: { pageParams: [null], pages: [{ next_cursor: 'c', transactions: [
      { id: 'a', at: new Date(2026, 10, 10, 12).toISOString(), kind: 'topup', amount: '5.00', description: 'Bought credits' },
    ] }] },
    fetchStatus: 'idle',
    fetchMeta: { fetchMore: { direction: 'forward' } },
  })
  const window = renderToStaticMarkup(createElement(QueryClientProvider, { client },
    createElement(ActivityWindow, { pairing: 'p1' })))
  // The lines already read stay; the end of the window is no longer watched.
  assert.match(window, /Bought credits/)
  assert.match(window, /More activity could not be loaded/)
  assert.match(window, />Try again</)
  assert.doesNotMatch(window, /wallet-activity__sentinel/)
})

test('the full history groups under day headings, newest first, one group per day across pages', () => {
  const now = new Date(2026, 10, 12, 13, 0)
  const at = (d, h) => new Date(2026, 10, d, h).toISOString()
  const days = activityDays([
    { id: 'a', at: at(12, 9) }, { id: 'b', at: at(12, 8) },
    { id: 'c', at: at(11, 20) },
    { id: 'd', at: at(9, 15) }, { id: 'e', at: at(9, 14) },
    { id: 'f', at: new Date(2025, 11, 30, 8).toISOString() },
  ], now, 'en-GB')
  assert.deepEqual(days.map(day => day.label), ['Today', 'Yesterday', '9 November', '30 December 2025'])
  assert.deepEqual(days.map(day => day.entries.map(e => e.id).join('')), ['ab', 'c', 'de', 'f'])
  assert.match(activityTime(at(9, 15), 'en-GB'), /^15:00$/)
})

test('the account page row shows the spendable balance, a debt, or that credits are not connected', () => {
  assert.deepEqual(walletGlance(SUMMARY), { kind: 'balance', figure: '2.25' })
  assert.deepEqual(walletGlance({ ...SUMMARY, balance: { balance: '-1.25', available: '0.00' } }), { kind: 'owed', figure: '1.25' })
  assert.deepEqual(walletGlance({ mode: 'unpaired', can_pair_with_account: true }), { kind: 'unconnected' })
  // Nothing trustworthy to show: still loading, unreadable, or no credits here.
  assert.equal(walletGlance(undefined), null)
  assert.equal(walletGlance({ mode: 'customer', error: { message: 'Offline' } }), null)
  assert.equal(walletGlance({ mode: 'unknown' }), null)
})

test('without the service terms no amounts are offered', () => {
  assert.doesNotMatch(render({ terms: null }), /Buy |Add credits/)
  assert.match(render({}), /Add credits/)
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

test('a wallet whose key the service refuses offers to reconnect through mobius.you', () => {
  const refused = {
    mode: 'customer', service_host: 'www.mobius.you', balance: null, can_pair_with_account: true,
    error: { code: 'key_refused', message: "The credits service refused this installation's key. It may have been revoked." },
  }
  assert.equal(canReconnect(refused), true)
  assert.equal(canReconnect({ ...refused, error: { code: 'invalid_api_key', message: 'no' } }), true)
  // A key that may still work is never replaced, and a self-hosted Möbius cannot ask.
  assert.equal(canReconnect({ ...refused, error: { code: '', message: 'Could not reach www.mobius.you.' } }), false)
  assert.equal(canReconnect({ ...refused, can_pair_with_account: false }), false)
  assert.equal(canReconnect({ ...refused, can_pair_with_account: undefined }), false)
  assert.equal(canReconnect(SUMMARY), false)

  const html = render({ summary: refused, terms: null })
  assert.match(html, /may have been revoked/)
  assert.match(html, />Reconnect credits</)
  assert.match(render({ summary: refused, terms: null, busy: true }), /disabled=""[^>]*>Connecting…</)
  assert.doesNotMatch(render({ summary: { ...refused, can_pair_with_account: false }, terms: null }), /Reconnect/)
  assert.doesNotMatch(render({}), /Reconnect/)
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
  assert.match(html, /Add credits/)
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
