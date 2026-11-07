/* The Möbius account bridge client shows only confirmed account state and keeps deployment mutations on the account dashboard. */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  ACCOUNT_CONFIRM_CODE,
  MobiusAccessError,
  accountState,
  dashboardUrl,
  deploymentCanOpen,
  deploymentCanRecover,
  deploymentIsBuilding,
  deploymentNeedsTracking,
  deploymentPresentation,
  mergeDeployments,
  mobiusAccessRequest,
  parseDeploymentMetrics,
  parseIdentity,
  parseRailway,
  railwayAccountChanged,
  retryableAccessError,
  webUrl,
} from '../mobiusAccount.js'

const here = { id: 'local', name: 'My Möbius', status: 'Active', url: 'https://me.example.com', current: true }
const profile = { user_id: 'u1', email: 'ada@example.com', display_name: 'Ada L', handle: 'ada', avatar_url: null }
const instance = {
  id: 'mob_abc123',
  name: 'Work',
  status: 'ready',
  url: 'https://work.example.com',
  railway_url: 'https://railway.com/project/1',
  current_step: null,
  last_error: null,
  resources: { cpu: '2', memory_mb: 2048, volume_size_mb: null, plan: 'hobby' },
  actions: { edit_resources: true, retry: false, delete: true },
}

test('signed-out identity may not carry account details', () => {
  const signedOut = { account_mode: 'signed_out', account_unavailable: false, instance_id: null, profile: null, deployments: [here], member_since: null }
  assert.equal(parseIdentity(signedOut).profile, null)
  assert.throws(() => parseIdentity({ ...signedOut, profile }), /while signed out/)
  assert.throws(() => parseIdentity({ ...signedOut, deployments: [{ ...here, current: false }] }), /invalid account response/)
})

test('a linked outage never shows stale profile data, and provider names are dropped', () => {
  const linked = { account_mode: 'linked', account_unavailable: false, instance_id: null, profile, deployments: [here], member_since: '2026-04-01', extra: 1 }
  const parsed = parseIdentity(linked)
  assert.equal(parsed.profile.handle, 'ada')
  assert.equal('display_name' in parsed.profile, false)
  assert.throws(() => parseIdentity({ ...linked, account_unavailable: true }), /linked-account/)
  assert.equal(accountState(parseIdentity({ ...linked, account_unavailable: true, profile: null })), 'unavailable')
})

test('a managed outage keeps only the locally bound user and email', () => {
  const degraded = { user_id: 'u1', email: null, display_name: null, handle: null, avatar_url: null }
  const managed = { account_mode: 'managed', account_unavailable: true, instance_id: 'inst', profile: degraded, deployments: [here] }
  assert.equal(parseIdentity(managed).profile.handle, null)
  assert.throws(() => parseIdentity({ ...managed, profile: { ...degraded, handle: 'ada' } }), /managed-account/)
})

test('Railway details never appear without account access, and the dashboard must be https', () => {
  assert.throws(() => parseRailway({ railway_access: 'reconnect', connection: null, instances: [instance] }), /without account access/)
  const parsed = parseRailway({ railway_access: 'available', connection: { connected: true, account: 'a@b.c', plan: 'x' }, instances: [instance], dashboard_url: 'http://www.mobius.you/dashboard' })
  assert.equal(parsed.dashboard_url, '')
  assert.deepEqual(parsed.connection, { connected: true, account: 'a@b.c' })
  assert.equal(dashboardUrl('https://www.mobius.you/dashboard'), 'https://www.mobius.you/dashboard')
  assert.equal(dashboardUrl('https://www.mobius.you/dashboard?next=x'), '')
  assert.throws(() => parseRailway({ railway_access: 'available', connection: null, instances: [{ ...instance, railway_url: 'https://evil.example/p' }] }), /invalid Railway/)
})

test('this deployment comes first and Railway instances join their bridge card by origin', () => {
  const other = { ...instance, id: 'mob_other1', url: 'https://other.example.com' }
  const mine = { ...instance, url: 'https://me.example.com' }
  const cards = mergeDeployments([{ ...here, current: false, id: 'x', url: 'https://x.example.com' }, here], [mine, other])
  assert.deepEqual(cards.map(card => card.id), ['local', 'x', 'mob_other1'])
  assert.equal(cards[0].railway.id, mine.id)
})

test('unknown cost is hidden rather than shown as zero', () => {
  assert.equal(parseDeploymentMetrics({ cpu: { label: '5%' } }).cost, null)
  assert.equal(parseDeploymentMetrics({ cost: { heading: '' } }).cost, null)
  assert.equal(parseDeploymentMetrics({ cost: { heading: '$1.20 this month', percent: '24%' } }).cost.heading, '$1.20 this month')
})

