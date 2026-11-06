/* The Möbius account bridge client shows only confirmed account state and keeps deployment mutations on the account dashboard. */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  accountState,
  dashboardUrl,
  deploymentCanRecover,
  deploymentPresentation,
  mergeDeployments,
  parseDeploymentMetrics,
  parseIdentity,
  parseRailway,
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
