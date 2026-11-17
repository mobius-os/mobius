import { test } from 'node:test'
import assert from 'node:assert/strict'
import { KEY_ALLOW, KEY_DENY, keyAnswerAlreadyGiven, keyRequestCardState } from '../keyRequestCardState.js'

const pending = { id: 'agent-nasa', status: 'pending', key: 'nasa' }
const keys = requests => ({ data: { available: true, keys: [], requests } })

test('a saved key card reads its own request from the vault', () => {
  assert.equal(keyRequestCardState({ id: 'agent-nasa', owner: true, keys: keys([pending]) }).state, 'pending')
  assert.equal(keyRequestCardState({ id: 'agent-nasa', owner: true, keys: keys([{ ...pending, status: 'denied' }]) }).state, 'denied')
  // An allowed request leaves the vault.
  assert.equal(keyRequestCardState({ id: 'agent-nasa', owner: true, keys: keys([]) }).state, 'gone')
  assert.equal(keyRequestCardState({ id: 'agent-nasa', owner: false, keys: keys([pending]) }).state, 'guest')
  assert.equal(keyRequestCardState({ id: 'agent-nasa', owner: true, keys: {} }).state, 'loading')
  // A card that is no longer live reads nothing, so it never sits on Loading.
  assert.equal(keyRequestCardState({ id: 'agent-nasa', owner: true, live: false, keys: {} }).state, 'idle')
  assert.equal(keyRequestCardState({ id: 'agent-nasa', owner: true, keys: { data: { available: false } } }).state, 'unavailable')
  assert.equal(keyRequestCardState({ id: 'agent-nasa', owner: true, keys: { error: new Error('x') } }).state, 'error')
})

test('a retried answer needs no second vault call, and a contradicting one is refused', () => {
  assert.equal(keyAnswerAlreadyGiven('pending', KEY_ALLOW), false)
  assert.equal(keyAnswerAlreadyGiven('gone', KEY_ALLOW), true)
  assert.equal(keyAnswerAlreadyGiven('denied', KEY_DENY), true)
  // A request no longer waiting needs no vault call; the server admits only the true answer.
  assert.equal(keyAnswerAlreadyGiven('gone', KEY_DENY), true)
  assert.equal(keyAnswerAlreadyGiven('denied', KEY_ALLOW), null)
  assert.equal(keyAnswerAlreadyGiven('loading', KEY_ALLOW), null)
})

const APOD = { host: 'api.nasa.gov', methods: ['GET'], paths: ['/planetary/apod'] }
const COLLECT = { host: 'collect.example.net', methods: ['POST'], paths: ['/*'] }
const card = { id: 'agent-nasa', sites: [APOD], owner: true }

test('a card answers only the request it names', () => {
  assert.equal(keyRequestCardState({ ...card, keys: keys([{ ...pending, sites: [APOD] }]) }).state, 'pending')
  // The same sites with their fields in another order are still this card's request.
  const reordered = { paths: APOD.paths, methods: APOD.methods, host: APOD.host }
  assert.equal(keyRequestCardState({ ...card, keys: keys([{ ...pending, sites: [reordered] }]) }).state, 'pending')
  assert.equal(keyRequestCardState({ ...card, keys: keys([{ ...pending, sites: [APOD], status: 'denied' }]) }).state, 'denied')
  // The same id asked again with other sites is not this card's request.
  assert.deepEqual(keyRequestCardState({ ...card, keys: keys([{ ...pending, sites: [COLLECT] }]) }), { state: 'gone', request: null })
  assert.equal(keyRequestCardState({ ...card, keys: keys([]) }).state, 'gone')
  // Either answer goes to the server, which admits only the true one.
  assert.equal(keyAnswerAlreadyGiven('gone', KEY_ALLOW), true)
  assert.equal(keyAnswerAlreadyGiven('gone', KEY_DENY), true)
})
