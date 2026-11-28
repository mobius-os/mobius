// Keys with several parts: a new key's card asks for one field per part, Allow
// sends each pasted part by name, and the key page lists every part (a key
// listed before parts existed reads as its one value).
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createSsrRenderServer } from './ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
const {
  approvalBody, newKeyParts, partsFilled, keyParts, partLabel, sentAs, requestWording, tokenClaims, plainPartsNote,
  signedRequestLimit, signInEndpoints, signInNote, signInState, usageWhat,
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
  assert.equal(requestWording(ask(), {}).pasteTitle, 'Paste the parts of your api.twilio.com key')
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

test('signing recipes read as what the vault does with the key', () => {
  assert.equal(sentAs({ type: 'aws_sigv4', service: 's3', region: 'auto' }), 'an AWS signature (s3, region auto)')
  assert.equal(sentAs({ type: 'jwt_bearer', alg: 'RS256', claims: { scope: 'drive.readonly' }, exchange: { kind: 'google' } }),
    'a Google access token for drive.readonly')
  assert.equal(sentAs({ type: 'jwt_bearer', alg: 'RS256', claims: { iss: '1' }, exchange: { kind: 'github', installation_id: '42' } }),
    'a GitHub App token for installation 42')
  assert.equal(sentAs({ type: 'jwt_bearer', alg: 'ES256', claims: { iss: 'x', aud: 'appstoreconnect-v1' } }),
    'a token signed ES256 for appstoreconnect-v1')
})

test('recipes name parts by their labels when the card lists them', () => {
  assert.equal(sentAs(BASIC, PARTS), 'Authorization: Basic (Account SID and token)')
  assert.equal(sentAs({ type: 'slots', slots: [{ type: 'header', name: 'X-Key', template: 'Token {account}:{token}' }] }, PARTS),
    'X-Key: Token {Account SID}:{token}')
  assert.equal(sentAs({ type: 'jwt_bearer', alg: 'RS256', claims: { iss: 'bot' } }), 'a token signed RS256, issued by bot')
})

test('a card that would mint a token shows every claim, so an act-as-user sub is never hidden', () => {
  assert.equal(tokenClaims({ type: 'jwt_bearer', claims: { scope: 'drive', sub: 'ceo@example.com' }, exchange: { kind: 'google' } }),
    'scope drive · sub ceo@example.com · exchanged with Google')
  assert.equal(tokenClaims({ type: 'jwt_bearer', claims: { iss: 'i', aud: 'a' }, key_id: 'K1' }), 'iss i · aud a · key id K1 · sent as it is')
  assert.equal(tokenClaims(BASIC), null)
})

test('parts that are not secret are named before the owner pastes into them', () => {
  assert.equal(plainPartsNote(PARTS), "Account SID isn't secret: shown as typed and not hidden from answers. Paste only what is meant to be seen there.")
  assert.equal(plainPartsNote([PARTS[1]]), null)
})

test('a card for a storage key says its own access policy is what limits a signed request', () => {
  assert.match(signedRequestLimit({ type: 'aws_sigv4', service: 's3', region: 'auto' }), /key's own policy decides what a signed request may do/)
  assert.equal(signedRequestLimit(BASIC), null)
  assert.equal(signedRequestLimit(null), null)
})

const SIGN_IN = {
  type: 'oauth2_refresh', client_id: 'client_id', client_secret: 'client_secret', refresh_token: 'refresh_token',
  client_auth: 'post', token_endpoint: 'https://oauth2.googleapis.com/token', revoke_endpoint: 'https://oauth2.googleapis.com/revoke',
}
const CLIENT = [{ name: 'client_id', label: 'Client ID', secret: false }, { name: 'client_secret', label: 'Client secret', secret: true }]

test('a sign-in key reads as a sign-in the vault renews, with the endpoints Allow approves', () => {
  assert.equal(sentAs(SIGN_IN), 'a sign-in renewed by the vault at oauth2.googleapis.com')
  assert.equal(signInEndpoints(SIGN_IN),
    'renewed at https://oauth2.googleapis.com/token · signed out at https://oauth2.googleapis.com/revoke · client sent in the form')
  assert.equal(signInEndpoints({ ...SIGN_IN, revoke_endpoint: undefined, client_auth: 'basic' }),
    'renewed at https://oauth2.googleapis.com/token · client sent as Basic')
  assert.match(signInNote(SIGN_IN), /whoever signs in decides which account this key acts as/)
  assert.equal(signInEndpoints(BASIC), null)
  assert.equal(signInNote(BASIC), null)
})

test('a new sign-in key is pasted as its client only, never a refresh token', () => {
  assert.deepEqual(newKeyParts(ask({ inject: SIGN_IN, parts: CLIENT })).map(part => part.name), ['client_id', 'client_secret'])
})

test('a sign-in key says on its page whether it is signed in', () => {
  const key = { name: 'gmail', inject: SIGN_IN, parts: CLIENT }
  assert.deepEqual(signInState(key), {
    signedIn: false, part: 'refresh_token', line: 'Not signed in yet. Sign in from the app that uses it.',
  })
  const signedIn = signInState({ ...key, parts: [...CLIENT, { name: 'refresh_token', label: 'Sign-in', secret: true }] })
  assert.equal(signedIn.signedIn, true)
  assert.equal(signedIn.line, 'Signed in · renewed by the vault at oauth2.googleapis.com')
  assert.equal(signInState({ name: 'x', inject: BASIC, parts: PARTS }), null)
})

test("a sign-in's changes read as sentences in the key's activity", () => {
  const at = '2026-11-27T18:00:00Z'
  assert.equal(usageWhat({ caller: 'app:8:x', outcome: 'signed in', host: '-', ts: at }, { 8: 'Mail' }), 'Mail signed it in')
  assert.equal(usageWhat({ caller: 'owner', outcome: 'signed out', host: '-', ts: at }, {}), 'You signed it out')
  assert.equal(usageWhat({ caller: 'platform:oauth-refresh', outcome: 'sign-in renewed', host: '-', ts: at }, {}),
    'Möbius renewed its sign-in')
})
