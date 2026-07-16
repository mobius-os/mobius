/**
 * Unit tests for setupSession.js.
 *
 * Run with:
 *   cd frontend && node --test src/lib/__tests__/setupSession.test.js
 *
 * No React loader required — setupSession is plain ESM that only
 * touches localStorage / sessionStorage. We provide minimal global
 * stubs so the module works under Node.
 */
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'

// Storage stub shared by both window.localStorage and sessionStorage.
function makeStorage() {
  const map = new Map()
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => { map.set(k, String(v)) },
    removeItem: (k) => { map.delete(k) },
    clear: () => { map.clear() },
    _raw: map,
  }
}

// Throwing storage to simulate Safari ITP / private browsing — every
// access throws. setupSession must swallow.
function makeThrowingStorage() {
  return {
    getItem: () => { throw new Error('ITP') },
    setItem: () => { throw new Error('ITP') },
    removeItem: () => { throw new Error('ITP') },
    clear: () => { throw new Error('ITP') },
  }
}

let localStorageStub
let sessionStorageStub
let setupSession

async function reloadModule() {
  // Bust ESM cache so the module-level `let _inProgress` re-reads
  // sessionStorage at import time for each test.
  const url = new URL('../setupSession.js', import.meta.url).href
    + `?t=${Math.random()}`
  setupSession = await import(url)
}

beforeEach(async () => {
  localStorageStub = makeStorage()
  sessionStorageStub = makeStorage()
  globalThis.localStorage = localStorageStub
  globalThis.sessionStorage = sessionStorageStub
  await reloadModule()
})

test('getResumeStep returns null when nothing saved', () => {
  assert.equal(setupSession.getResumeStep(), null)
})

test('getResumeStep returns saved provider step', () => {
  localStorageStub.setItem('setup-step', 'provider')
  assert.equal(setupSession.getResumeStep(), 'provider')
})

test('getResumeStep clears retired or unknown step values', () => {
  // Defensive: a future SetupWizard edit could try to save a step
  // name the resumer doesn't recognise. Returning null forces a
  // clean start instead of crashing AppRoot's initial render.
  localStorageStub.setItem('setup-step', 'something-else')
  assert.equal(setupSession.getResumeStep(), null)
  assert.equal(localStorageStub.getItem('setup-step'), null)
})

test('saveStep writes provider step', () => {
  setupSession.saveStep('provider')
  assert.equal(localStorageStub.getItem('setup-step'), 'provider')
})

test('saveStep is a no-op for "account"', () => {
  // The 'account' step has no token yet — nothing meaningful to
  // resume to. Persisting it would just confuse the next visit.
  setupSession.saveStep('account')
  assert.equal(localStorageStub.getItem('setup-step'), null)
})

test('clearResumeStep removes the saved step', () => {
  setupSession.saveStep('provider')
  setupSession.clearResumeStep()
  assert.equal(localStorageStub.getItem('setup-step'), null)
})

test('setInProgress(true) round-trips through sessionStorage', () => {
  setupSession.setInProgress(true)
  assert.equal(setupSession.isInProgress(), true)
  assert.equal(sessionStorageStub.getItem('mobius-setup-in-progress'), '1')
})

test('setInProgress(false) clears sessionStorage', () => {
  setupSession.setInProgress(true)
  setupSession.setInProgress(false)
  assert.equal(setupSession.isInProgress(), false)
  assert.equal(sessionStorageStub.getItem('mobius-setup-in-progress'), null)
})

test('setInProgress coerces truthy/falsy to booleans', () => {
  setupSession.setInProgress('yes')
  assert.equal(setupSession.isInProgress(), true)
  setupSession.setInProgress(0)
  assert.equal(setupSession.isInProgress(), false)
})

test('isInProgress reads sessionStorage on module load', async () => {
  // Simulates a refresh during the wizard-to-shell transition:
  // sessionStorage already has the flag set before module init.
  sessionStorageStub.setItem('mobius-setup-in-progress', '1')
  await reloadModule()
  assert.equal(setupSession.isInProgress(), true)
})

test('Safari ITP — storage that throws does not crash any helper', async () => {
  globalThis.localStorage = makeThrowingStorage()
  globalThis.sessionStorage = makeThrowingStorage()
  await reloadModule()
  // None of these should throw.
  assert.equal(setupSession.getResumeStep(), null)
  setupSession.saveStep('provider')
  setupSession.clearResumeStep()
  setupSession.setInProgress(true)
  assert.equal(setupSession.isInProgress(), true)  // module-level let still works
  setupSession.setInProgress(false)
  assert.equal(setupSession.isInProgress(), false)
})

test('Safari ITP — module init read failure defaults to false', async () => {
  globalThis.sessionStorage = makeThrowingStorage()
  await reloadModule()
  assert.equal(setupSession.isInProgress(), false)
})
