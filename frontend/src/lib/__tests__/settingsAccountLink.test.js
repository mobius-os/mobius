import { test } from 'node:test'
import assert from 'node:assert/strict'
import { waitForAccountLink } from '../../components/SettingsView/identity/identity-contract.js'

const state = 's'.repeat(32)
const code = 'c'.repeat(32)
const attempt = {
  state,
  authorization_url: `https://account.example/link?state=${state}`,
  authorization_origin: 'https://account.example',
  expires_at: '2030-01-01T00:00:00Z',
}

function shell() {
  const listeners = new Set()
  const navigations = []
  let closes = 0
  const popup = { closed: false, close() { closes++ }, location: { replace(url) { navigations.push(url) } } }
  const target = {
    location: { origin: 'https://shell.example' },
    addEventListener(type, listener) { listeners.add(listener) },
    removeEventListener(type, listener) { listeners.delete(listener) },
    // A native shell is not an attributed app frame: no AppCanvas registration ack.
    postMessage() {},
  }
  target.parent = target
  Object.defineProperty(globalThis, 'window', { configurable: true, value: target })
  return { popup, target, listeners, navigations, get closes() { return closes },
    emit(data, origin = attempt.authorization_origin, source = popup) {
      for (const listener of listeners) listener({ data, origin, source })
    },
  }
}

// The shell supplies the production defaults, including parent === self.
function setup(t) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window')
  t.after(() => previous ? Object.defineProperty(globalThis, 'window', previous) : delete globalThis.window)
  return shell()
}

test('native Settings navigates without an iframe registration ack and consumes external completion', async t => {
  const s = setup(t)
  const waiting = waitForAccountLink({ popup: s.popup, attempt, registrationTimeoutMs: 10 })
  s.emit({ type: 'mobius-account-link', state, code })
  assert.deepEqual(await waiting, { code, state })
  assert.deepEqual(s.navigations, [attempt.authorization_url])
  assert.equal(s.listeners.size, 0)
  assert.equal(s.closes, 1)
})

test('native Settings rejects forged origin, state, shape, code and popup identity', async t => {
  const s = setup(t)
  const controller = new AbortController()
  const waiting = waitForAccountLink({ popup: s.popup, attempt, signal: controller.signal })
  const valid = { type: 'mobius-account-link', state, code }
  s.emit(valid, 'https://evil.example')
  s.emit(valid, attempt.authorization_origin, {})
  s.emit({ ...valid, state: 'x'.repeat(32) })
  s.emit({ ...valid, extra: true })
  s.emit({ ...valid, code: 'bad code' })
  s.emit({ type: 'moebius:account-link-result', state, code, authorizationOrigin: attempt.authorization_origin }, 'https://shell.example', s.target)
  assert.equal(s.listeners.size, 1)
  controller.abort()
  await assert.rejects(waiting, /cancelled/)
  assert.equal(s.listeners.size, 0)
  assert.equal(s.closes, 1)
  s.emit(valid)
  assert.equal(s.closes, 1)
})

test('an already aborted native sign-in never navigates', async t => {
  const s = setup(t)
  const controller = new AbortController()
  controller.abort()
  await assert.rejects(waitForAccountLink({ popup: s.popup, attempt, signal: controller.signal }), /cancelled/)
  assert.equal(s.navigations.length, 0)
  assert.equal(s.listeners.size, 0)
})

test('closing the native sign-in popup removes the completion listener', async t => {
  const s = setup(t)
  const waiting = waitForAccountLink({ popup: s.popup, attempt, closedPollMs: 1 })
  s.popup.closed = true
  await assert.rejects(waiting, /window was closed/)
  assert.equal(s.listeners.size, 0)
})

test('native completion remains bounded by the broker lifetime despite a skewed clock', async t => {
  const s = setup(t)
  let time = 100
  const controller = new AbortController()
  const waiting = waitForAccountLink({ popup: s.popup, attempt, now: () => time, signal: controller.signal })
  time += 10 * 60 * 1000 + 1
  s.emit({ type: 'mobius-account-link', state, code })
  assert.equal(s.listeners.size, 1)
  controller.abort()
  await assert.rejects(waiting, /cancelled/)
})

test('native sign-in rejects invalid registration and cleans up navigation failure', async t => {
  const s = setup(t)
  await assert.rejects(waitForAccountLink({ popup: s.popup, attempt: { ...attempt, state: 'short' } }), /prepare secure/)
  assert.equal(s.navigations.length, 0)
  assert.equal(s.listeners.size, 0)
  s.popup.location.replace = () => { throw new Error('window gone') }
  await assert.rejects(waitForAccountLink({ popup: s.popup, attempt }), /could not be opened/)
  assert.equal(s.listeners.size, 0)
})
