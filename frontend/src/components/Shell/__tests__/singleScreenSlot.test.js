import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as paneModel from '../paneModel.js'
import * as tabModel from '../tabModel.js'

const { makeTab } = tabModel

// The two-worlds single-screen slot (codex-modecontext-design.md). The slot is
// the single world's entire memory — one concrete item, independent of the
// builder pane tree. These tests lock the state semantics: forgiving parse,
// selected-tab handoff, deletion reconciliation, and UNDO/RESET_FLAT isolation.

function reduce(state, action) { return paneModel.workspaceReducer(state, action) }
function init(ws) { return paneModel.initialWorkspaceState(ws) }
function builderSeed(tabs) {
  return paneModel.setViewMode(paneModel.seedFromFlatTabs(tabs), 'panes')
}

// A two-pane builder workspace: chat 5 on the left (focused), app 42 on the right.
function tiledBuilder() {
  let ws = builderSeed([makeTab('chat', '5')])
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
  // A legacy/uninitialized blob has NO singleScreen property, and the seedFromFlatTabs
  // constructor writes none either — so its ABSENCE (the migration marker) is exactly
  // what normalize must preserve.
  const ws = paneModel.seedFromFlatTabs([makeTab('chat', '5')])
  assert.equal('singleScreen' in ws, false, 'a legacy blob has no slot (uninitialized)')
  const n = paneModel.normalize(ws)
  assert.equal('singleScreen' in n, false, 'absence survives normalize')
})

