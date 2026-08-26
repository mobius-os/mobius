import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial))
  return {
    getItem: key => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  }
}

test('a claimed Apps navigation survives the shell reload before the old page paints it', async () => {
  const listeners = new Map()
  globalThis.window = {
    location: { pathname: '/shell/', search: '', href: 'http://localhost/shell/' },
    innerWidth: 390,
    innerHeight: 844,
    addEventListener(type, handler) {
      if (!listeners.has(type)) listeners.set(type, new Set())
      listeners.get(type).add(handler)
    },
    removeEventListener(type, handler) { listeners.get(type)?.delete(handler) },
  }
  globalThis.location = window.location
  globalThis.document = { body: { style: {} } }
  globalThis.localStorage = memoryStorage()
  globalThis.sessionStorage = memoryStorage({
    'shell-reload': JSON.stringify({
      destinationClaimed: true,
      activeView: 'apps',
      activeAppId: null,
      activeChatId: null,
      drawerOpen: false,
    }),
  })
  globalThis.history = {
    state: null,
    pushState(state) { this.state = state },
    replaceState(state) { this.state = state },
    back() {},
  }
  delete globalThis.navigation

  const [{ default: useNavigation }, paneModel] = await Promise.all([
    import('../useNavigation.js'),
    import('../../components/Shell/paneModel.js'),
  ])
  const ws = paneModel.seedFromFlatTabs([{ kind: 'chat', id: 'current-chat' }])
  const workspaceStateRef = { current: { ws, undo: null } }
  const actions = []
  const dispatchWorkspace = action => {
    actions.push(action)
    workspaceStateRef.current = paneModel.workspaceReducer(workspaceStateRef.current, action)
  }

  renderHook(useNavigation, {
    workspace: ws,
    workspaceStateRef,
    dispatchWorkspace,
    visiblePaneIds: new Set(Object.keys(ws.panes)),
    blobValid: true,
    replaceImplicitBootTab: false,
    dragActiveRef: { current: false },
  })

  assert.deepEqual(
    actions.find(action => action.type === 'SET_SINGLE_SCREEN'),
    { type: 'SET_SINGLE_SCREEN', item: { kind: 'apps', id: 'apps' } },
  )
})
