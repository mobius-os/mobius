import { test } from 'node:test'
import assert from 'node:assert/strict'
import { acquireAudioSession } from '../audioSession.js'

function navigatorWithSession(type = 'auto') {
  const changes = []
  return {
    changes,
    audioSession: {
      get type() { return type },
      set type(next) { changes.push(next); type = next },
    },
  }
}

test('app host selects playback without starting or unmuting any audio', () => {
  const nav = navigatorWithSession()
  const release = acquireAudioSession('playback', nav)
  assert.equal(nav.audioSession.type, 'playback')
  release()
  assert.deepEqual(nav.changes, ['playback', 'auto'])
  release()
  assert.deepEqual(nav.changes, ['playback', 'auto'])
})

test('multiple canvases keep playback until the last one leaves', () => {
  const nav = navigatorWithSession()
  const first = acquireAudioSession('playback', nav)
  const second = acquireAudioSession('playback', nav)
  first()
  assert.equal(nav.audioSession.type, 'playback')
  second()
  assert.deepEqual(nav.changes, ['playback', 'auto'])
})

test('capture wins over app playback and restores it when all captures end', () => {
  const nav = navigatorWithSession()
  const app = acquireAudioSession('playback', nav)
  const mic = acquireAudioSession('play-and-record', nav)
  const camera = acquireAudioSession('play-and-record', nav)
  const otherApp = acquireAudioSession('playback', nav)
  assert.equal(nav.audioSession.type, 'play-and-record')
  mic()
  assert.equal(nav.audioSession.type, 'play-and-record')
  camera()
  assert.equal(nav.audioSession.type, 'playback')
  app()
  otherApp()
  assert.deepEqual(nav.changes, ['playback', 'play-and-record', 'playback', 'auto'])
})

test('unmounting the last app does not interrupt a capture still in progress', () => {
  const nav = navigatorWithSession()
  const app = acquireAudioSession('playback', nav)
  const mic = acquireAudioSession('play-and-record', nav)
  app()
  assert.equal(nav.audioSession.type, 'play-and-record')
  mic()
  assert.equal(nav.audioSession.type, 'auto')
})

test('standalone capture restores the original browser category', () => {
  const nav = navigatorWithSession('ambient')
  const mic = acquireAudioSession('play-and-record', nav)
  assert.equal(nav.audioSession.type, 'play-and-record')
  mic()
  assert.equal(nav.audioSession.type, 'ambient')
})

test('app playback preserves an existing explicit category', () => {
  for (const type of ['ambient', 'transient', 'playback', 'play-and-record']) {
    const nav = navigatorWithSession(type)
    const release = acquireAudioSession('playback', nav)
    assert.equal(nav.audioSession.type, type)
    release()
    assert.deepEqual(nav.changes, [])
  }
})

test('cleanup does not undo a newer explicit category chosen elsewhere', () => {
  const nav = navigatorWithSession()
  const release = acquireAudioSession('playback', nav)
  nav.audioSession.type = 'ambient'
  release()
  assert.equal(nav.audioSession.type, 'ambient')
})

test('absent or inaccessible optional browser support never blocks the app', () => {
  for (const nav of [null, {}, {
    get audioSession() { throw new Error('unavailable') },
  }, {
    audioSession: { get type() { throw new Error('unavailable') } },
  }, {
    audioSession: { get type() { return 'auto' }, set type(value) { throw new Error(value) } },
  }]) {
    const release = acquireAudioSession('playback', nav)
    assert.doesNotThrow(release)
  }
})
