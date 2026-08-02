import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../ChatView/hooks/__tests__/react-hook-shim.mjs'
import useWorkspaceSession from '../useWorkspaceSession.js'
import * as paneModel from '../paneModel.js'

function memoryStorage(seed = {}) {
  const values = new Map(Object.entries(seed))
  return {
    getItem(key) {
      return values.has(key) ? values.get(key) : null
    },
    setItem(key, value) {
      values.set(key, String(value))
    },
    removeItem(key) {
      values.delete(key)
    },
  }
}

test('workspace owner composes transitions through one synchronous dispatch boundary', () => {
  const storage = memoryStorage()
  const { result } = renderHook(useWorkspaceSession, { storage })
  const paneId = result.current.workspace.focusedPaneId

  result.current.dispatchWorkspace({
    type: 'OPEN_TAB',
    paneId,
    tab: { kind: 'chat', id: 'first' },
  })
  result.current.dispatchWorkspace({
    type: 'OPEN_TAB',
    paneId,
    tab: { kind: 'chat', id: 'second' },
  })

  assert.deepEqual(
    paneModel.flatten(result.current.workspace).map(tab => tab.id),
    ['first', 'second'],
  )
  assert.equal(result.current.workspaceStateRef.current.ws, result.current.workspace)
})

test('workspace owner publishes transition policy hooks without owning their policy', () => {
  const storage = memoryStorage()
  const { result } = renderHook(useWorkspaceSession, { storage })
  const transitions = []
  result.current.onWorkspaceTransitionRef.current = (prev, next) => {
    transitions.push([prev, next])
  }

  result.current.dispatchWorkspace({
    type: 'OPEN_TAB',
    paneId: result.current.workspace.focusedPaneId,
    tab: { kind: 'app', id: 42 },
  })

  assert.equal(transitions.length, 1)
  assert.notEqual(transitions[0][0], transitions[0][1])
})

test('reload snapshot persists the ref-current workspace, not a stale render closure', () => {
  const storage = memoryStorage()
  const { result } = renderHook(useWorkspaceSession, { storage })

  result.current.dispatchWorkspace({
    type: 'OPEN_TAB',
    paneId: result.current.workspace.focusedPaneId,
    tab: { kind: 'chat', id: 'durable' },
  })
  result.current.persistWorkspaceSnapshot()

  const restored = paneModel.parseWorkspace(
    storage.getItem(paneModel.STORAGE_KEY),
  )
  assert.deepEqual(paneModel.flatten(restored).map(tab => tab.id), ['durable'])
})

test('a live session workspace is promoted once and restored after a cold relaunch', () => {
  let visited = paneModel.seedFromFlatTabs([
    { kind: 'chat', id: 'chat-a' },
    { kind: 'app', id: 42 },
  ])
  visited = paneModel.setActiveTab(visited, visited.focusedPaneId, 'chat:chat-a')
  const legacy = memoryStorage({
    [paneModel.STORAGE_KEY]: paneModel.serializeWorkspace(visited),
  })
  const durable = memoryStorage()

  const firstLaunch = renderHook(useWorkspaceSession, {
    storage: durable,
    legacyStorage: legacy,
  })
  assert.equal(
    firstLaunch.result.current.workspace.panes.p0.activeTabKey,
    'chat:chat-a',
  )
  assert.equal(legacy.getItem(paneModel.STORAGE_KEY), null)
  assert.ok(durable.getItem(paneModel.STORAGE_KEY))

  const coldRelaunch = renderHook(useWorkspaceSession, {
    storage: durable,
    legacyStorage: memoryStorage(),
  })
  assert.deepEqual(
    paneModel.flatten(coldRelaunch.result.current.workspace).map(tab => tab.id),
    ['chat-a', '42'],
  )
  assert.equal(
    coldRelaunch.result.current.workspace.panes.p0.activeTabKey,
    'chat:chat-a',
  )
})

test('a failed durable copy keeps the session workspace available', () => {
  const visited = paneModel.seedFromFlatTabs([{ kind: 'chat', id: 'still-here' }])
  const legacy = memoryStorage({
    [paneModel.STORAGE_KEY]: paneModel.serializeWorkspace(visited),
  })
  const unavailable = {
    getItem() { return null },
    setItem() { throw new DOMException('Quota exceeded', 'QuotaExceededError') },
    removeItem() {},
  }

  const { result } = renderHook(useWorkspaceSession, {
    storage: unavailable,
    legacyStorage: legacy,
  })

  assert.deepEqual(
    paneModel.flatten(result.current.workspace).map(tab => tab.id),
    ['still-here'],
  )
  assert.ok(legacy.getItem(paneModel.STORAGE_KEY))
})
