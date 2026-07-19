import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as paneModel from '../paneModel.js'
import * as tabModel from '../tabModel.js'

const { makeTab } = tabModel

// The two-worlds single-screen slot (codex-modecontext-design.md). The slot is
// the single world's entire memory — one concrete item, independent of the
// builder pane tree. These tests lock the state semantics: forgiving parse,
// seed-once, deletion reconciliation, and the UNDO/RESET_FLAT world-isolation.

function reduce(state, action) { return paneModel.workspaceReducer(state, action) }
function init(ws) { return paneModel.initialWorkspaceState(ws) }

// A two-pane builder workspace: chat 5 on the left (focused), app 42 on the right.
function tiledBuilder() {
  let ws = paneModel.seedFromFlatTabs([makeTab('chat', '5')])
  ws = paneModel.splitPaneWithTab(ws, makeTab('app', '42'), {
    paneId: ws.focusedPaneId, edge: 'right',
  })
  // Re-focus the original chat pane so the seed source is deterministic.
  const chatPane = paneModel.paneOf(ws, 'chat:5')
  ws = paneModel.focusPane(ws, chatPane.id)
  return ws
}

// ── Forgiving parse / normalize (design: forward-compat, no v2 bump) ─────────

test('normalize preserves property ABSENCE as the migration marker', () => {
  const ws = paneModel.seedFromFlatTabs([makeTab('chat', '5')])
  assert.equal('singleScreen' in ws, false, 'a fresh seed has no slot (uninitialized)')
  const n = paneModel.normalize(ws)
  assert.equal('singleScreen' in n, false, 'absence survives normalize')
})

test('normalize sanitizes a present slot; corrupt/settings → explicit null', () => {
  const base = paneModel.seedFromFlatTabs([makeTab('chat', '5')])
  const withGarbage = paneModel.normalize({ ...base, singleScreen: { kind: 'wat', id: 9 } })
  assert.equal(withGarbage.singleScreen, null, 'unknown kind collapses to empty, never focus')
  const withSettings = paneModel.normalize({ ...base, singleScreen: { kind: 'settings', id: 'settings' } })
  assert.equal(withSettings.singleScreen, null, 'Settings never occupies the slot')
  const badApp = paneModel.normalize({ ...base, singleScreen: { kind: 'app', id: 'not-a-number' } })
  assert.equal(badApp.singleScreen, null, 'a non-numeric app id would never resolve')
})

test('normalize coerces a valid slot id to a string and is idempotent', () => {
  const base = paneModel.seedFromFlatTabs([makeTab('chat', '5')])
  const n1 = paneModel.normalize({ ...base, singleScreen: { kind: 'app', id: 42 } })
  assert.deepEqual(n1.singleScreen, { kind: 'app', id: '42' })
  const n2 = paneModel.normalize(n1)
  assert.equal(n2, n1, 'normalize(normalize(ws)) is reference-stable')
})

test('explicit null slot is preserved (initialized empty/home)', () => {
  const base = paneModel.seedFromFlatTabs([makeTab('chat', '5')])
  const n = paneModel.normalize({ ...base, singleScreen: null })
  assert.equal('singleScreen' in n, true)
  assert.equal(n.singleScreen, null)
})

test('a persisted blob with a slot round-trips through parse (blob stays v1)', () => {
  const base = paneModel.seedFromFlatTabs([makeTab('chat', '5')])
  const withSlot = { ...base, singleScreen: { kind: 'app', id: '42' } }
  const raw = paneModel.serializeWorkspace(withSlot)
  const parsed = paneModel.parseWorkspace(raw)
  assert.equal(parsed.v, 1, 'no v2 bump')
  assert.deepEqual(parsed.singleScreen, { kind: 'app', id: '42' })
})

// ── singleScreenKey ──────────────────────────────────────────────────────────

test('singleScreenKey matches tabModel.tabKey shape', () => {
  const base = paneModel.seedFromFlatTabs([])
  assert.equal(paneModel.singleScreenKey({ ...base, singleScreen: { kind: 'chat', id: '5' } }), 'chat:5')
  assert.equal(paneModel.singleScreenKey({ ...base, singleScreen: { kind: 'app', id: '42' } }), 'app:42')
  assert.equal(paneModel.singleScreenKey(base), null, 'absent slot → null')
  assert.equal(paneModel.singleScreenKey({ ...base, singleScreen: null }), null, 'empty slot → null')
})

