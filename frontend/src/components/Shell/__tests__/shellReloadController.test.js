import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as paneModel from '../paneModel.js'
import useShellReloadController, {
  deriveShellReloadState,
} from '../useShellReloadController.js'
import { renderHook } from '../../ChatView/hooks/__tests__/react-hook-shim.mjs'

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
    location: { reload() {} },
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
