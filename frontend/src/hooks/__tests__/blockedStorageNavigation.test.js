/* Unavailable browser persistence must not prevent in-memory navigation. */
import test from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'

test('navigation imports and opens an explicit chat when browser storage getters are blocked', async t => {
  const keys = ['window', 'document', 'history', 'location', 'localStorage', 'sessionStorage', 'navigation']
  const originals = keys.map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)])
  t.after(() => {
    for (const [key, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
  })
  globalThis.window = {
    location: { pathname: '/shell/', search: '?chat=destination', href: 'https://example.test/shell/?chat=destination' },
    innerWidth: 390, innerHeight: 797,
    addEventListener() {}, removeEventListener() {},
  }
  globalThis.location = window.location
  globalThis.document = { body: { style: {} } }
  globalThis.history = {
    state: null,
    pushState(state) { this.state = state },
    replaceState(state) { this.state = state },
    back() {},
  }
  delete globalThis.navigation
  for (const key of ['localStorage', 'sessionStorage']) {
    Object.defineProperty(globalThis, key, {
      configurable: true,
      get() { throw new DOMException('Browser storage is blocked.', 'SecurityError') },
    })
  }
  const [{ default: useNavigation }, paneModel] = await Promise.all([
    import('../useNavigation.js'),
    import('../../components/Shell/paneModel.js'),
  ])
  const workspace = paneModel.seedFromFlatTabs([{ kind: 'chat', id: 'previous' }])
  const workspaceStateRef = { current: { ws: workspace, undo: null } }
  const hook = renderHook(useNavigation, {
    workspace, workspaceStateRef,
    dispatchWorkspace: action => {
      workspaceStateRef.current = paneModel.workspaceReducer(workspaceStateRef.current, action)
    },
    visiblePaneIds: new Set(Object.keys(workspace.panes)),
    blobValid: true, replaceImplicitBootTab: false,
    dragActiveRef: { current: false },
  })
  try {
    assert.equal(paneModel.activeContentRoute(workspaceStateRef.current.ws).chatId, 'destination')
  } finally {
    hook.unmount()
  }
})