test('recovery is offered for ready or failed deployments unless withheld', () => {
  assert.equal(deploymentCanRecover({ status: 'ready', actions: {} }), true)
  assert.equal(deploymentCanRecover({ status: 'ready', actions: { recover: false } }), false)
  assert.equal(deploymentCanRecover({ status: 'deploying', actions: {} }), false)
  assert.equal(deploymentPresentation({ status: 'delete_failed', last_error: 'check the build' }).tone, 'danger')
})

test('this Möbius may report a plain-HTTP address on any host; other deployments may not', () => {
  const signedOut = { account_mode: 'signed_out', account_unavailable: false, instance_id: null, profile: null, member_since: null }
  // Self-hosted installs may serve plain HTTP on a LAN address.
  const lan = { ...here, url: 'http://192.0.2.10:8000' }
  assert.equal(parseIdentity({ ...signedOut, deployments: [lan] }).deployments[0].url, 'http://192.0.2.10:8000')
  assert.throws(() => parseIdentity({ ...signedOut, deployments: [{ ...here, url: 'http://user:pw@192.0.2.10:8000' }] }), /invalid account response/)
  assert.throws(() => parseIdentity({ ...signedOut, deployments: [{ ...here, url: 'javascript:alert(1)' }] }), /invalid account response/)
  // An address the account service reports for another deployment keeps the HTTPS rule.
  const linked = { ...signedOut, account_mode: 'linked', profile }
  const other = { id: 'mob_x', name: 'Other', status: 'Active', url: 'http://192.0.2.20:8000', current: false }
  assert.throws(() => parseIdentity({ ...linked, deployments: [here, other] }), /invalid account response/)
})

function respond(t, status, body) {
  t.mock.method(globalThis, 'fetch', async () => new Response(
    body === undefined ? null : typeof body === 'string' ? body : JSON.stringify(body),
    { status, headers: { 'Content-Type': 'application/json' } },
  ))
}

async function failure(promise) {
  try {
    await promise
  } catch (error) {
    return error
  }
  assert.fail('expected the request to fail')
}

test('an unreachable account service is a retryable failure with a plain message', async t => {
  t.mock.method(globalThis, 'fetch', async () => { throw new TypeError('fetch failed') })
  const error = await failure(mobiusAccessRequest('/identity'))
  assert.ok(error instanceof MobiusAccessError)
  assert.equal(error.status, 0)
  assert.match(error.message, /could not reach its account service/)
  assert.equal(retryableAccessError(error), true)
})

test('a cancelled request stays a cancellation, not an account error', async t => {
  t.mock.method(globalThis, 'fetch', async () => { throw new DOMException('aborted', 'AbortError') })
  const error = await failure(mobiusAccessRequest('/identity'))
  assert.equal(error.name, 'AbortError')
  assert.equal(error instanceof MobiusAccessError, false)
})

test('a dashboard-only action carries its code and only a validated dashboard address', async t => {
  const detail = { code: ACCOUNT_CONFIRM_CODE, message: 'Confirm this on your account.', confirm_url: 'https://www.mobius.you/dashboard' }
  respond(t, 409, { detail })
  let error = await failure(mobiusAccessRequest('/identity/railway/instances/mob_abc123', { method: 'DELETE' }))
  assert.equal(error.status, 409)
  assert.equal(error.code, ACCOUNT_CONFIRM_CODE)
  assert.equal(error.message, 'Confirm this on your account.')
  assert.equal(error.confirmUrl, 'https://www.mobius.you/dashboard')
  assert.equal(retryableAccessError(error), false)

  respond(t, 409, { detail: { ...detail, confirm_url: 'https://evil.example/phish' } })
  error = await failure(mobiusAccessRequest('/identity/railway/instances/mob_abc123', { method: 'DELETE' }))
  assert.equal(error.confirmUrl, '')
  // Another code never carries a dashboard address, even a valid one.
  respond(t, 409, { detail: { ...detail, code: 'other' } })
  error = await failure(mobiusAccessRequest('/identity/railway/instances/mob_abc123', { method: 'DELETE' }))
  assert.equal(error.confirmUrl, '')
})

