/* A result launch uses the normal workspace and HOME history in both modes. */
import test from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial))
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  }
}

test('result deep link overrides persisted workspace and claimed reload, then Back reaches HOME', async () => {
  const listeners = new Map()
  globalThis.window = {
    location: { pathname: '/shell/', search: '?project=p-1&result=site', href: 'https://localhost/shell/?project=p-1&result=site' },
    innerWidth: 1000, innerHeight: 800,
    addEventListener: (type, callback) => listeners.set(type, callback),
    removeEventListener: (type, callback) => { if (listeners.get(type) === callback) listeners.delete(type) },
  }
  globalThis.location = window.location
  globalThis.document = { body: { style: {} } }
  globalThis.localStorage = memoryStorage({ moebius_active_chat: 'home-chat' })
  globalThis.sessionStorage = memoryStorage({
    'shell-reload': JSON.stringify({ destinationClaimed: true, activeView: 'apps' }),
  })
  delete globalThis.navigation
  const [{ default: useNavigation }, paneModel] = await Promise.all([
    import('../useNavigation.js'), import('../../components/Shell/paneModel.js'),
  ])

  for (const mode of ['single', 'panes']) {
    const entries = []
    let index = -1
    globalThis.history = {
      get state() { return entries[index]?.state ?? null },
      replaceState(state, _title, url) {
        if (index < 0) { entries.push({ state, url }); index = 0 } else entries[index] = { state, url: url ?? entries[index].url }
      },
      pushState(state, _title, url) { entries.splice(++index); entries.push({ state, url }) },
      back() { index -= 1; listeners.get('popstate')?.() },
      forward() { index += 1; listeners.get('popstate')?.() },
    }
    const saved = paneModel.setViewMode(paneModel.seedFromFlatTabs([{ kind: 'chat', id: 'old-chat' }]), mode)
    const workspaceStateRef = { current: { ws: saved, undo: null } }
    const mounted = renderHook(useNavigation, {
      workspace: saved, workspaceStateRef,
      dispatchWorkspace: action => { workspaceStateRef.current = paneModel.workspaceReducer(workspaceStateRef.current, action) },
      visiblePaneIds: new Set(Object.keys(saved.panes)), blobValid: true,
      replaceImplicitBootTab: false, dragActiveRef: { current: false },
    })
    const route = paneModel.activeContentRoute(workspaceStateRef.current.ws)
    assert.equal(route.view, 'artifact', mode)
    assert.equal(route.artifactRef, 'p-1:site', mode)
    assert.equal(entries[0].state.route.view, 'chat', mode)
    assert.equal(entries[0].url, '/shell/', mode)
    assert.equal(entries[1].state.route.view, 'artifact', mode)
    assert.equal(entries[1].state.route.artifactRef, 'p-1:site', mode)
    history.back()
    assert.equal(paneModel.activeContentRoute(workspaceStateRef.current.ws).view, 'chat', mode)
    history.forward()
    assert.equal(paneModel.activeContentRoute(workspaceStateRef.current.ws).artifactRef, 'p-1:site', mode)
    mounted.unmount()
  }
})
