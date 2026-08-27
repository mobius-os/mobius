import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as paneModel from '../paneModel.js'
import useShellReloadController, {
  deriveShellReloadState,
} from '../useShellReloadController.js'
import { renderHook } from '../../ChatView/hooks/__tests__/react-hook-shim.mjs'


const tick = () => new Promise(resolve => setImmediate(resolve))

function controllerHarness() {
  const ref = current => ({ current })
  const winListeners = new Map()
  const replacements = []
  const stored = new Map()
  let scheduledChecks = 0
  const doc = {
    activeElement: null,
    visibilityState: 'visible',
    addEventListener() {},
    removeEventListener() {},
  }
  const win = {
    Event: class Event { constructor(type) { this.type = type } },
    addEventListener(type, fn) {
      if (!winListeners.has(type)) winListeners.set(type, new Set())
      winListeners.get(type).add(fn)
    },
    removeEventListener(type, fn) { winListeners.get(type)?.delete(fn) },
    emit(type) { for (const fn of winListeners.get(type) || []) fn() },
    clearTimeout() {},
    setTimeout() { scheduledChecks += 1; return scheduledChecks },
    dispatchEvent() {},
    location: { replace(url) { replacements.push(url) } },
  }
  return {
    win,
    inputs: {
      win,
      doc,
      nav: { onLine: true, serviceWorker: { getRegistration: async () => null } },
      storage: {
        getItem: key => stored.get(key) ?? null,
        setItem: (key, value) => { stored.set(key, String(value)) },
        removeItem: key => { stored.delete(key) },
      },
      queryClient: {
        getMutationCache: () => ({ getAll: () => [] }),
        getQueryCache: () => ({ getAll: () => [] }),
      },
      persistWorkspaceSnapshot() {},
      workspaceStateRef: ref({ ws: paneModel.seedFromFlatTabs([]) }),
      activeViewRef: ref('settings'),
      activeChatIdRef: ref(null),
      drawerOpenRef: ref(false),
      multiPaneBuilderVisibleRef: ref(false),
      streamingChatIdsRef: ref(new Set()),
      voiceDictationActiveRef: ref(false),
      activeView: 'settings',
      activeChatId: null,
      multiPaneBuilderVisible: false,
    },
    replacements,
    stored,
  }
}

test('reload snapshot derives content from the current workspace authority', () => {
  // In single mode the reload authority is the Standard SLOT (never the focused
  // Builder pane). The slot app is the reload surface; the tree chat is irrelevant.
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
  // The Standard slot is the kept chat; a Settings takeover changes only the reload
  // surface, leaving the slot's content ids intact.
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

test('a claimed Settings destination is serialized before the outgoing view paints', () => {
  const workspace = {
    ...paneModel.seedFromFlatTabs([{ kind: 'chat', id: 'kept' }]),
    singleScreen: { kind: 'chat', id: 'kept' },
  }

  assert.deepEqual(deriveShellReloadState({
    workspace,
    activeView: 'chat',
    drawerOpen: true,
    destination: { view: 'settings', chatId: 'kept', appId: null },
  }), {
    destinationClaimed: true,
    activeView: 'settings',
    activeAppId: null,
    activeChatId: 'kept',
    drawerOpen: false,
  })
})

test('a claimed content destination becomes the reload route', () => {
  const workspace = paneModel.seedFromFlatTabs([{ kind: 'chat', id: 'old' }])

  assert.deepEqual(deriveShellReloadState({
    workspace,
    activeView: 'chat',
    drawerOpen: true,
    destination: {
      view: 'chat', chatId: 'new', appId: null,
    },
  }), {
    destinationClaimed: true,
    activeView: 'chat',
    activeAppId: null,
    activeChatId: 'new',
    drawerOpen: false,
  })
})

test('an in-flight passive reload does not swallow disruptive chat navigation', () => {
  const ref = current => ({ current })
  const win = {
    Event: class {},
    addEventListener() {},
    removeEventListener() {},
    clearTimeout() {},
    setTimeout() { return 1 },
    dispatchEvent() {},
    location: { replace() {} },
  }
  const doc = {
    activeElement: null,
    visibilityState: 'visible',
    addEventListener() {},
    removeEventListener() {},
  }
  const registrationPending = new Promise(() => {})
  const inputs = {
    win,
    doc,
    nav: {
      onLine: true,
      serviceWorker: { getRegistration: () => registrationPending },
    },
    storage: { getItem: () => null, setItem() {}, removeItem() {} },
    queryClient: {},
    persistWorkspaceSnapshot() {},
    workspaceStateRef: ref({ ws: paneModel.seedFromFlatTabs([]) }),
    activeViewRef: ref('canvas'),
    activeChatIdRef: ref(null),
    drawerOpenRef: ref(false),
    multiPaneBuilderVisibleRef: ref(false),
    streamingChatIdsRef: ref(new Set()),
    voiceDictationActiveRef: ref(false),
    activeView: 'canvas',
    activeChatId: null,
    multiPaneBuilderVisible: false,
  }
  const { result } = renderHook(useShellReloadController, inputs)

  result.current.requestShellReload({ passive: true })

  assert.equal(result.current.claimPendingShellReloadNavigation({
    view: 'chat', chatId: 'next', appId: null,
  }), false)
})


test('a fresh press holds an old pending apply until its destination is claimed', async () => {
  const previousHistory = globalThis.history
  const previousNow = Date.now
  globalThis.history = { state: null, replaceState() {} }
  let now = 1000
  Date.now = () => now
  try {
    const harness = controllerHarness()
    const { result } = renderHook(useShellReloadController, harness.inputs)

    harness.win.emit('input')
    result.current.requestShellReload()
    now = 8000

    harness.win.emit('pointerdown')
    result.current.checkPendingShellReload()
    await tick()
    assert.equal(harness.replacements.length, 0)

    assert.equal(result.current.claimPendingShellReloadNavigation({
      view: 'chat', chatId: 'chosen-chat', appId: null,
    }), true)
    await tick()
    await tick()

    assert.deepEqual(harness.replacements, ['/shell/'])
    assert.equal(JSON.parse(harness.stored.get('shell-reload')).activeChatId, 'chosen-chat')
  } finally {
    Date.now = previousNow
    globalThis.history = previousHistory
  }
})

test('an expired non-navigation interaction cannot starve an old pending apply', async () => {
  const previousHistory = globalThis.history
  const previousNow = Date.now
  globalThis.history = { state: null, replaceState() {} }
  let now = 1000
  Date.now = () => now
  try {
    const harness = controllerHarness()
    const { result } = renderHook(useShellReloadController, harness.inputs)

    harness.win.emit('input')
    result.current.requestShellReload()
    now = 8000
    result.current.checkPendingShellReload()
    await tick()
    await tick()
    await tick()

    assert.deepEqual(harness.replacements, ['/shell/'])
  } finally {
    Date.now = previousNow
    globalThis.history = previousHistory
  }
})

test('a prepared cross-document transition activates before replacement', async () => {
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
    const { result } = renderHook(useShellReloadController, harness.inputs)

    result.current.requestShellReload()
    await tick()
    await tick()

    assert.equal(prepared, 1)
    assert.equal(harness.replacements.length, 0,
      'replacement must not race the dynamically inserted navigation rule')
    assert.equal(frames.length, 1)

    frames.shift()()
    assert.deepEqual(harness.replacements, ['/shell/'])
  } finally {
    globalThis.history = previousHistory
  }
})