test('service errors keep their message, and an unreadable one gets a plain fallback', async t => {
  respond(t, 409, { detail: 'That handle is taken.' })
  let error = await failure(mobiusAccessRequest('/identity/profile', { method: 'PATCH' }))
  assert.equal(error.message, 'That handle is taken.')
  assert.equal(error.code, '')

  respond(t, 502, '<html>Bad gateway</html>')
  error = await failure(mobiusAccessRequest('/identity'))
  assert.equal(error.status, 502)
  assert.equal(error.message, 'Your Möbius account is unavailable right now.')
  assert.equal(retryableAccessError(error), true)
})

test('account requests ride the shell API client: /api path, owner headers, no JSON type on an upload', async t => {
  const sent = []
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    sent.push({ url: String(url), headers: init.headers, signal: init.signal })
    return new Response(null, { status: 204 })
  })
  await mobiusAccessRequest('/identity/link', { method: 'DELETE' })
  const body = new FormData()
  body.append('avatar', new Blob(['png'], { type: 'image/png' }))
  await mobiusAccessRequest('/identity/avatar', { method: 'POST', body })
  assert.match(sent[0].url, /\/api\/identity\/link$/)
  assert.equal(sent[0].headers['Content-Type'], 'application/json')
  // The shell's deadline applies to every account request.
  assert.ok(sent[0].signal instanceof AbortSignal)
  assert.equal('Content-Type' in sent[1].headers, false)
})

test('known routes return their validated shape, whatever their query string', async t => {
  respond(t, 204)
  assert.equal(await mobiusAccessRequest('/identity/link'), null)
  respond(t, 200, { railway_access: 'signed_out', connection: null, instances: [] })
  assert.deepEqual(await mobiusAccessRequest('/identity/railway?dashboard=1'), {
    railway_access: 'signed_out', connection: null, instances: [], dashboard_url: '',
  })
  respond(t, 200, { railway_access: 'signed_out', connection: null, instances: [instance] })
  await assert.rejects(mobiusAccessRequest('/identity/railway?dashboard=1'), /without account access/)
})

test('web addresses must be https without credentials; plain HTTP only on this machine', () => {
  assert.equal(webUrl('https://me.example.com/x')?.href, 'https://me.example.com/x')
  assert.equal(webUrl('http://localhost:8000')?.host, 'localhost:8000')
  assert.equal(webUrl('http://127.0.0.1:8000')?.host, '127.0.0.1:8000')
  assert.equal(webUrl('http://localhost:8000', { httpsOnly: true }), null)
  assert.equal(webUrl('http://192.0.2.10'), null)
  assert.equal(webUrl('https://user:pw@me.example.com'), null)
  assert.equal(webUrl('javascript:alert(1)'), null)
  assert.equal(webUrl(`https://me.example.com/${'a'.repeat(2048)}`), null)
  assert.equal(webUrl(null), null)
})

test('a Railway reconnect is noticed only when a different account is connected', () => {
  const connected = account => ({ connection: { connected: true, account } })
  assert.equal(railwayAccountChanged('a@b.c', connected('d@e.f')), true)
  assert.equal(railwayAccountChanged('', connected('d@e.f')), true)
  assert.equal(railwayAccountChanged('a@b.c', connected('a@b.c')), false)
  assert.equal(railwayAccountChanged('a@b.c', connected('')), false)
  assert.equal(railwayAccountChanged('a@b.c', { connection: { connected: false, account: 'd@e.f' } }), false)
  assert.equal(railwayAccountChanged('a@b.c', null), false)
})

test('deployments in flight are followed live, and only running ones can be opened', () => {
  assert.equal(deploymentNeedsTracking({ status: 'Deploying' }), true)
  assert.equal(deploymentNeedsTracking({ status: 'deleting' }), true)
  assert.equal(deploymentNeedsTracking({ status: 'ready', updates: { state: 'checking' } }), true)
  assert.equal(deploymentNeedsTracking({ status: 'ready', updates: { state: 'done' } }), false)
  assert.equal(deploymentIsBuilding({ status: 'queued' }), true)
  assert.equal(deploymentIsBuilding({ status: 'deleting' }), false)
  assert.equal(deploymentCanOpen({ status: 'Active' }), true)
  assert.equal(deploymentCanOpen({ status: 'deploying' }), false)
  assert.deepEqual(deploymentPresentation({ status: 'creating' }), { label: 'Deploying', detail: 'Möbius is following the Railway build.', tone: 'progress' })
  assert.deepEqual(deploymentPresentation({ status: 'ready', current_step: 'Ready' }), { label: 'Active', detail: '', tone: 'success' })
  assert.equal(deploymentPresentation({ status: 'error' }).detail, 'This deployment needs your attention.')
  assert.equal(deploymentPresentation({ status: 'paused' }).label, 'paused')
})
