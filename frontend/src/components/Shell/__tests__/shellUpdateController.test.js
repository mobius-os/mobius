import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as paneModel from '../paneModel.js'
import useShellUpdateController, {
  deriveShellReloadState,
} from '../useShellUpdateController.js'
import { renderHook } from '../../ChatView/hooks/__tests__/react-hook-shim.mjs'

const tick = () => new Promise(resolve => setImmediate(resolve))

function controllerHarness({ registration = null } = {}) {
  const ref = current => ({ current })
  const docListeners = new Map()
  const winListeners = new Map()
  const replacements = []
  const stored = new Map()
  const dispatched = []
  let persisted = 0
  const doc = {
    visibilityState: 'visible',
    addEventListener(type, fn) {
      if (!docListeners.has(type)) docListeners.set(type, new Set())
      docListeners.get(type).add(fn)
    },
    removeEventListener(type, fn) { docListeners.get(type)?.delete(fn) },
    emit(type) { for (const fn of docListeners.get(type) || []) fn() },
  }
  const win = {
    Event: class Event { constructor(type) { this.type = type } },
    addEventListener(type, fn) {
      if (!winListeners.has(type)) winListeners.set(type, new Set())
      winListeners.get(type).add(fn)
    },
    removeEventListener(type, fn) { winListeners.get(type)?.delete(fn) },
    emit(type) { for (const fn of winListeners.get(type) || []) fn() },
    dispatchEvent(event) { dispatched.push(event.type) },
    location: { replace(url) { replacements.push(url) } },
  }
  const activeWorker = { id: 'active' }
  const serviceWorker = {
    controller: activeWorker,
    async getRegistration() { return registration },
  }
  const inputs = {
    win,
    doc,
    nav: { onLine: true, serviceWorker },
    storage: {
      getItem: key => stored.get(key) ?? null,
      setItem: (key, value) => { stored.set(key, String(value)) },
      removeItem: key => { stored.delete(key) },
    },
    queryClient: {
      getMutationCache: () => ({ getAll: () => [] }),
      getQueryCache: () => ({ getAll: () => [] }),
    },
    persistWorkspaceSnapshot() { persisted += 1 },
    workspaceStateRef: ref({ ws: paneModel.seedFromFlatTabs([]) }),
    activeViewRef: ref('settings'),
    drawerOpenRef: ref(false),
  }
  return {
    doc,
    win,
    serviceWorker,
    inputs,
    replacements,
    stored,
    dispatched,
    persisted: () => persisted,
  }
}

test('reload snapshot derives content from the current workspace authority', () => {
  const workspace = {
    ...paneModel.seedFromFlatTabs([
      { kind: 'chat', id: 'older' },
      { kind: 'app', id: 42 },
    ]),
    singleScreen: { kind: 'app', id: '42' },
  }

  assert.deepEqual(deriveShellReloadState({
    workspace,
    activeView: 'canvas',
    drawerOpen: true,
  }), {
    activeView: 'canvas',
    activeAppId: 42,
    activeChatId: null,
    drawerOpen: true,
  })
})

test('settings takeover changes only the reload surface, not workspace content ids', () => {
  const workspace = {
    ...paneModel.seedFromFlatTabs([{ kind: 'chat', id: 'kept' }]),
    singleScreen: { kind: 'chat', id: 'kept' },
  }

  assert.deepEqual(deriveShellReloadState({
    workspace,
    activeView: 'settings',
    drawerOpen: false,
  }), {
    activeView: 'settings',
    activeAppId: null,
    activeChatId: 'kept',
    drawerOpen: false,
  })
})

test('many rebuild signals coalesce without navigating', () => {
  const harness = controllerHarness()
  const { result } = renderHook(useShellUpdateController, harness.inputs)

  result.current.markShellUpdateAvailable()
  result.current.markShellUpdateAvailable()
  result.current.markShellUpdateAvailable()

  assert.equal(result.current.updateAvailable, true)
  assert.deepEqual(harness.replacements, [])
  assert.equal(harness.stored.has('shell-reload'), false)
  assert.equal(harness.persisted(), 0)
})

test('a resume-time generation becomes available without navigating', async () => {
  const active = { id: 'active' }
  const harness = controllerHarness({
    registration: {
      active,
      waiting: { id: 'waiting' },
      installing: null,
      async update() {},
      addEventListener() {},
      removeEventListener() {},
    },
  })
  harness.serviceWorker.controller = active
  const { result } = renderHook(useShellUpdateController, harness.inputs)

  harness.doc.emit('visibilitychange')
  await tick()
  await tick()

  assert.equal(result.current.updateAvailable, true)
  assert.deepEqual(harness.replacements, [])
})

test('one explicit update writes the latest workspace and navigates exactly once', async () => {
  const previousHistory = globalThis.history
  globalThis.history = { state: null, replaceState() {} }
  const waiting = { messages: [], postMessage(message) { this.messages.push(message) } }
  const active = { id: 'active' }
  const registration = {
    active,
    waiting,
    installing: null,
    async update() {},
    addEventListener() {},
    removeEventListener() {},
  }
  try {
    const harness = controllerHarness({ registration })
    harness.serviceWorker.controller = active
    const { result } = renderHook(useShellUpdateController, harness.inputs)

    result.current.markShellUpdateAvailable()
    harness.inputs.workspaceStateRef.current = {
      ws: {
        ...paneModel.seedFromFlatTabs([{ kind: 'chat', id: 'latest-chat' }]),
        singleScreen: { kind: 'chat', id: 'latest-chat' },
      },
    }
    harness.inputs.activeViewRef.current = 'chat'

    const first = result.current.applyShellUpdate()
    const second = await result.current.applyShellUpdate()
    await first

    assert.equal(second, false, 'a second press cannot start another navigation')
    assert.deepEqual(harness.replacements, ['/shell/'])
    assert.equal(harness.persisted(), 1)
    assert.deepEqual(harness.dispatched, ['mobius:before-shell-reload'])
    assert.deepEqual(waiting.messages, [{ type: 'SKIP_WAITING' }])
    assert.equal(
      JSON.parse(harness.stored.get('shell-reload')).activeChatId,
      'latest-chat',
    )
  } finally {
    globalThis.history = previousHistory
  }
})

test('an explicit update activates its cross-document transition before replacement', async () => {
  const previousHistory = globalThis.history
  globalThis.history = { state: null, replaceState() {} }
  try {
    const harness = controllerHarness()
    const frames = []
    let prepared = 0
    harness.win.__mobiusPrepareShellReloadTransition = () => {
      prepared += 1
      return true
    }
    harness.win.requestAnimationFrame = callback => {
      frames.push(callback)
      return frames.length
    }
    const { result } = renderHook(useShellUpdateController, harness.inputs)

    assert.equal(await result.current.applyShellUpdate(), true)
    assert.equal(prepared, 1)
    assert.deepEqual(harness.replacements, [],
      'replacement must not race the dynamically inserted navigation rule')
    assert.equal(frames.length, 1)

    frames.shift()()
    assert.deepEqual(harness.replacements, ['/shell/'])
  } finally {
    globalThis.history = previousHistory
  }
})