test('the fresh seed has no slot; RESET_FLAT seeds first boot from the active item', () => {
  // Two-worlds: seedFromFlatTabs is a PURE constructor — no single-screen slot
  // (absence is the migration marker, reused by fixtures modelling a legacy blob).
  // The derivations stay borrow-free: a genuinely absent slot is the empty home. The
  // real first-boot seed lives in the RESET_FLAT reducer — the only path that turns a
  // legacy/flat active chat into the live Standard world.
  const chat = paneModel.seedFromFlatTabs([makeTab('chat', '5')])
  assert.equal('singleScreen' in chat, false, 'the constructor writes no slot')
  assert.equal(chat.viewMode, 'single')
  assert.equal(paneModel.activeContentRoute(chat).chatId, null, 'an unseeded slot is the empty home')

  const booted = reduce(init(chat), { type: 'RESET_FLAT', tabs: [makeTab('chat', '5')] }).ws
  assert.deepEqual(booted.singleScreen, { kind: 'chat', id: '5' }, 'RESET_FLAT seeds the active chat')
  assert.equal(paneModel.activeContentRoute(booted).chatId, '5', 'first boot lands on the active chat')

  const bootedApp = reduce(init(paneModel.seedFromFlatTabs([])), {
    type: 'RESET_FLAT', tabs: [makeTab('chat', '5'), makeTab('app', '42')],
  }).ws
  assert.deepEqual(bootedApp.singleScreen, { kind: 'app', id: '42' }, 'the last-active tab seeds the slot')
  assert.equal(paneModel.activeContentRoute(bootedApp).appId, 42)

  const empty = paneModel.seedFromFlatTabs([])
  assert.equal('singleScreen' in empty, false, 'an empty seed has no departing item — slot stays absent')
  assert.equal(paneModel.activeContentRoute(empty).chatId, null, 'an empty first boot starts at home')
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

test('activeKeyForOwner resolves real panes and the synthetic single owner', () => {
  const ws = {
    ...tiledBuilder(),
    singleScreen: { kind: 'chat', id: 'single-chat' },
  }
  const chatPane = paneModel.paneOf(ws, 'chat:5')
  assert.equal(paneModel.activeKeyForOwner(ws, chatPane.id), 'chat:5')
  assert.equal(
    paneModel.activeKeyForOwner(ws, paneModel.SINGLE_SLOT_PANE),
    'chat:single-chat',
  )
  assert.equal(paneModel.activeKeyForOwner(ws, 'missing-pane'), null)
})

test('activeKeyForOwner treats a legacy absent slot as empty — never the focused seed', () => {
  const legacy = paneModel.seedFromFlatTabs([
    { kind: 'chat', id: '5', title: 'Five' },
  ])
  // Strip the seeded slot to model an uninitialized legacy blob (absent marker).
  delete legacy.singleScreen
  assert.equal('singleScreen' in legacy, false)
  // Two-worlds: the Standard owner selects through its OWN slot only. An absent slot
  // is empty (null), never the focused Builder pane's chat.
  assert.equal(
    paneModel.activeKeyForOwner(legacy, paneModel.SINGLE_SLOT_PANE),
    null,
  )

  const initializedEmpty = { ...legacy, singleScreen: null }
  assert.equal(
    paneModel.activeKeyForOwner(initializedEmpty, paneModel.SINGLE_SLOT_PANE),
    null,
  )
})

// ── Builder→Standard selected-tab handoff ─────────────────────────────────

test('SET_VIEW_MODE to single selects the focused Builder tab on every exit', () => {
  const ws = { ...tiledBuilder(), singleScreen: { kind: 'app', id: '42' } }
  const s1 = reduce(init(ws), { type: 'SET_VIEW_MODE', mode: 'single' })
  assert.deepEqual(s1.ws.singleScreen, { kind: 'chat', id: '5' },
    'the focused chat replaces the older Standard app')
  // Back to panes, select the sibling app, then exit again: the current Builder
  // selection wins instead of the chat that Standard most recently displayed.
  const s2 = reduce(s1, { type: 'SET_VIEW_MODE', mode: 'panes' })
  const appPane = paneModel.paneOf(s2.ws, 'app:42')
  const s3 = reduce(s2, { type: 'FOCUS', paneId: appPane.id })
  const s4 = reduce(s3, { type: 'SET_VIEW_MODE', mode: 'single' })
  assert.deepEqual(s4.ws.singleScreen, { kind: 'app', id: '42' },
    'the newly selected Builder app becomes the Standard screen')
})

test('exiting a Settings-focused Builder pane uses its underlying concrete tab', () => {
  let ws = builderSeed([makeTab('chat', '5')])
  ws = paneModel.openTab(ws, tabModel.settingsTab(), { paneId: ws.focusedPaneId, activate: true })
  const s = reduce(init(ws), { type: 'SET_VIEW_MODE', mode: 'single' })
  assert.deepEqual(s.ws.singleScreen, { kind: 'chat', id: '5' },
    'Settings itself is skipped without blanking the single world')
})

test('the Builder exit replaces an explicitly empty Standard slot', () => {
  const ws = { ...tiledBuilder(), singleScreen: null }
  const selected = paneModel.selectFocusedBuilderTabForStandard(ws)
  assert.deepEqual(selected.singleScreen, { kind: 'chat', id: '5' })
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

// ── Empty-builder auto-return + one-gesture undo (owner semantic) ────────────

test('closing the final Builder tab keeps a visible Standard item and can re-enter', () => {
  // Single-pane builder with one chat; close it → the tree empties → auto-return.
  const ws = builderSeed([makeTab('chat', '5')])
  const s = reduce(init(ws), { type: 'CLOSE_TAB', tabKey: 'chat:5' })
  assert.equal(s.ws.viewMode, 'single', 'empty builder auto-returns to single')
  assert.deepEqual(
    s.ws.singleScreen,
    { kind: 'chat', id: '5' },
    'a legacy workspace carries the departing visible tab into Standard',
  )
  assert.equal(s.undo.restoreViewMode, true, 'the undo is flagged one-gesture')

  const fromNewChat = {
    ...builderSeed([makeTab('chat', '5')]),
    singleScreen: null,
  }
  const carried = reduce(init(fromNewChat), { type: 'CLOSE_TAB', tabKey: 'chat:5' })
  assert.deepEqual(
    carried.ws.singleScreen,
    { kind: 'chat', id: '5' },
    'an initialized empty Standard screen still keeps the departing visible tab',
  )
  const reentered = reduce(carried, { type: 'SET_VIEW_MODE', mode: 'panes' })
  assert.equal(reentered.ws.viewMode, 'panes')
  assert.ok(paneModel.paneOf(reentered.ws, 'chat:5'))
})

test('an empty builder tree enters by seeding the current Standard chat or app', () => {
  for (const singleScreen of [{ kind: 'chat', id: '5' }, { kind: 'app', id: '42' }]) {
    const empty = {
      ...paneModel.seedFromFlatTabs([]),
      singleScreen,
    }
    const entered = reduce(init(empty), { type: 'SET_VIEW_MODE', mode: 'panes' })
    assert.equal(entered.ws.viewMode, 'panes')
    assert.ok(
      paneModel.paneOf(entered.ws, `${singleScreen.kind}:${singleScreen.id}`),
      'the Standard screen becomes the first Builder tab',
    )
    assert.deepEqual(entered.ws.singleScreen, singleScreen, 'Standard keeps its independent slot')
  }

  const empty = paneModel.seedFromFlatTabs([])
  const refused = reduce(init(empty), { type: 'SET_VIEW_MODE', mode: 'panes' })
  assert.equal(refused.ws.viewMode, 'single', 'the New Chat landing has no concrete tab to seed')

  const stale = JSON.stringify({ ...empty, viewMode: 'panes' })
  assert.equal(
    paneModel.parseWorkspace(stale).viewMode,
    'single',
    'a persisted empty Builder repairs to Standard at boot',
  )
})

test('a drawer drop into an empty Builder preserves a Standard chat or app as the first tab', () => {
  for (const [singleScreen, dropped] of [
    [{ kind: 'chat', id: '5' }, makeTab('app', '42')],
    [{ kind: 'app', id: '42' }, makeTab('chat', '5')],
  ]) {
    const ws = {
      ...paneModel.seedFromFlatTabs([]),
      singleScreen,
    }
    const state = reduce(init(ws), {
      type: 'OPEN_TAB_AT',
      tab: dropped,
      target: { paneId: ws.focusedPaneId },
      flipViewMode: 'panes',
    })
    const pane = state.ws.panes[state.ws.focusedPaneId]
    assert.equal(state.ws.viewMode, 'panes')
    assert.deepEqual(pane.tabs.map(tabModel.tabKey), [
      `${singleScreen.kind}:${singleScreen.id}`,
      tabModel.tabKey(dropped),
    ])
    assert.equal(
      pane.activeTabKey,
      tabModel.tabKey(dropped),
      'the dropped item activates without replacing Standard',
    )
    assert.deepEqual(state.ws.singleScreen, ws.singleScreen)
  }
})

test('a refused drawer drop does not enter or seed Builder', () => {
  const ws = {
    ...paneModel.seedFromFlatTabs([]),
    singleScreen: { kind: 'chat', id: '5' },
  }
  const state = reduce(init(ws), {
    type: 'OPEN_TAB_AT',
    tab: makeTab('app', '42'),
    target: { paneId: 'missing', edge: 'right' },
    flipViewMode: 'panes',
  })
  assert.equal(state.ws, ws)
  assert.equal(state.ws.viewMode, 'single')
  assert.equal(paneModel.isEmptyTree(state.ws), true)
})

test('deleting the last builder resource also leaves no empty builder behind', () => {
  const ws = builderSeed([makeTab('chat', '5')])
  const state = reduce(init(ws), {
    type: 'CLOSE_TAB', tabKey: 'chat:5', reason: 'deleted',
  })
  assert.equal(state.ws.viewMode, 'single')
  assert.equal(state.undo, null, 'resource deletion remains non-undoable workspace state')
})

test('undo of the last-tab close restores the tab AND builder mode as one gesture', () => {
  const ws = builderSeed([makeTab('chat', '5')])
  let state = reduce(init(ws), { type: 'CLOSE_TAB', tabKey: 'chat:5' })
  state = reduce(state, { type: 'UNDO_LAST' })
  assert.equal(state.ws.viewMode, 'panes', 'builder mode restored')
  assert.ok(paneModel.paneOf(state.ws, 'chat:5'), 'and the closed tab restored')
})

test('closing a tab that leaves others does NOT auto-return', () => {
  const ws = builderSeed([makeTab('chat', '5'), makeTab('chat', '6')])
  const s = reduce(init(ws), { type: 'CLOSE_TAB', tabKey: 'chat:5' })
  assert.equal(s.ws.viewMode, 'panes', 'still builder — other tabs remain')
  assert.equal(s.undo.restoreViewMode, false)
})

test('closing the last tab in SINGLE mode does not auto-return (already single)', () => {
  const ws = { ...paneModel.seedFromFlatTabs([makeTab('chat', '5')]), viewMode: 'single', singleScreen: { kind: 'chat', id: '5' } }
  const s = reduce(init(ws), { type: 'CLOSE_TAB', tabKey: 'chat:5' })
  assert.equal(s.ws.viewMode, 'single')
  assert.equal(s.undo.restoreViewMode, false)
})

test('CLOSE_PANE that empties the builder auto-returns to single', () => {
  const ws = builderSeed([makeTab('chat', '5')])
  const s = reduce(init(ws), { type: 'CLOSE_PANE', paneId: ws.focusedPaneId })
  assert.equal(s.ws.viewMode, 'single')
  assert.deepEqual(s.ws.singleScreen, { kind: 'chat', id: '5' })
  assert.equal(s.undo.restoreViewMode, true)
})

test('a real later mode choice still rebases a coupled undo to tree-only', () => {
  let state = init({
    ...paneModel.seedFromFlatTabs([makeTab('chat', '5')]),
    viewMode: 'single',
  })
  state = reduce(state, {
    type: 'OPEN_TAB_AT',
    tab: makeTab('app', '42'),
    target: { paneId: state.ws.focusedPaneId, edge: 'right' },
    flipViewMode: 'panes',
  })
  assert.equal(state.undo.restoreViewMode, true)

  state = reduce(state, { type: 'SET_VIEW_MODE', mode: 'single' })
  assert.equal(state.undo.restoreViewMode, false, 'the accepted mode choice rebases the undo')
  state = reduce(state, { type: 'UNDO_LAST' })
  assert.equal(state.ws.viewMode, 'single', 'tree undo preserves the later Standard choice')
  assert.equal(Object.keys(state.ws.panes).length, 1, 'the split itself is still undone')
})

test('singleScreenRoute: chat, installed app, Apps launcher, and empty/home', () => {
  const base = paneModel.seedFromFlatTabs([])
  assert.deepEqual(paneModel.singleScreenRoute({ ...base, singleScreen: { kind: 'chat', id: '9' } }), {
    view: 'chat', chatId: '9', appId: null, paneId: base.focusedPaneId,
  })
  const appR = paneModel.singleScreenRoute({ ...base, singleScreen: { kind: 'app', id: '42' } })
  assert.equal(appR.view, 'canvas'); assert.equal(appR.appId, 42)
  assert.deepEqual(
    paneModel.singleScreenRoute({ ...base, singleScreen: tabModel.appsTab() }),
    { view: 'apps', chatId: null, appId: null, paneId: base.focusedPaneId },
  )
  // Null/empty slot → the empty chat home surface, never a fabricated id.
  assert.deepEqual(paneModel.singleScreenRoute({ ...base, singleScreen: null }), {
    view: 'chat', chatId: null, appId: null, paneId: base.focusedPaneId,
  })
})
