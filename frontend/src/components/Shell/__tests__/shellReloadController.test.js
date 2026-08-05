import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as paneModel from '../paneModel.js'
import { deriveShellReloadState } from '../useShellReloadController.js'

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
