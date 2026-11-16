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
  assert.equal(keyAnswerAlreadyGiven('gone', KEY_DENY), null)
  assert.equal(keyAnswerAlreadyGiven('denied', KEY_ALLOW), null)
  assert.equal(keyAnswerAlreadyGiven('loading', KEY_ALLOW), null)
})
