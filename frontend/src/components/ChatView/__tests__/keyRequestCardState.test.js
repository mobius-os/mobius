import { test } from 'node:test'
import assert from 'node:assert/strict'
import { KEY_ALLOW, KEY_DENY, agentsMayUse, keyAnswerAlreadyGiven, keyRequestCardState } from '../keyRequestCardState.js'

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
  assert.equal(keyAnswerAlreadyGiven('denied', KEY_DENY), true)
  assert.equal(keyAnswerAlreadyGiven('denied', KEY_ALLOW), null)
  // A request no longer waiting needs no vault call, and only the true answer
  // goes on: Allow where the vault already lets the agents use the key.
  assert.equal(keyAnswerAlreadyGiven('allowed', KEY_ALLOW), true)
  assert.equal(keyAnswerAlreadyGiven('allowed', KEY_DENY), null)
  assert.equal(keyAnswerAlreadyGiven('gone', KEY_DENY), true)
  assert.equal(keyAnswerAlreadyGiven('gone', KEY_ALLOW), null)
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
})

const nasaKey = (sites, agents = true) => ({ name: 'nasa', grants: { agents, sites, apps: [] } })
const vault = keyList => ({ data: { available: true, keys: keyList, requests: [] } })

test("a request no longer waiting says whether the vault already lets the agents use the key there, as the server checks", () => {
  const asked = { ...card, key: 'nasa' }
  assert.equal(keyRequestCardState({ ...asked, keys: vault([nasaKey([APOD])]) }).state, 'allowed')
  assert.equal(keyRequestCardState({ ...asked, keys: vault([nasaKey([APOD], false)]) }).state, 'gone')
  assert.equal(keyRequestCardState({ ...asked, keys: vault([]) }).state, 'gone')
  // Wider sites cover it: a `*` path, a `*.` host.
  assert.equal(agentsMayUse([nasaKey([{ ...APOD, paths: ['/planetary/*'] }])], 'nasa', [APOD]), true)
  assert.equal(agentsMayUse([nasaKey([{ ...APOD, host: '*.nasa.gov', paths: ['/*'] }])], 'nasa', [APOD]), true)
  // Every host, method and path must be covered.
  assert.equal(agentsMayUse([nasaKey([{ ...APOD, paths: ['/other/*'] }])], 'nasa', [APOD]), false)
  assert.equal(agentsMayUse([nasaKey([APOD])], 'nasa', [{ ...APOD, methods: ['GET', 'POST'] }]), false)
  assert.equal(agentsMayUse([nasaKey([APOD])], 'nasa', [APOD, COLLECT]), false)
  assert.equal(agentsMayUse([nasaKey([{ ...APOD, host: '*.nasa.gov' }])], 'nasa', [{ ...APOD, host: 'nasa.gov' }]), false)
  // A dot in a path is a dot, not any character.
  assert.equal(agentsMayUse([nasaKey([{ ...APOD, paths: ['/a.json'] }])], 'nasa', [{ ...APOD, paths: ['/abjson'] }]), false)
})
