/* Every chat destination (deep link, in-chat link, app frame, notification,
 * restored tab) reaches the workspace through navTo or recordChatNavigation.
 * A chat id is later fetched as /api/chats/<id>, so a non-id must stop here,
 * before any history write, whichever entry point let it through. */
import test from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'

function memoryStorage() {
  const values = new Map()
  return {
    getItem: key => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  }
}

test('navTo and recordChatNavigation refuse a chat id that is not one path segment', async t => {
  const keys = ['window', 'document', 'history', 'location', 'localStorage', 'sessionStorage', 'navigation',
    'requestAnimationFrame', 'cancelAnimationFrame']
  const originals = keys.map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)])
  t.after(() => {
    for (const [key, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
  })
  const pushes = []
  globalThis.window = {
    location: { pathname: '/shell/', search: '', href: 'https://example.test/shell/' },
    innerWidth: 390, innerHeight: 797,
    addEventListener() {}, removeEventListener() {},
  }
  globalThis.location = window.location
  globalThis.document = { body: { style: {} } }
  globalThis.history = {
    state: null,
    pushState(state) { pushes.push(state); this.state = state },
    replaceState(state) { this.state = state },
    back() {},
  }
  globalThis.localStorage = memoryStorage()
  globalThis.sessionStorage = memoryStorage()
  globalThis.requestAnimationFrame = callback => { callback(); return 1 }
  globalThis.cancelAnimationFrame = () => {}
  delete globalThis.navigation

  const [{ default: useNavigation }, paneModel] = await Promise.all([
    import('../useNavigation.js'),
    import('../../components/Shell/paneModel.js'),
  ])
  const tab = { kind: 'chat', id: 'previous' }
  const workspace = { ...paneModel.seedFromFlatTabs([tab]), singleScreen: tab }
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
    const pushesBefore = pushes.length
    for (const chatId of ['../storage/apps/7/fake-chat.json?', 'a b', '']) {
      hook.result.current.navTo('chat', { chatId })
      assert.equal(hook.result.current.recordChatNavigation(chatId), false, chatId)
    }
    assert.equal(pushes.length, pushesBefore, 'no history entry for a refused chat')
    assert.equal(paneModel.activeContentRoute(workspaceStateRef.current.ws).chatId, 'previous')

    hook.result.current.navTo('chat', { chatId: 'destination' })
    assert.equal(paneModel.activeContentRoute(workspaceStateRef.current.ws).chatId, 'destination',
      'a real chat id still navigates')
  } finally {
    hook.unmount()
  }
})
