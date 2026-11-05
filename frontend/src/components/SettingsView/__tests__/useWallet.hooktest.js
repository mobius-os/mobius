// Settings → Credits across a re-pairing: purchase terms belong to one service.
import { afterEach, beforeEach, test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../ChatView/hooks/__tests__/react-hook-shim.mjs'
import { useWallet } from '../useWallet.js'

const GBP = { currency: 'gbp', amounts_cents: [500], waiver_required: true }
const USD = { currency: 'usd', amounts_cents: [1000, 2500], waiver_required: false }

const saved = {}
beforeEach(() => {
  for (const key of ['window', 'document', 'fetch']) {
    saved[key] = Object.getOwnPropertyDescriptor(globalThis, key)
  }
  globalThis.window = new EventTarget()
  globalThis.document = Object.assign(new EventTarget(), { visibilityState: 'visible' })
})
afterEach(() => {
  for (const [key, descriptor] of Object.entries(saved)) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor)
    else delete globalThis[key]
  }
})

const tick = async () => { for (let i = 0; i < 5; i += 1) await new Promise(r => setTimeout(r, 0)) }
const json = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})

/** A credits service this Möbius is paired with; `pairWith` re-pairs it. */
function stubServer() {
  // `holdTerms` delays a terms answer: one the service already wrote (a slow
  // network), or with `readAfterHold`, one the service writes only then.
  const server = { pairing: 'p-old', terms: GBP, holdTerms: null, readAfterHold: false, calls: [] }
  server.pairWith = (pairing, terms) => { server.pairing = pairing; server.terms = terms }
  globalThis.fetch = async (url, init = {}) => {
    const path = String(url).replace(/^.*\/api/, '')
    const body = init.body ? JSON.parse(init.body) : null
    server.calls.push({ path, body })
    if (path === '/identity/credits') {
      return json({ mode: 'customer', pairing: server.pairing, balance: { available: '1.00' } })
    }
    if (path.startsWith('/identity/credits/transactions')) return json({ transactions: [] })
    if (path === '/identity/credits/purchase-terms') {
      const answer = () => ({ ...server.terms, pairing: server.pairing })
      const early = answer()
      if (server.holdTerms) await server.holdTerms
      return json(server.readAfterHold ? answer() : early)
    }
    if (path === '/identity/credits/topup') {
      if (body.pairing !== server.pairing) {
        return json({ detail: { code: 'pairing_changed', message: 'Paired again.' } }, 409)
      }
      return json({ checkout_url: 'https://checkout.stripe.com/c/pay/cs_1', topup_id: 't1' })
    }
    throw new Error(`unexpected ${path}`)
  }
  return server
}

test('pairing_again_drops_the_old_services_terms_before_showing_the_new_ones', async () => {
  const server = stubServer()
  const { result } = renderHook(useWallet, { active: true })
  await tick()
  assert.equal(result.current.terms.currency, 'gbp')

  server.pairWith('p-new', USD)
  window.dispatchEvent(new Event('focus'))
  await tick()
  assert.equal(result.current.summary.pairing, 'p-new')
  assert.equal(result.current.terms.currency, 'usd')
  assert.equal(result.current.terms.pairing, 'p-new')
  assert.deepEqual(result.current.terms.amounts_cents, [1000, 2500])
})

test('a_terms_read_that_lands_after_pairing_again_is_never_shown', async () => {
  const server = stubServer()
  let release
  server.holdTerms = new Promise(resolve => { release = resolve })
  const { result } = renderHook(useWallet, { active: true })
  await tick()
  assert.equal(result.current.terms, null) // The old service's terms are still in flight.

  // The owner pairs again elsewhere; the old read answers only afterwards.
  server.pairWith('p-new', USD)
  window.dispatchEvent(new Event('focus'))
  await tick()
  assert.equal(result.current.summary.pairing, 'p-new')
  release()
  server.holdTerms = null
  await tick()
  assert.equal(result.current.terms?.currency, 'usd')
  assert.notEqual(result.current.terms?.currency, 'gbp')
})

test('terms_read_under_a_newer_pairing_than_the_summary_wait_for_the_summary', async () => {
  const server = stubServer()
  let release
  server.holdTerms = new Promise(resolve => { release = resolve })
  server.readAfterHold = true
  const { result } = renderHook(useWallet, { active: true })
  await tick()
  // Paired again between the summary read and the terms read.
  server.pairWith('p-new', USD)
  release()
  server.holdTerms = null
  await tick()
  // Never USD terms under the old summary; the summary is read again instead.
  assert.equal(result.current.summary.pairing, 'p-new')
  assert.equal(result.current.terms.currency, 'usd')
  assert.equal(server.calls.filter(c => c.path === '/identity/credits').length, 2)
})

test('a_purchase_names_its_pairing_and_a_refusal_after_pairing_again_reloads', async () => {
  const server = stubServer()
  const { result } = renderHook(useWallet, { active: true })
  await tick()
  const popup = { opener: {}, closed: false, document: { body: {} }, close() { this.closed = true } }
  window.open = () => popup
  server.pairWith('p-new', USD) // Not seen by this screen yet.

  await result.current.buy({ amountCents: 500, waiverAccepted: true })
  const topup = server.calls.find(c => c.path === '/identity/credits/topup')
  assert.equal(topup.body.pairing, 'p-old')
  assert.equal(popup.closed, true)
  assert.match(result.current.refusal, /Paired again/)
  await tick()
  assert.equal(result.current.summary.pairing, 'p-new')
  assert.equal(result.current.terms.currency, 'usd')
})
