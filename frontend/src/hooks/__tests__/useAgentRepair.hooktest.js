import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'
import {
  errorRecoveryFingerprint,
  writeErrorRecoveryAttempt,
} from '../../lib/errorRecovery.js'
import useAgentRepair from '../useAgentRepair.js'

function memoryStorage() {
  const values = new Map()
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  }
}

// The hook reads the ledger through the module defaults and listens on the
// real window, so the test stands in for both.
function installBrowser(t) {
  const listeners = new Map()
  const assigned = []
  const previous = {
    window: globalThis.window,
    sessionStorage: globalThis.sessionStorage,
  }
  const client = { chats: {} }
  globalThis.sessionStorage = memoryStorage()
  globalThis.window = {
    addEventListener: (type, fn) => listeners.set(type, fn),
    removeEventListener: (type, fn) => { if (listeners.get(type) === fn) listeners.delete(type) },
    location: { assign: path => assigned.push(path), pathname: '/' },
  }
  t.after(() => {
    globalThis.window = previous.window
    globalThis.sessionStorage = previous.sessionStorage
  })
  return {
    client,
    listeners,
    assigned,
    storage: globalThis.sessionStorage,
    stubChats(chats) { client.chats = chats },
  }
}

const SURFACE = 'chat:one'

test('a surface with nothing to repair has no attempt and no repair', async (t) => {
  const browser = installBrowser(t)
  let created = 0
  browser.stubChats({ create: async () => { created += 1 }, send: async () => ({ ok: true }) })

  const { result } = renderHook(useAgentRepair, {
    surfaceKey: SURFACE, fingerprint: null, prompt: 'fix',
    repairTransport: () => ({ client: browser.client, base: '' }),
  })

  assert.equal(result.current.attempt, null)
  await result.current.repair()
  assert.equal(result.current.repairActive, false)
  assert.equal(created, 0)
  assert.equal(browser.listeners.has('pageshow'), false)
})

test('a new failure re-reads the ledger for its own fingerprint', (t) => {
  const browser = installBrowser(t)
  const first = errorRecoveryFingerprint(SURFACE, 'first crash')
  const second = errorRecoveryFingerprint(SURFACE, 'second crash')

  const { result, rerender } = renderHook(useAgentRepair, {
    surfaceKey: SURFACE, fingerprint: first, prompt: 'fix',
    repairTransport: () => ({ client: browser.client, base: '' }),
  })
  assert.equal(result.current.attempt, null)

  // The surface's ledger entry (one per surface) now belongs to a later crash,
  // as after a refresh that crashed differently.
  writeErrorRecoveryAttempt({
    storage: browser.storage, surfaceKey: SURFACE, fingerprint: second,
    phase: 'refreshed', now: Date.now(),
  })
  rerender({
    surfaceKey: SURFACE, fingerprint: first, prompt: 'fix',
    repairTransport: () => ({ client: browser.client, base: '' }),
  })
  assert.equal(result.current.attempt, null, 'the same failure keeps its read')
  rerender({
    surfaceKey: SURFACE, fingerprint: second, prompt: 'fix',
    repairTransport: () => ({ client: browser.client, base: '' }),
  })
  assert.equal(result.current.attempt?.phase, 'refreshed')
})

test('one repair runs at a time; a bfcache restore drops it and re-reads the ledger', async (t) => {
  const browser = installBrowser(t)
  const fingerprint = errorRecoveryFingerprint(SURFACE, 'crash')
  let created = 0
  let abortSignal
  browser.stubChats({
    create: (_payload, { signal }) => {
      created += 1
      abortSignal = signal
      return new Promise((_resolve, reject) => {
        signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
      })
    },
    send: async () => ({ ok: true }),
  })

  const { result } = renderHook(useAgentRepair, {
    surfaceKey: SURFACE, fingerprint, prompt: 'fix',
    repairTransport: () => ({ client: browser.client, base: '' }),
  })
  const firstRun = result.current.repair()
  const secondRun = result.current.repair()
  assert.equal(created, 1)
  assert.equal(result.current.repairActive, true)
  assert.equal(result.current.attempt?.phase, 'agent-starting')

  browser.listeners.get('pageshow')({ persisted: true })
  assert.equal(abortSignal.aborted, true)
  assert.equal(result.current.repairActive, false)
  assert.equal(result.current.attempt?.phase, 'agent-starting',
    'the restore shows the ledger the departed navigation left behind')

  await Promise.all([firstRun, secondRun])
  assert.equal(result.current.repairActive, false)
  assert.deepEqual(browser.assigned, [], 'an aborted repair navigates nowhere')
})

test('a client startup failure is visible even before a request is recorded', async t => {
  installBrowser(t)
  const { result } = renderHook(useAgentRepair, {
    surfaceKey: SURFACE, prompt: 'help',
    repairTransport: () => { throw new Error('client load failed') },
  })
  await result.current.repair()
  assert.equal(result.current.repairActive, false)
  assert.match(result.current.error, /Couldn’t open the chat/)
})

test('a restored page cannot navigate from a response that settled just before cancellation', async t => {
  const browser = installBrowser(t)
  let finishSend
  browser.stubChats({
    create: async () => ({ ok: true, json: async () => ({ id: 'chat-one' }) }),
    send: () => new Promise(resolve => { finishSend = resolve }),
  })
  const { result } = renderHook(useAgentRepair, {
    surfaceKey: SURFACE, prompt: 'help',
    repairTransport: () => ({ client: browser.client, base: '' }),
  })
  const running = result.current.repair()
  // Settle the already-started asynchronous create without depending on timing.
  for (let i = 0; i < 8 && !finishSend; i += 1) await Promise.resolve()
  assert.equal(typeof finishSend, 'function')
  finishSend({ ok: true })
  browser.listeners.get('pageshow')({ persisted: true })
  await running
  assert.deepEqual(browser.assigned, [])
  assert.equal(result.current.repairActive, false)
})
