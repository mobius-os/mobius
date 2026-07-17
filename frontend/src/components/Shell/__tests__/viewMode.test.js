import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as paneModel from '../paneModel.js'
import * as tabModel from '../tabModel.js'

const { makeTab } = tabModel

// A fresh workspace and a two-pane one, both seeded through the public ops so
// their viewMode is whatever the model assigns by default.
function onePane() {
  return paneModel.seedFromFlatTabs([makeTab('chat', '5')])
}
function twoPanes() {
  let ws = paneModel.seedFromFlatTabs([makeTab('chat', '5')])
  return paneModel.splitPaneWithTab(ws, makeTab('app', '42'), {
    paneId: ws.focusedPaneId, edge: 'right',
  })
}

// ── viewMode field + persistence (design: view-mode toggle, forgiving parse) ──

test('a fresh workspace defaults to panes view-mode', () => {
  assert.equal(onePane().viewMode, 'panes')
  assert.equal(twoPanes().viewMode, 'panes')
})

test('setViewMode sets the mode and is same-reference on a no-op', () => {
  const ws = onePane()
  assert.equal(ws, paneModel.setViewMode(ws, 'panes'), 'no change -> same reference')
  const single = paneModel.setViewMode(ws, 'single')
  assert.equal(single.viewMode, 'single')
  assert.notEqual(single, ws)
  // The tree is untouched — only viewMode differs.
  assert.deepEqual(single.layout, ws.layout)
  assert.deepEqual(single.panes, ws.panes)
  assert.equal(single.focusedPaneId, ws.focusedPaneId)
  // An unknown mode coerces to panes.
  assert.equal(paneModel.setViewMode(single, 'nonsense').viewMode, 'panes')
})

test('toggleViewMode flips both directions, treating absent as panes', () => {
  const ws = onePane()
  assert.equal(paneModel.toggleViewMode(ws).viewMode, 'single')
  assert.equal(paneModel.toggleViewMode(paneModel.setViewMode(ws, 'single')).viewMode, 'panes')
  // A blob with no viewMode field toggles to single (absent === panes).
  const legacy = { ...ws }
  delete legacy.viewMode
  assert.equal(paneModel.toggleViewMode(legacy).viewMode, 'single')
})

test('normalize preserves a valid viewMode and coerces absent/corrupt to panes', () => {
  const single = paneModel.setViewMode(onePane(), 'single')
  assert.equal(paneModel.normalize(single).viewMode, 'single')
  const noField = { ...onePane() }
  delete noField.viewMode
  assert.equal(paneModel.normalize(noField).viewMode, 'panes', 'absent -> panes')
  assert.equal(
    paneModel.normalize({ ...onePane(), viewMode: 'garbage' }).viewMode, 'panes',
    'corrupt -> panes',
  )
})

test('normalize stays reference-stable when viewMode already matches', () => {
  // A workspace produced by the ops is already normalized, so re-normalizing it
  // must return the SAME reference (viewMode must not break the deepEqual bail).
  const single = paneModel.normalize(paneModel.setViewMode(twoPanes(), 'single'))
  assert.equal(paneModel.normalize(single), single)
})

test('viewMode round-trips the blob', () => {
  const single = paneModel.setViewMode(twoPanes(), 'single')
  const back = paneModel.parseWorkspace(paneModel.serializeWorkspace(single), { fallbackTabs: [] })
  assert.equal(back.viewMode, 'single')
  // And a panes blob round-trips as panes.
  const panes = twoPanes()
  assert.equal(
    paneModel.parseWorkspace(paneModel.serializeWorkspace(panes), { fallbackTabs: [] }).viewMode,
    'panes',
  )
})

test('parseWorkspace defaults an absent viewMode to panes', () => {
  const noField = { ...twoPanes() }
  delete noField.viewMode
  const blob = JSON.stringify(noField)
  assert.equal(paneModel.parseWorkspace(blob, { fallbackTabs: [] }).viewMode, 'panes')
  // The blob is still valid (viewMode never gates validity).
  assert.equal(paneModel.isValidWorkspaceBlob(blob), true)
})

test('parseWorkspace degrades a corrupted viewMode to panes without falling back', () => {
  const corrupt = JSON.stringify({ ...twoPanes(), viewMode: 42 })
  const ws = paneModel.parseWorkspace(corrupt, { fallbackTabs: [] })
  assert.equal(ws.viewMode, 'panes')
  // The rest of the tree survived (this was NOT a fresh-seed fallback).
  assert.equal(Object.keys(ws.panes).length, 2, 'the two panes are preserved')
  assert.equal(paneModel.isValidWorkspaceBlob(corrupt), true)
})

// ── Reducer: SET_VIEW_MODE + UNDO_LAST interplay ────────────────────────────

test('SET_VIEW_MODE flips the mode and PRESERVES the undo slot (orthogonal to the tree)', () => {
  // Prime an undo slot with a real tree mutation (an edge split is undoable).
  let state = paneModel.initialWorkspaceState(onePane())
  state = paneModel.workspaceReducer(state, {
    type: 'OPEN_TAB_AT', tab: makeTab('app', '9'),
    target: { paneId: 'p0', edge: 'right' },
  })
  assert.ok(state.undo, 'the split armed an undo slot')
  const undoBefore = state.undo
  // Flip the view-mode: the slot must survive so the split stays undoable.
  const flipped = paneModel.workspaceReducer(state, { type: 'SET_VIEW_MODE', mode: 'toggle' })
  assert.equal(flipped.ws.viewMode, 'single')
  assert.equal(flipped.undo, undoBefore, 'the undo slot is untouched by a view flip')
})

test('SET_VIEW_MODE is a no-op (same state) when the mode is unchanged', () => {
  const state = paneModel.initialWorkspaceState(onePane())
  assert.equal(paneModel.workspaceReducer(state, { type: 'SET_VIEW_MODE', mode: 'panes' }), state)
})

test('SET_VIEW_MODE accepts an explicit mode (the single-leaf split-drop path)', () => {
  const state = paneModel.initialWorkspaceState(paneModel.setViewMode(onePane(), 'single'))
  const next = paneModel.workspaceReducer(state, { type: 'SET_VIEW_MODE', mode: 'panes' })
  assert.equal(next.ws.viewMode, 'panes')
})

test('UNDO_LAST restores the captured tree but KEEPS the current view-mode', () => {
  // Move a tab (undoable) in panes mode, then toggle to single, then undo. The
  // undo must revert the move WITHOUT reverting the view flip.
  let state = paneModel.initialWorkspaceState(twoPanes())
  const beforeLeaves = Object.keys(state.ws.panes).length
  // Close a pane (undoable, changes the tree) to arm a distinctive undo target.
  state = paneModel.workspaceReducer(state, { type: 'CLOSE_PANE', paneId: state.ws.focusedPaneId })
  assert.notEqual(Object.keys(state.ws.panes).length, beforeLeaves, 'the close changed the tree')
  state = paneModel.workspaceReducer(state, { type: 'SET_VIEW_MODE', mode: 'toggle' })
  assert.equal(state.ws.viewMode, 'single')
  const undone = paneModel.workspaceReducer(state, { type: 'UNDO_LAST' })
  assert.equal(Object.keys(undone.ws.panes).length, beforeLeaves, 'the tree change is reverted')
  assert.equal(undone.ws.viewMode, 'single', 'the view-mode flip is NOT reverted')
})
