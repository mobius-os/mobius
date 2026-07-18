import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as paneModel from '../paneModel.js'
import * as tabModel from '../tabModel.js'

const { makeTab, settingsTab, SETTINGS_TAB_KEY } = tabModel

// A one-pane workspace holding a single chat, through the public seed op.
function onePane(chatId = '5') {
  return paneModel.seedFromFlatTabs([makeTab('chat', chatId)])
}

// ── The model accepts the canonical Settings tab (builder flag ON in tests:
//    no localStorage in node → the '0' kill switch never fires → default true) ──

test('sanitize/normalize keeps the canonical settings:settings tab', () => {
  let ws = onePane()
  ws = paneModel.openTab(ws, settingsTab(), { paneId: ws.focusedPaneId, activate: true })
  const pane = ws.panes[ws.focusedPaneId]
  assert.ok(pane.tabs.some(t => tabModel.tabKey(t) === SETTINGS_TAB_KEY), 'settings tab present')
  assert.equal(pane.activeTabKey, SETTINGS_TAB_KEY, 'settings tab is active')
  // normalize is idempotent and never drops the accepted settings tab.
  assert.equal(paneModel.normalize(ws), ws, 'already-normalized → same reference')
})

test('normalize drops a NON-canonical settings id (never coerces it)', () => {
  const ws = onePane()
  // A foreign settings id is not a real tab — dropped, so it can never masquerade
  // as the single canonical instance nor collide with it.
  const corrupt = {
    ...ws,
    panes: {
      [ws.focusedPaneId]: {
        id: ws.focusedPaneId,
        tabs: [makeTab('chat', '5'), { kind: 'settings', id: 'other' }],
        activeTabKey: 'chat:5',
      },
    },
  }
  const norm = paneModel.normalize(corrupt)
  const keys = norm.panes[norm.focusedPaneId].tabs.map(tabModel.tabKey)
  assert.deepEqual(keys, ['chat:5'], 'foreign settings id scrubbed, canonical chat kept')
})

// ── focusedContentRoute teaches the derived triple about Settings ─────────────

test('focusedContentRoute reports settings when the focused active tab is Settings', () => {
  let ws = onePane()
  ws = paneModel.openTab(ws, settingsTab(), { paneId: ws.focusedPaneId, activate: true })
  const route = paneModel.focusedContentRoute(ws)
  assert.equal(route.view, 'settings')
  assert.equal(route.chatId, null)
  assert.equal(route.appId, null)
  assert.equal(route.paneId, ws.focusedPaneId, 'route carries the focused pane hint')
})

test('focusedContentRoute ignores a BACKGROUND settings tab', () => {
  let ws = onePane()
  // Open settings, then re-activate the chat: settings becomes a background tab.
  ws = paneModel.openTab(ws, settingsTab(), { paneId: ws.focusedPaneId, activate: true })
  ws = paneModel.setActiveTab(ws, ws.focusedPaneId, 'chat:5')
  const route = paneModel.focusedContentRoute(ws)
  assert.equal(route.view, 'chat', 'a non-active settings tab does not drive the route')
  assert.equal(route.chatId, '5')
})

// ── Legacy rollback projection stays chat/app-only ───────────────────────────

test('flattenRollbackPriority excludes the Settings tab', () => {
  let ws = onePane()
  ws = paneModel.openTab(ws, makeTab('app', 42), { paneId: ws.focusedPaneId, activate: true })
  ws = paneModel.openTab(ws, settingsTab(), { paneId: ws.focusedPaneId, activate: true })
  const rollback = paneModel.flattenRollbackPriority(ws)
  assert.ok(!rollback.some(tabModel.isSettingsTab), 'settings never mirrored to the legacy key')
  // flatten() (the strip projection) DOES keep it — it is a real, tappable tab.
  assert.ok(paneModel.flatten(ws).some(tabModel.isSettingsTab), 'flatten keeps the settings tab')
})
