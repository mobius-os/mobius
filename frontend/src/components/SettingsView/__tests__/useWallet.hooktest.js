// Settings → Credits across a re-pairing: purchase terms belong to one service.
import { afterEach, beforeEach, test } from 'node:test'
import assert from 'node:assert/strict'
import { __setRerender, renderHook as renderTestHook } from '../../ChatView/hooks/__tests__/react-hook-shim.mjs'
import { useWallet } from '../useWallet.js'

const GBP = { currency: 'gbp', amounts_cents: [500], waiver_required: true }
const USD = { currency: 'usd', amounts_cents: [1000, 2500], waiver_required: false }

const mountedHooks = []
function renderHook(...args) {
  const hook = renderTestHook(...args)
  mountedHooks.push(hook)
  return hook
}

const saved = {}
beforeEach(() => {
  for (const key of ['window', 'document', 'fetch']) {
    saved[key] = Object.getOwnPropertyDescriptor(globalThis, key)
  }
  globalThis.window = new EventTarget()
  globalThis.document = Object.assign(new EventTarget(), { visibilityState: 'visible' })
})
afterEach(() => {
  for (const hook of mountedHooks.splice(0)) hook.unmount()
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
  const server = { pairing: 'p-old', terms: GBP, holdTerms: null, readAfterHold: false, termsDown: false, calls: [] }
  server.pairWith = (pairing, terms) => { server.pairing = pairing; server.terms = terms }
  globalThis.fetch = async (url, init = {}) => {
    const path = String(url).replace(/^.*\/api/, '')
    const body = init.body ? JSON.parse(init.body) : null
    server.calls.push({ path, body, signal: init.signal })
    if (path === '/identity/credits') {
      return json({ mode: 'customer', pairing: server.pairing, balance: { available: '1.00' } })
    }
    if (path.startsWith('/identity/credits/transactions')) return json({ transactions: [] })
    if (path === '/identity/credits/purchase-terms') {
      if (server.termsDown) return json({ detail: 'unavailable' }, 503)
      const answer = () => ({ ...server.terms, pairing: server.pairing })
      const early = answer()
      if (server.holdTerms) await server.holdTerms
      return json(server.readAfterHold ? answer() : early)
    }
    if (path.startsWith('/identity/credits/topups/')) {
      if (server.holdStatus) await server.holdStatus
      return json({ status: server.topupStatus || 'pending' })
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

test('terms_that_failed_to_load_are_read_again_when_the_wallet_refreshes', async () => {
  const server = stubServer()
  server.termsDown = true
  const { result } = renderHook(useWallet, { active: true })
  await tick()
  assert.equal(result.current.summary.pairing, 'p-old')
  assert.equal(result.current.terms, null) // No amounts: the buy control stays hidden.

  // Connectivity returns; the owner comes back to the window, same pairing.
  server.termsDown = false
  window.dispatchEvent(new Event('focus'))
  await tick()
  assert.equal(result.current.terms?.currency, 'gbp')
  assert.equal(result.current.terms?.pairing, 'p-old')
})

const flush = async () => { for (let i = 0; i < 60; i += 1) await Promise.resolve() }
const CHECKOUT_LIMIT = 30 * 60 * 1_000

async function checkoutHook(t) {
  const server = stubServer()
  const hook = renderHook(useWallet, { active: true })
  await tick()
  const popup = { opener: {}, closed: false, location: {}, document: { body: {} },
    close() { this.closed = true } }
  window.open = () => popup
  t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] })
  const purchase = hook.result.current.buy({ amountCents: 500, waiverAccepted: true })
  await flush()
  assert.equal(hook.result.current.busy, true)
  assert.match(popup.location.href, /^https:\/\/checkout\.stripe\.com\//)
  return { server, hook, popup, purchase }
}

test('unmount removes the popup watcher and never writes state afterwards', async t => {
  const { server, hook, popup, purchase } = await checkoutHook(t)
  let watched = 0
  Object.defineProperty(popup, 'closed', { get: () => { watched += 1; return false } })
  t.mock.timers.tick(800)
  assert.equal(watched, 1)
  hook.unmount()
  let updates = 0
  __setRerender(() => { updates += 1 })
  t.mock.timers.tick(CHECKOUT_LIMIT)
  await purchase
  assert.equal(watched, 1)
  assert.equal(updates, 0)
  assert.equal(server.calls.filter(c => c.path.includes('/topups/')).length, 0)
  assert.equal(server.calls.find(c => c.path.endsWith('/topup')).signal.aborted, true)
})

test('unmount cancels settlement sleeps and stops all later polling', async t => {
  const { server, hook, popup, purchase } = await checkoutHook(t)
  popup.closed = true
  t.mock.timers.tick(800)
  await flush()
  assert.equal(hook.result.current.notice, 'checking')
  const reads = () => server.calls.filter(c => c.path.includes('/topups/'))
  assert.equal(reads().length, 1)
  hook.unmount()
  let updates = 0
  __setRerender(() => { updates += 1 })
  await purchase
  t.mock.timers.tick(CHECKOUT_LIMIT)
  await flush()
  assert.equal(reads().length, 1)
  assert.equal(reads()[0].signal.aborted, true)
  assert.equal(updates, 0)
})

test('checkout watching has a deadline and leaves a calm notice', async t => {
  const { server, hook, purchase } = await checkoutHook(t)
  t.mock.timers.tick(CHECKOUT_LIMIT)
  await purchase
  assert.equal(hook.result.current.busy, false)
  assert.equal(hook.result.current.notice, 'paused')
  assert.equal(hook.result.current.refusal, null)
  assert.equal(server.calls.filter(c => c.path.includes('/topups/')).length, 0)
})

test('the same deadline covers an in-flight settlement read', async t => {
  const { server, hook, popup, purchase } = await checkoutHook(t)
  let release
  server.holdStatus = new Promise(resolve => { release = resolve })
  popup.closed = true
  t.mock.timers.tick(800)
  await flush()
  const poll = server.calls.find(c => c.path.includes('/topups/'))
  assert.ok(poll)
  t.mock.timers.tick(CHECKOUT_LIMIT - 800)
  assert.equal(poll.signal.aborted, true)
  assert.equal(hook.result.current.notice, 'paused')
  assert.equal(hook.result.current.busy, false)
  server.topupStatus = 'paid'
  release() // Even a transport that delivers after cancellation cannot write state.
  await purchase
  assert.equal(hook.result.current.notice, 'paused')
  assert.equal(server.calls.filter(c => c.path.includes('/topups/')).length, 1)
})

test('a summary arriving after unmount cannot update state or start more reads', async () => {
  let release
  const held = new Promise(resolve => { release = resolve })
  let calls = 0
  globalThis.fetch = async () => {
    calls += 1
    await held
    return json({ mode: 'customer', pairing: 'p-old' })
  }
  const hook = renderHook(useWallet, { active: true })
  hook.unmount()
  let updates = 0
  __setRerender(() => { updates += 1 })
  release()
  await tick()
  assert.equal(updates, 0)
  assert.equal(calls, 1)
})

test('a paid checkout refreshes the balance and releases its lifecycle', async t => {
  const { server, hook, popup, purchase } = await checkoutHook(t)
  server.topupStatus = 'paid'
  popup.closed = true
  t.mock.timers.tick(800)
  await purchase
  await flush()
  assert.equal(hook.result.current.notice, 'paid')
  assert.equal(hook.result.current.busy, false)
  assert.equal(server.calls.filter(c => c.path === '/identity/credits').length, 2)
  t.mock.timers.tick(CHECKOUT_LIMIT)
  assert.equal(hook.result.current.notice, 'paid')
})

/** An unpaired Möbius launched from mobius.you, which the launcher can pair. */
function stubUnpaired({ refuse = false } = {}) {
  const server = { paired: false, calls: [] }
  globalThis.fetch = async (url, init = {}) => {
    const path = String(url).replace(/^.*\/api/, '')
    server.calls.push({ path, method: init.method || 'GET' })
    if (path === '/identity/credits') {
      return json(server.paired
        ? { mode: 'customer', pairing: 'p-account', balance: { available: '0.00' } }
        : { mode: 'unpaired', pairing: null, can_pair_with_account: true })
    }
    if (path === '/identity/credits/pair/account') {
      if (refuse) {
        return json({ detail: { code: 'credits_unavailable', message: 'mobius.you could not connect your credits just now.' } }, 503)
      }
      server.paired = true
      return json({ paired: true })
    }
    if (path.startsWith('/identity/credits/transactions')) return json({ transactions: [] })
    if (path === '/identity/credits/purchase-terms') return json({ ...USD, pairing: 'p-account' })
    throw new Error(`unexpected ${path}`)
  }
  return server
}

test('connecting_with_the_account_pairs_and_shows_the_wallet', async () => {
  const server = stubUnpaired()
  const { result } = renderHook(useWallet, { active: true })
  await tick()
  assert.equal(result.current.summary.mode, 'unpaired')
  assert.equal(result.current.summary.can_pair_with_account, true)

  await result.current.pairWithAccount()
  await tick()

  assert.ok(server.calls.some(c => c.path === '/identity/credits/pair/account' && c.method === 'POST'))
  assert.equal(result.current.summary.mode, 'customer')
  assert.equal(result.current.refusal, null)
  assert.equal(result.current.busy, false)
})

test('a_refused_connection_says_why_and_stays_unpaired', async () => {
  stubUnpaired({ refuse: true })
  const { result } = renderHook(useWallet, { active: true })
  await tick()

  await result.current.pairWithAccount()
  await tick()

  assert.equal(result.current.summary.mode, 'unpaired')
  assert.equal(result.current.refusal, 'mobius.you could not connect your credits just now.')
  assert.equal(result.current.busy, false)
})
