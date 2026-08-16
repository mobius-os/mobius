import test from 'node:test'
import assert from 'node:assert/strict'

import * as paneModel from '../paneModel.js'
import * as tabModel from '../tabModel.js'
import {
  hasWorkspaceShortcutProvider,
  workspaceShortcutAction,
} from '../useWorkspaceShortcuts.js'

test('workspace shortcut provider is activated only by reviewed capability v1', () => {
  assert.equal(hasWorkspaceShortcutProvider([]), false)
  assert.equal(hasWorkspaceShortcutProvider([{
    capability_contract: { runtime: { 'workspace.shortcuts': { version: 1 } } },
  }]), true)
  assert.equal(hasWorkspaceShortcutProvider([{
    capability_contract: { runtime: { 'workspace.shortcuts': { version: 2 } } },
  }]), false)
})

test('Windows shortcut mapping ignores AltGraph and unrelated modifiers', () => {
  assert.equal(workspaceShortcutAction({ ctrlKey: true, altKey: true, key: 't' }, 'Win32'), 'open')
  assert.equal(workspaceShortcutAction({ ctrlKey: true, altKey: true, shiftKey: true, key: 'T' }, 'Win32'), 'restore')
  assert.equal(workspaceShortcutAction({ ctrlKey: true, altKey: true, key: 'PageDown' }, 'Win32'), 'next')
  assert.equal(workspaceShortcutAction({ ctrlKey: true, altKey: true, key: '9' }, 'Win32'), 'select:9')
  assert.equal(workspaceShortcutAction({ ctrlKey: true, altKey: true, key: 't', getModifierState: k => k === 'AltGraph' }, 'Win32'), null)
  assert.equal(workspaceShortcutAction({ ctrlKey: true, altKey: true, key: 't' }, 'MacIntel'), null)
})

test('closed tab records restore into the current workspace without replacing siblings', () => {
  const a = tabModel.makeTab('chat', 'a')
  const b = tabModel.makeTab('app', '2')
  const ws = { ...paneModel.seedFromFlatTabs([a, b]), viewMode: 'panes' }
  const key = tabModel.tabKey(b)
  const record = paneModel.closedTabRecord(ws, key)
  const closed = paneModel.closeTab(ws, key)
  const restored = paneModel.restoreClosedTab(closed, record)
  assert.deepEqual(paneModel.flatten(restored), [a, b])
  assert.equal(restored.panes[restored.focusedPaneId].activeTabKey, key)
})
