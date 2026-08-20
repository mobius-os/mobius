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

test('workspace shortcut provider is disabled while paused', () => {
  assert.equal(hasWorkspaceShortcutProvider([{
    capability_contract: { runtime: { 'workspace.shortcuts': { version: 1 } } },
    paused_capabilities: { 'workspace.shortcuts': true },
  }]), false)
  assert.equal(hasWorkspaceShortcutProvider([{
    capability_contract: { runtime: { 'workspace.shortcuts': { version: 1 } } },
    paused_capabilities: { 'workspace.shortcuts': false },
  }]), true)
})

test('Windows/Linux shortcut mapping ignores AltGraph and unrelated modifiers', () => {
  assert.equal(workspaceShortcutAction({ ctrlKey: true, altKey: true, key: 't' }, 'Win32'), 'open')
  assert.equal(workspaceShortcutAction({ ctrlKey: true, altKey: true, shiftKey: true, key: 'T' }, 'Win32'), 'restore')
  assert.equal(workspaceShortcutAction({ ctrlKey: true, altKey: true, key: 'PageDown' }, 'Win32'), 'next')
  assert.equal(workspaceShortcutAction({ ctrlKey: true, altKey: true, key: '9' }, 'Win32'), 'select:9')
  assert.equal(workspaceShortcutAction({ ctrlKey: true, altKey: true, key: 't', getModifierState: k => k === 'AltGraph' }, 'Win32'), null)
  // A Cmd-modified event should never fire the Ctrl+Alt mapping.
  assert.equal(workspaceShortcutAction({ ctrlKey: true, altKey: true, metaKey: true, key: 't' }, 'Win32'), null)
})

test('Mac shortcut mapping uses Cmd+Option, never the VoiceOver or native-Chrome combos', () => {
  assert.equal(workspaceShortcutAction({ metaKey: true, altKey: true, key: 't' }, 'MacIntel'), 'open')
  assert.equal(workspaceShortcutAction({ metaKey: true, altKey: true, shiftKey: true, key: 'T' }, 'MacIntel'), 'restore')
  assert.equal(workspaceShortcutAction({ metaKey: true, altKey: true, key: ']' }, 'MacIntel'), 'next')
  assert.equal(workspaceShortcutAction({ metaKey: true, altKey: true, key: '[' }, 'MacIntel'), 'previous')
  assert.equal(workspaceShortcutAction({ metaKey: true, altKey: true, key: '9' }, 'MacIntel'), 'select:9')
  // Ctrl+Alt (Ctrl+Option) is the VoiceOver modifier on Mac — must not fire.
  assert.equal(workspaceShortcutAction({ ctrlKey: true, altKey: true, key: 't' }, 'MacIntel'), null)
  // Cmd+Option+Left/Right is Chrome's own native tab-switcher — must not fire
  // (it would move real browser tabs, and the page never sees it anyway).
  assert.equal(workspaceShortcutAction({ metaKey: true, altKey: true, key: 'ArrowRight' }, 'MacIntel'), null)
  assert.equal(workspaceShortcutAction({ metaKey: true, altKey: true, key: 't', getModifierState: k => k === 'AltGraph' }, 'MacIntel'), null)
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
