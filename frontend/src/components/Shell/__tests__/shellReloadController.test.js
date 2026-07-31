import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as paneModel from '../paneModel.js'
import { deriveShellReloadState } from '../useShellReloadController.js'

test('reload snapshot derives content from the current workspace authority', () => {
  const workspace = paneModel.seedFromFlatTabs([
    { kind: 'chat', id: 'older' },
    { kind: 'app', id: 42 },
  ])

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
  const workspace = paneModel.seedFromFlatTabs([
    { kind: 'chat', id: 'kept' },
  ])

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
