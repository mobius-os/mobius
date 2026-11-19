// Keys with several parts: a new key's card asks for one field per part, Allow
// sends each pasted part by name, and the key page lists every part (a key
// listed before parts existed reads as its one value).
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createSsrRenderServer } from './ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
const {
  approvalBody, newKeyParts, partsFilled, keyParts, partLabel, sentAs, requestWording,
} = await vite.ssrLoadModule('/src/lib/keysSettingsText.js')
after(() => vite.close())

const BASIC = { type: 'basic', username: 'account', password: 'token' }
const PARTS = [{ name: 'account', label: 'Account SID', secret: false }, { name: 'token', label: '', secret: true }]
const ask = (overrides = {}) => ({
  id: 'agent-twilio', key: 'twilio', requester: 'agent', app: null, ticket: 't1', key_exists: false,
  sites: [{ host: 'api.twilio.com', methods: ['POST'], paths: ['/2010-04-01/*'] }],
  inject: BASIC, parts: PARTS, ...overrides,
})

test('a new key of several parts is pasted part by part and sent by name', () => {
  assert.deepEqual(newKeyParts(ask()), PARTS)
  assert.deepEqual(approvalBody(ask(), { account: 'AC1', token: 'secret' }), {
    ticket: 't1', sites: ask().sites, parts: { account: 'AC1', token: 'secret' }, inject: BASIC,
  })
})

test('an existing key or a one-value request never asks for parts', () => {
  assert.equal(newKeyParts(ask({ key_exists: true })), null)
  assert.equal(newKeyParts(ask({ parts: null })), null)
  assert.deepEqual(approvalBody(ask({ parts: null, inject: null }), 'secret').value, 'secret')
})

test('Allow waits until every part has text', () => {
  assert.equal(partsFilled(PARTS, { account: 'AC1' }), false)
  assert.equal(partsFilled(PARTS, { account: 'AC1', token: '  ' }), false)
  assert.equal(partsFilled(PARTS, { account: 'AC1', token: 's' }), true)
})

test('the paste step names details rather than one key', () => {
  assert.equal(requestWording(ask(), {}).pasteTitle, 'Paste your api.twilio.com details')
  assert.equal(requestWording(ask({ parts: null, inject: null }), {}).pasteTitle, 'Paste your api.twilio.com key')
})

test('a key from before parts reads as its one value; labels fall back to names', () => {
  assert.deepEqual(keyParts({ name: 'k', multiline: true }), [{ name: 'value', label: '', secret: true, multiline: true }])
  assert.deepEqual(keyParts({ name: 'k', parts: PARTS }), PARTS)
  assert.equal(partLabel(PARTS[0]), 'Account SID')
  assert.equal(partLabel(PARTS[1]), 'token')
})

test('every recipe reads as how it is sent', () => {
  assert.equal(sentAs(BASIC), 'Authorization: Basic (account and token)')
  assert.equal(sentAs({ type: 'slots', slots: [
    { type: 'header', name: 'X-Api-Key', template: 'Key {secret}' },
    { type: 'query', name: 'app', part: 'app' },
    { type: 'body', format: 'json', field: 'client_secret', part: 'secret' },
  ] }), 'X-Api-Key: Key {secret} · ?app={app} · JSON field client_secret={secret}')
  assert.equal(sentAs({ type: 'header', name: 'Authorization', prefix: 'Bearer ' }), 'Authorization: Bearer …')
})
