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

// ── Compatibility suite (design §7) ─────────────────────────────────────────

// Two panes: p0 = chat 'c' (focused), p1 = app 42.
function twoPanes() {
  let ws = paneModel.seedFromFlatTabs([makeTab('chat', 'c')])
  ws = paneModel.splitPaneWithTab(ws, makeTab('app', 42), { paneId: 'p0', edge: 'right' })
  return paneModel.focusPane(ws, 'p0')
}

test('blob round-trip preserves the Settings tab (v:1, no migration)', () => {
  let ws = paneModel.openTab(onePane('9'), settingsTab(), { paneId: 'p0', activate: true })
  ws = paneModel.normalize(ws)
  const restored = paneModel.parseWorkspace(paneModel.serializeWorkspace(ws))
  assert.equal(restored.v, 1, 'blob version unchanged — no migration')
  assert.deepEqual(restored, ws, 'exact round-trip')
  assert.ok(paneModel.paneOf(restored, SETTINGS_TAB_KEY), 'settings tab survived')
  assert.equal(paneModel.focusedContentRoute(restored).view, 'settings')
})

test('an older/unknown tab kind is scrubbed on parse (forgiving read)', () => {
  const raw = JSON.stringify({
    v: 1,
    viewMode: 'panes',
    layout: 'p0',
    panes: {
      p0: {
        id: 'p0',
        tabs: [{ kind: 'widget', id: 'x' }, { kind: 'chat', id: '5' }],
        activeTabKey: 'chat:5',
      },
    },
    focusedPaneId: 'p0',
    nextId: 1,
  })
  const parsed = paneModel.parseWorkspace(raw)
  assert.deepEqual(paneModel.flatten(parsed).map(t => t.kind), ['chat'],
    'the unknown kind is dropped, the chat survives')
})

test('flag OFF scrubs a persisted Settings tab before first render (rollback safety)', async () => {
  // A blob a builder (flag-on) shell wrote, carrying a Settings tab.
  const onWs = paneModel.openTab(onePane('9'), settingsTab(), { paneId: 'p0', activate: true })
  const blob = paneModel.serializeWorkspace(onWs)
  assert.ok(paneModel.flatten(onWs).some(tabModel.isSettingsTab), 'flag-on keeps it')

  // Re-evaluate paneModel with the kill switch OFF (a fresh module instance via a
  // cache-busting query, evaluated while localStorage returns '0' for the key).
  const prevLS = globalThis.localStorage
  globalThis.localStorage = { getItem: (k) => (k === 'mobius:builder-settings' ? '0' : null) }
  try {
    const pmOff = await import('../paneModel.js?builder-settings-off')
    assert.equal(pmOff.BUILDER_SETTINGS_ENABLED, false, 'flag read as off')
    const parsed = pmOff.parseWorkspace(blob)
    assert.ok(!pmOff.flatten(parsed).some(t => t.kind === 'settings'),
      'the Settings tab is scrubbed like any unknown kind')
    assert.ok(pmOff.flatten(parsed).some(t => t.kind === 'chat' && t.id === '9'),
      'the chat survives the scrub')
  } finally {
    if (prevLS === undefined) delete globalThis.localStorage
    else globalThis.localStorage = prevLS
  }
})

test('workspace-splits OFF also disables the Settings tab and scrubs it (review §3)', async () => {
  // The Settings tab only makes sense where builder mode can exist. With splits off
  // there is no builder mode, so BUILDER_SETTINGS_ENABLED must be false even though
  // the builder-settings key is unset — a persisted settings:settings is scrubbed
  // and can never leak into the legacy single-pane strip.
  const onWs = paneModel.openTab(onePane('9'), settingsTab(), { paneId: 'p0', activate: true })
  const blob = paneModel.serializeWorkspace(onWs)

  const prevLS = globalThis.localStorage
  // splits off; the builder-settings key is UNSET (its own default would be "on").
  globalThis.localStorage = { getItem: (k) => (k === 'mobius:workspace-splits' ? '0' : null) }
  try {
    const pmOff = await import('../paneModel.js?workspace-splits-off')
    assert.equal(pmOff.WORKSPACE_SPLITS_ENABLED, false)
    assert.equal(pmOff.BUILDER_SETTINGS_ENABLED, false, 'gated on splits, not just its own key')
    const parsed = pmOff.parseWorkspace(blob)
    assert.ok(!pmOff.flatten(parsed).some(t => t.kind === 'settings'), 'settings scrubbed')
    assert.ok(pmOff.flatten(parsed).some(t => t.kind === 'chat' && t.id === '9'), 'chat kept')
  } finally {
    if (prevLS === undefined) delete globalThis.localStorage
    else globalThis.localStorage = prevLS
  }
})