// ── Seed-once on first builder→single switch ─────────────────────────────────

test('SET_VIEW_MODE to single seeds the slot from the focused item, once', () => {
  const ws = tiledBuilder() // focused chat 5
  const s1 = reduce(init(ws), { type: 'SET_VIEW_MODE', mode: 'single' })
  assert.deepEqual(s1.ws.singleScreen, { kind: 'chat', id: '5' }, 'seeded from focus')
  // Back to panes, focus the app, back to single: the slot is NOT reseeded.
  const s2 = reduce(s1, { type: 'SET_VIEW_MODE', mode: 'panes' })
  const appPane = paneModel.paneOf(s2.ws, 'app:42')
  const s3 = reduce(s2, { type: 'FOCUS', paneId: appPane.id })
  const s4 = reduce(s3, { type: 'SET_VIEW_MODE', mode: 'single' })
  assert.deepEqual(s4.ws.singleScreen, { kind: 'chat', id: '5' },
    'builder focus change never rewrites the single screen')
})

test('seeding skips a Settings-focused builder pane (slot stays empty)', () => {
  let ws = paneModel.seedFromFlatTabs([makeTab('chat', '5')])
  ws = paneModel.openTab(ws, tabModel.settingsTab(), { paneId: ws.focusedPaneId, activate: true })
  const s = reduce(init(ws), { type: 'SET_VIEW_MODE', mode: 'single' })
  assert.equal(s.ws.singleScreen, null, 'Settings focus seeds an empty screen, not Settings')
})

test('seedSingleScreenIfAbsent leaves an explicit null untouched', () => {
  const ws = { ...tiledBuilder(), singleScreen: null }
  const seeded = paneModel.seedSingleScreenIfAbsent(ws)
  assert.equal(seeded, ws, 'an initialized empty screen is never reseeded from focus')
})

// ── SET_SINGLE_SCREEN — the single world's one navigation write ──────────────

test('SET_SINGLE_SCREEN sets the slot and never touches the tree', () => {
  const ws = { ...tiledBuilder(), viewMode: 'single', singleScreen: { kind: 'chat', id: '5' } }
  const before = ws.panes
  const s = reduce(init(ws), { type: 'SET_SINGLE_SCREEN', item: { kind: 'app', id: '42' } })
  assert.deepEqual(s.ws.singleScreen, { kind: 'app', id: '42' })
  assert.equal(s.ws.panes, before, 'the builder pane tree is byte-identical')
})

test('SET_SINGLE_SCREEN preserves a pending builder undo (orthogonal like SET_VIEW_MODE)', () => {
  const ws = { ...tiledBuilder(), viewMode: 'single', singleScreen: null }
  // Arm an undo with a tree move.
  let state = init(ws)
  state = reduce(state, {
    type: 'MOVE_TAB', tabKey: 'app:42', target: { paneId: paneModel.paneOf(ws, 'chat:5').id }, label: 'Moved',
  })
  assert.ok(state.undo, 'undo armed')
  const after = reduce(state, { type: 'SET_SINGLE_SCREEN', item: { kind: 'chat', id: '5' } })
  assert.equal(after.undo, state.undo, 'single-world nav does not clobber the builder undo')
})

test('SET_SINGLE_SCREEN to the same item is a no-op reference', () => {
  const ws = { ...tiledBuilder(), viewMode: 'single', singleScreen: { kind: 'chat', id: '5' } }
  const state = init(ws)
  const after = reduce(state, { type: 'SET_SINGLE_SCREEN', item: { kind: 'chat', id: '5' } })
  assert.equal(after, state)
})

// ── Deletion reconciliation (both worlds atomic) ─────────────────────────────

test('deleting the slot item clears the slot (CLOSE_TAB reason deleted)', () => {
  const ws = { ...tiledBuilder(), singleScreen: { kind: 'app', id: '42' } }
  const s = reduce(init(ws), { type: 'CLOSE_TAB', tabKey: 'app:42', reason: 'deleted' })
  assert.equal(s.ws.singleScreen, null, 'a deleted slot degrades to empty, never to focus')
  assert.equal(paneModel.paneOf(s.ws, 'app:42'), null, 'and the tree tab is gone too')
})

