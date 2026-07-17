import test from 'node:test'
import assert from 'node:assert/strict'
import * as paneModel from '../paneModel.js'
import * as tabModel from '../tabModel.js'

// A two-pane workspace: p0 = [chat 1] (focused), p1 = [chat 2].
function twoPanes() {
  let ws = paneModel.seedFromFlatTabs([{ kind: 'chat', id: '1' }, { kind: 'chat', id: '2' }])
  ws = paneModel.moveTab(ws, 'chat:2', { root: true, edge: 'right' })
  return paneModel.focusPane(ws, 'p0')
}

const leaves = (ws) => Object.keys(ws.panes).length

// ── openTabAt: a drawer item lands exactly where the zone named it ───────────

test('openTabAt center-joins a new item as the active tab of the target pane', () => {
  const ws = twoPanes()
  const next = paneModel.openTabAt(ws, tabModel.makeTab('app', 42), { paneId: 'p1' })
  const p1 = next.panes.p1
  assert.equal(p1.tabs.some(t => tabModel.tabKey(t) === 'app:42'), true)
  assert.equal(p1.activeTabKey, 'app:42')
  assert.equal(leaves(next), 2) // no new pane for a center join
})

test('openTabAt inserts a new item at a strip caret index', () => {
  const ws = twoPanes()
  // Give p1 a second tab so an index insert is observable.
  let seeded = paneModel.openTab(ws, tabModel.makeTab('chat', '3'), { paneId: 'p1' })
  const next = paneModel.openTabAt(seeded, tabModel.makeTab('app', 7), { paneId: 'p1', index: 0 })
  assert.equal(tabModel.tabKey(next.panes.p1.tabs[0]), 'app:7')
})

test('openTabAt edge-splits a new item into a fresh pane, leaving the target intact', () => {
  const ws = twoPanes()
  const before = leaves(ws)
  const next = paneModel.openTabAt(ws, tabModel.makeTab('app', 9), { paneId: 'p0', edge: 'bottom' })
  assert.equal(leaves(next), before + 1) // a new pane was created
  // p0 still holds exactly its original tab (the item went to the NEW pane).
  assert.deepEqual(next.panes.p0.tabs.map(tabModel.tabKey), ['chat:1'])
  // The item is alone in the newly focused pane.
  const focused = next.panes[next.focusedPaneId]
  assert.deepEqual(focused.tabs.map(tabModel.tabKey), ['app:9'])
})

test('openTabAt root-splits a new item across the whole workspace', () => {
  const ws = twoPanes()
  const before = leaves(ws)
  const next = paneModel.openTabAt(ws, tabModel.makeTab('chat', '5'), { root: true, edge: 'top' })
  assert.equal(leaves(next), before + 1)
  const focused = next.panes[next.focusedPaneId]
  assert.deepEqual(focused.tabs.map(tabModel.tabKey), ['chat:5'])
})

test('openTabAt degrades an already-open tab to a move (never a duplicate)', () => {
  const ws = twoPanes()
  // chat:2 lives in p1; dropping it on p0's center moves it there.
  const next = paneModel.openTabAt(ws, tabModel.makeTab('chat', '2'), { paneId: 'p0' })
  // chat:2 now in p0, and p1 collapsed away (it lost its only tab).
  assert.equal(paneModel.paneOf(next, 'chat:2').id, 'p0')
  // No duplicate anywhere.
  let count = 0
  for (const id of Object.keys(next.panes)) {
    count += next.panes[id].tabs.filter(t => tabModel.tabKey(t) === 'chat:2').length
  }
  assert.equal(count, 1)
})

test('openTabAt returns the same reference on a no-op', () => {
  const ws = twoPanes()
  // Re-open chat:1 (already in the focused pane) at its own pane center — nothing
  // changes, so the reference is preserved for React to bail.
  assert.equal(paneModel.openTabAt(ws, tabModel.makeTab('chat', '1'), { paneId: 'p0' }), ws)
})

// ── OPEN_TAB_AT reducer: single-slot undo makes a drop reversible ────────────

test('OPEN_TAB_AT sets the undo slot and UNDO_LAST restores the pre-drop tree', () => {
  const ws = twoPanes()
  const s0 = paneModel.initialWorkspaceState(ws)
  const s1 = paneModel.workspaceReducer(s0, {
    type: 'OPEN_TAB_AT', tab: tabModel.makeTab('app', 42), target: { paneId: 'p0', edge: 'right' },
    label: 'Moved App',
  })
  assert.notEqual(s1.ws, ws)
  assert.equal(s1.undo.ws, ws)
  assert.equal(s1.undo.label, 'Moved App')
  const s2 = paneModel.workspaceReducer(s1, { type: 'UNDO_LAST' })
  assert.equal(s2.ws, ws) // exact pre-drop reference restored
  assert.equal(s2.undo, null)
})

test('OPEN_TAB_AT returns the same state on a no-op', () => {
  const ws = twoPanes()
  const s0 = paneModel.initialWorkspaceState(ws)
  const s1 = paneModel.workspaceReducer(s0, {
    type: 'OPEN_TAB_AT', tab: tabModel.makeTab('chat', '1'), target: { paneId: 'p0' },
  })
  assert.equal(s1, s0)
})
