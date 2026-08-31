import assert from 'node:assert/strict'
import test from 'node:test'

import { createMediaSessionOwner } from '../mediaSessionOwner.js'

function event(kind, sessionId, playbackState = 'playing', playbackRate) {
  return {
    event: kind,
    sessionId,
    title: `Title ${sessionId}`,
    playbackState,
    ...(playbackRate === undefined ? {} : { playbackRate }),
  }
}

test('a newer app session stops and replaces the previous playback lease', () => {
  const changes = []
  const controls = []
  const owner = createMediaSessionOwner(value => changes.push(value))

  owner.receive(1, event('open', 'one'), action => controls.push(['one', action]) || true)
  owner.receive(2, event('open', 'two'), action => controls.push(['two', action]) || true)

  assert.deepEqual(controls, [['one', 'stop']])
  assert.equal(changes.at(-1).appId, 2)
  assert.equal(changes.at(-1).sessionId, 'two')
})

test('late updates and closes cannot take over or clear the current lease', () => {
  const changes = []
  const owner = createMediaSessionOwner(value => changes.push(value))
  owner.receive(1, event('open', 'one'), () => true)
  owner.receive(2, event('open', 'two'), () => true)

  assert.equal(owner.receive(1, event('update', 'one', 'paused'), () => true), false)
  assert.equal(owner.receive(1, event('close', 'one'), () => true), false)
  assert.equal(changes.at(-1).sessionId, 'two')
})

test('stop remains app-confirmed and a failed delivery keeps controls available', () => {
  const changes = []
  const controls = []
  const owner = createMediaSessionOwner(value => changes.push(value))
  owner.receive(1, event('open', 'one'), action => { controls.push(action); return false })

  assert.equal(owner.control('stop'), false)
  assert.deepEqual(controls, ['stop'])
  assert.equal(changes.at(-1).sessionId, 'one')

  assert.equal(owner.receive(1, event('close', 'one'), () => true), true)
  assert.equal(changes.at(-1), null)
})

test('speed metadata persists across state updates and enables one cycle control', () => {
  const changes = []
  const controls = []
  const owner = createMediaSessionOwner(value => changes.push(value))

  const sendControl = (action) => { controls.push(action); return true }
  owner.receive(1, event('open', 'one', 'loading', 1.5), sendControl)
  owner.receive(1, event('update', 'one', 'playing'), sendControl)

  assert.equal(changes.at(-1).playbackRate, 1.5)
  assert.equal(owner.control('cycle-speed'), true)
  assert.deepEqual(controls, ['cycle-speed'])
})