test('deleting a NON-slot item leaves the slot intact', () => {
  const ws = { ...tiledBuilder(), singleScreen: { kind: 'chat', id: '5' } }
  const s = reduce(init(ws), { type: 'CLOSE_TAB', tabKey: 'app:42', reason: 'deleted' })
  assert.deepEqual(s.ws.singleScreen, { kind: 'chat', id: '5' })
})

test('prune clears a slot whose backing item is no longer live', () => {
  const ws = { ...tiledBuilder(), singleScreen: { kind: 'app', id: '42' } }
  // App 42 uninstalled out of band; only app 99 is live.
  const s = reduce(init(ws), { type: 'PRUNE', liveChatIds: ['5'], liveAppIds: ['99'] })
  assert.equal(s.ws.singleScreen, null)
})

test('prune keeps a still-live slot', () => {
  const ws = { ...tiledBuilder(), singleScreen: { kind: 'app', id: '42' } }
  const s = reduce(init(ws), { type: 'PRUNE', liveChatIds: ['5'], liveAppIds: ['42'] })
  assert.deepEqual(s.ws.singleScreen, { kind: 'app', id: '42' })
})

// ── UNDO_LAST world isolation ────────────────────────────────────────────────

test('tree undo carries the CURRENT slot forward, never resurrects an old one', () => {
  let state = init({ ...tiledBuilder(), viewMode: 'single', singleScreen: { kind: 'chat', id: '5' } })
  // A tree move arms an undo whose snapshot holds the old slot.
  state = reduce(state, {
    type: 'MOVE_TAB', tabKey: 'app:42', target: { paneId: paneModel.paneOf(state.ws, 'chat:5').id }, label: 'Moved',
  })
  // Single-world navigation changes the slot AFTER the undo was armed.
  state = reduce(state, { type: 'SET_SINGLE_SCREEN', item: { kind: 'app', id: '42' } })
  // Undo the TREE move — the slot must stay at the current value, not revert.
  const undone = reduce(state, { type: 'UNDO_LAST' })
  assert.deepEqual(undone.ws.singleScreen, { kind: 'app', id: '42' },
    'tree undo does not roll back a later single-world navigation')
})

// ── RESET_FLAT world isolation ───────────────────────────────────────────────

test('RESET_FLAT reseeds the tree but preserves viewMode + slot', () => {
  const ws = { ...tiledBuilder(), viewMode: 'single', singleScreen: { kind: 'app', id: '42' } }
  const s = reduce(init(ws), { type: 'RESET_FLAT', tabs: [makeTab('chat', '7')] })
  assert.equal(s.ws.viewMode, 'single', 'RESET_FLAT must not reset the world')
  assert.deepEqual(s.ws.singleScreen, { kind: 'app', id: '42' }, 'nor the single screen')
  assert.ok(paneModel.paneOf(s.ws, 'chat:7'), 'but the tree was reseeded')
})

// ── World-aware active-content route (the nav adapter's projection) ──────────

test('activeContentRoute reflects the SLOT in single mode, the focused pane in builder', () => {
  const builderWs = tiledBuilder() // focused chat 5, viewMode panes
  assert.deepEqual(paneModel.activeContentRoute(builderWs), {
    view: 'chat', chatId: '5', appId: null, paneId: builderWs.focusedPaneId,
  }, 'builder → focused pane')
  const singleWs = { ...builderWs, viewMode: 'single', singleScreen: { kind: 'app', id: '42' } }
  const r = paneModel.activeContentRoute(singleWs)
  assert.equal(r.view, 'canvas')
  assert.equal(r.appId, 42, 'app id is numeric for the legacy triple')
  assert.equal(r.chatId, null)
})

test('singleScreenRoute: chat slot, app slot, and empty/home', () => {
  const base = paneModel.seedFromFlatTabs([])
  assert.deepEqual(paneModel.singleScreenRoute({ ...base, singleScreen: { kind: 'chat', id: '9' } }), {
    view: 'chat', chatId: '9', appId: null, paneId: base.focusedPaneId,
  })
  const appR = paneModel.singleScreenRoute({ ...base, singleScreen: { kind: 'app', id: '42' } })
  assert.equal(appR.view, 'canvas'); assert.equal(appR.appId, 42)
  // Null/empty slot → the empty chat home surface, never a fabricated id.
  assert.deepEqual(paneModel.singleScreenRoute({ ...base, singleScreen: null }), {
    view: 'chat', chatId: null, appId: null, paneId: base.focusedPaneId,
  })
})
