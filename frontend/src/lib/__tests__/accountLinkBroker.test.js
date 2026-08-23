import test from 'node:test'
import assert from 'node:assert/strict'

import {
  accountLinkCompletion,
  accountLinkRegistration,
  accountLinkUnregistration,
  identityLinkBrokerAllowed,
} from '../accountLinkBroker.js'

const state = 's'.repeat(43)
const expiresAt = '2026-08-20T03:00:00Z'

test('account-link broker requires the reviewed identity capability', () => {
  assert.equal(identityLinkBrokerAllowed({ data: { identity_manage: true } }), true)
  assert.equal(identityLinkBrokerAllowed({ data: { identity_manage: false } }), false)
  assert.equal(identityLinkBrokerAllowed(null), false)
})

test('account-link registration accepts only an exact secure or loopback origin', () => {
  const valid = accountLinkRegistration({
    type: 'moebius:account-link-register',
    state,
    authorizationOrigin: 'https://www.mobius.you',
    expiresAt,
  }, () => 1_000)
  assert.deepEqual(valid, {
    state,
    authorizationOrigin: 'https://www.mobius.you',
    deadline: 601_000,
  })
  assert.ok(accountLinkRegistration({
    type: 'moebius:account-link-register',
    state,
    authorizationOrigin: 'http://127.0.0.1:8080',
    expiresAt,
  }))
  for (const authorizationOrigin of (
    [
      'http://account.example',
      'https://account.example/path',
      'https://u:p@account.example',
      'http://192.168.1.2:8080',
    ]
  )) {
    assert.equal(accountLinkRegistration({
      type: 'moebius:account-link-register',
      state,
      authorizationOrigin,
      expiresAt,
    }), null)
  }
  assert.equal(accountLinkRegistration({
    type: 'moebius:account-link-register',
    state,
    authorizationOrigin: 'https://www.mobius.you',
    expiresAt,
    extra: true,
  }), null)
})

test('account-link unregistration is exact and state-bound', () => {
  const registration = { state }
  assert.equal(accountLinkUnregistration({
    type: 'moebius:account-link-unregister', state,
  }, registration), true)
  assert.equal(accountLinkUnregistration({
    type: 'moebius:account-link-unregister', state: 'x'.repeat(43),
  }, registration), false)
  assert.equal(accountLinkUnregistration({
    type: 'moebius:account-link-unregister', state, extra: true,
  }, registration), false)
})

test('account-link completion consumes only the exact origin, state and shape', () => {
  const registration = {
    state,
    authorizationOrigin: 'https://www.mobius.you',
    deadline: 10_000,
  }
  const event = {
    origin: registration.authorizationOrigin,
    data: { type: 'mobius-account-link', state, code: 'c'.repeat(32) },
  }
  assert.deepEqual(accountLinkCompletion(event, registration, () => 9_000), {
    type: 'moebius:account-link-result',
    state,
    code: 'c'.repeat(32),
    authorizationOrigin: registration.authorizationOrigin,
  })
  assert.equal(accountLinkCompletion({ ...event, origin: 'https://evil.example' }, registration), null)
  assert.equal(accountLinkCompletion({
    ...event, data: { ...event.data, state: 'x'.repeat(43) },
  }, registration), null)
  assert.equal(accountLinkCompletion({
    ...event, data: { ...event.data, extra: true },
  }, registration), null)
  assert.equal(accountLinkCompletion(event, registration, () => 10_001), null)
})