test('a mode flip (SET_VIEW_MODE) PRESERVES a builder Settings tab AND a pending undo', () => {
  // v2 deleted the 'mode-convert' close: a builder Settings tab SURVIVES entering
  // single (single paints its own slot, never Settings), and the pure SET_VIEW_MODE
  // flip is orthogonal to the tree, so it neither removes the tab nor clobbers a
  // pending undo. Two panes so a MOVE_TAB can set an undo slot; then Settings enters p1.
  let state = paneModel.initialWorkspaceState(twoPanes())
  state = paneModel.workspaceReducer(state, {
    type: 'OPEN_TAB', paneId: 'p1', tab: settingsTab(), activate: true,
  })
  // An undoable move (chat 'c' from p0 → p1's strip) sets the undo slot.
  state = paneModel.workspaceReducer(state, {
    type: 'MOVE_TAB', tabKey: 'chat:c', target: { paneId: 'p1' },
  })
  const pendingUndo = state.undo
  assert.ok(pendingUndo, 'the move is undoable')

  // Entering single mode is a pure view flip — the Settings tab is NOT removed.
  state = paneModel.workspaceReducer(state, { type: 'SET_VIEW_MODE', mode: 'single' })
  assert.ok(paneModel.paneOf(state.ws, SETTINGS_TAB_KEY), 'settings tab survives the flip')
  assert.equal(state.undo, pendingUndo, 'the pending move undo is PRESERVED, not clobbered')

  // And the preserved slot still undoes the move.
  state = paneModel.workspaceReducer(state, { type: 'UNDO_LAST' })
  assert.equal(paneModel.paneOf(state.ws, 'chat:c').id, 'p0', 'undo reverted the move')
})

test('reopening Settings focuses the existing tab (single instance, no duplicate)', () => {
  let state = paneModel.initialWorkspaceState(twoPanes())
  // Open Settings into p0.
  state = paneModel.workspaceReducer(state, {
    type: 'OPEN_TAB', paneId: 'p0', tab: settingsTab(), activate: true,
  })
  // Move focus to p1, then reopen Settings TARGETING p1 — dedup keeps the one
  // instance in p0 and moves focus back to it.
  state = paneModel.workspaceReducer(state, { type: 'FOCUS', paneId: 'p1' })
  state = paneModel.workspaceReducer(state, {
    type: 'OPEN_TAB', paneId: 'p1', tab: settingsTab(), activate: true,
  })
  const settingsTabs = paneModel.flatten(state.ws).filter(tabModel.isSettingsTab)
  assert.equal(settingsTabs.length, 1, 'exactly one Settings tab workspace-wide')
  assert.equal(paneModel.paneOf(state.ws, SETTINGS_TAB_KEY).id, 'p0', 'stayed in p0')
  assert.equal(state.ws.focusedPaneId, 'p0', 'reopen focused the existing tab')
})

test('closing a pane holding Settings, then UNDO, restores the Settings tab', () => {
  let state = paneModel.initialWorkspaceState(twoPanes())
  // Open Settings into p1 (alongside app 42), then make it p1's active tab.
  state = paneModel.workspaceReducer(state, {
    type: 'OPEN_TAB', paneId: 'p1', tab: settingsTab(), activate: true,
  })
  assert.equal(paneModel.paneOf(state.ws, SETTINGS_TAB_KEY).id, 'p1')
  // Close pane p1 — the whole pane (app + Settings) collapses.
  state = paneModel.workspaceReducer(state, { type: 'CLOSE_PANE', paneId: 'p1' })
  assert.equal(paneModel.paneOf(state.ws, SETTINGS_TAB_KEY), null, 'settings gone with the pane')
  assert.ok(state.undo, 'the close is undoable')
  // Undo restores the pane and its Settings tab.
  state = paneModel.workspaceReducer(state, { type: 'UNDO_LAST' })
  assert.ok(paneModel.paneOf(state.ws, SETTINGS_TAB_KEY), 'settings restored by undo')
})

test('mode-conversion primitives: open adds the tab, close removes it', () => {
  // The reducer-level building blocks the nav adapter composes for the no-history
  // conversion. Entering builder converts an overlay to a tab (openTab); entering
  // single removes the builder-only tab (closeTab), and the chat re-fronts.
  let ws = onePane('5')
  ws = paneModel.openTab(ws, settingsTab(), { paneId: 'p0', activate: true })
  assert.equal(paneModel.focusedContentRoute(ws).view, 'settings', 'settings is the surface')
  ws = paneModel.closeTab(ws, SETTINGS_TAB_KEY)
  assert.equal(paneModel.paneOf(ws, SETTINGS_TAB_KEY), null, 'the builder-only tab is removed')
  assert.equal(paneModel.focusedContentRoute(ws).view, 'chat', 'the chat re-fronts')
  assert.equal(paneModel.focusedContentRoute(ws).chatId, '5')
})
