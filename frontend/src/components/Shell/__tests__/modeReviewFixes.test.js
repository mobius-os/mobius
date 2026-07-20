import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import * as paneModel from '../paneModel.js'
import * as tabModel from '../tabModel.js'
import { modeReducer } from '../modeMachine.js'

// Permanent regression locks for Codex's flagship review round 2 (15 findings).
// Each fix is pinned here — a grep-level lock on the sanctioned funnels plus
// behavioral reducer cases — so a future edit that reintroduces a bypass, a
// destructive Settings conversion, a stale drag epoch, or an un-clamped nav fails
// loudly. DOM/timing cells (hold boundaries, BFCache, drag blur) are covered by
// tests/mode-transition.spec.mjs.

const shell = readFileSync(new URL('../Shell.jsx', import.meta.url), 'utf8')
const nav = readFileSync(new URL('../../../hooks/useNavigation.js', import.meta.url), 'utf8')
const controller = readFileSync(new URL('../useModeController.js', import.meta.url), 'utf8')
const gesture = readFileSync(new URL('../useLogoModeGesture.js', import.meta.url), 'utf8')
const brand = readFileSync(new URL('../ShellBrand.jsx', import.meta.url), 'utf8')
const halo = readFileSync(new URL('../useLivingHalo.js', import.meta.url), 'utf8')

const { makeTab } = tabModel
function reduce(state, action) { return paneModel.workspaceReducer(state, action) }
function init(ws) { return paneModel.initialWorkspaceState(ws) }

// -- Finding 1 (BLOCKER): dragArm computes the epoch id BEFORE dispatch --------
test('finding 1: dragArm returns the epoch it WILL assign, computed before dispatch', () => {
  // Reading stateRef AFTER an async useReducer dispatch returns the stale
  // pre-dispatch epoch, so cancel/blur would carry the wrong id and never clear the
  // preview -- the wedge reincarnated. The id is read from nextId before dispatch.
  assert.match(controller, /const id = s\.nextId\s*\n\s*dispatch\(\{ type: 'drag-arm'/)
  // The reducer really assigns nextId as the transition id, so that pre-computed id
  // is exactly what a later cancel/commit must carry.
  const armed = modeReducer({ committedMode: 'single', transition: null, nextId: 7 },
    { type: 'drag-arm', now: 0 })
  assert.equal(armed.transition.id, 7)
  // A cancel carrying that id clears it; a stale id does not (INV 5/15).
  assert.equal(modeReducer(armed, { type: 'drag-cancel', id: 7 }).transition, null)
  assert.equal(modeReducer(armed, { type: 'drag-cancel', id: 6 }).transition, armed.transition)
})

// -- Finding 2 (BLOCKER): Settings is non-destructive across a world toggle -----
test('finding 2: convertSettingsForModeTransition is a NO-OP (no destructive close/reopen)', () => {
  assert.match(nav, /const convertSettingsForModeTransition = useCallback\(\(\) => \{\}, \[\]\)/)
  assert.doesNotMatch(nav, /convertSettingsForModeTransition[\s\S]*?CLOSE_TAB[\s\S]*?mode-convert/)
})

test('finding 2: a mode toggle PRESERVES a Settings-only pane (tree untouched)', () => {
  // Builder workspace: chat 5 left, Settings alone in a right pane; chat focused
  // (the UNFOCUSED sole Settings pane the review named).
  let ws = paneModel.seedFromFlatTabs([makeTab('chat', '5')])
  ws = paneModel.splitPaneWithTab(ws, tabModel.settingsTab(), { paneId: ws.focusedPaneId, edge: 'right' })
  ws = paneModel.focusPane(ws, paneModel.paneOf(ws, 'chat:5').id)
  const settingsPaneId = paneModel.paneOf(ws, tabModel.SETTINGS_TAB_KEY).id
  const s = reduce(init(ws), { type: 'SET_VIEW_MODE', mode: 'single' })
  assert.ok(paneModel.paneOf(s.ws, tabModel.SETTINGS_TAB_KEY), 'Settings tab survives the toggle')
  assert.ok(s.ws.panes[settingsPaneId], 'its pane is not collapsed')
})

test('finding 2: an undo snapshot restoring single + a Settings tab is not a forbidden state', () => {
  let ws = paneModel.seedFromFlatTabs([makeTab('chat', '5')])
  ws = paneModel.openTab(ws, tabModel.settingsTab(), { paneId: ws.focusedPaneId, activate: true })
  ws = { ...ws, viewMode: 'single', singleScreen: { kind: 'chat', id: '5' } }
  // Single mode paints the SLOT (chat 5), not the Settings tab, so a Settings tab
  // in the tree is hidden -- no painted single+Settings.
  assert.equal(paneModel.singleScreenKey(ws), 'chat:5')
  assert.notEqual(paneModel.singleScreenKey(ws), tabModel.SETTINGS_TAB_KEY)
})

// -- Findings 3/4/9: all nav paths funnel through the ONE decision point --------
test('finding 3: the deep-link boot routes through applyModeDestination', () => {
  assert.match(nav, /const bootDeepLink = \(route, tab\) => \{[\s\S]*?applyModeDestination\(route\)/)
})

test('finding 4: non-history newChat routes through applyModeDestination (not a direct OPEN_TAB)', () => {
  assert.match(shell, /applyModeDestination\(\{ view: 'chat', chatId, appId: null, paneId: ws\.focusedPaneId \}\)/)
  assert.doesNotMatch(shell, /type: 'OPEN_TAB', paneId: ws\.focusedPaneId,\s*\n\s*tab: tabModel\.makeTab\('chat', chatId\)/)
})

test('finding 9: applyModeDestination clamps the kill switch BEFORE the world branch', () => {
  assert.match(nav, /const mode = paneModel\.WORKSPACE_SPLITS_ENABLED \? ws\.viewMode : 'single'/)
  assert.match(nav, /if \(mode === 'single'\) \{[\s\S]*?SET_SINGLE_SCREEN/)
})

test('bypass hunt: navTo and restoreRoute apply destinations ONLY through applyModeDestination', () => {
  assert.match(nav, /applyModeDestination\(nextRoute\)/)
  assert.match(nav, /applyModeDestination\(itemRoute\)/)
})

// -- Finding F9 (expanding review): the chat repair/seed paths funnel too --------
test('finding F9: mounted-chat + boot chat repair route through applyModeDestination, world-aware', () => {
  // All three repair/seed sites (mounted-chat 404, boot chat-list seed, restored-
  // chat 404) must set the fallback via the ONE decision point, not a raw OPEN_TAB
  // into the hidden pane tree — else single mode paints an empty screen while the
  // "repaired" chat sits invisible in the tree (INV 2/4).
  assert.match(shell, /applyModeDestination\(\{ view: 'chat', chatId: fallback\.id, appId: null, paneId: ws\.focusedPaneId \}\)/)
  assert.match(shell, /applyModeDestination\(\{ view: 'chat', chatId: chats\[0\]\.id, appId: null, paneId: ws\.focusedPaneId \}\)/)
  // The "visible world is empty" guard is mode-aware: single checks a null slot.
  assert.match(shell, /const single = !paneModel\.WORKSPACE_SPLITS_ENABLED \|\| ws\.viewMode === 'single'/)
  assert.match(shell, /\? ws\.singleScreen == null/)
  // No repair path OPEN_TABs a fallback/seed chat into the tree any more.
  assert.doesNotMatch(shell, /type: 'OPEN_TAB', paneId: ws\.focusedPaneId,\s*\n\s*tab: tabModel\.makeTab\('chat', fallback\.id\)/)
  assert.doesNotMatch(shell, /type: 'OPEN_TAB', paneId: ws\.focusedPaneId,\s*\n\s*tab: tabModel\.makeTab\('chat', chats\[0\]\.id\)/)
})

// -- Finding 5: completion is epoch-keyed (captured epoch, not inferred) --------
test('finding 5: completion captures the originating epoch, not the current transition id', () => {
  assert.match(controller, /const epoch = contract\.id/)
  assert.match(controller, /dispatch\(\{ type: 'complete', id: epoch \}\)/)
  assert.match(controller, /Promise\.allSettled\(anims\.map\(a => a\.finished\)\)/)
  assert.doesNotMatch(controller, /addEventListener\('animationend'/)
  // The reducer's id guard rejects a superseded epoch's completion (enter1 -> exit2
  // -> enter3: a delayed complete{enter1} cannot clear enter3).
  let s = { committedMode: 'single', transition: null, nextId: 1 }
  s = modeReducer(s, { type: 'toggle', to: 'panes', multiPane: false, now: 0 }) // enter, id 1
  const e1 = s.transition.id
  s = modeReducer(s, { type: 'toggle', to: 'single', multiPane: true, leavingPaneIds: ['p2'], now: 1 }) // exit, id 2
  s = modeReducer(s, { type: 'toggle', to: 'panes', multiPane: false, now: 2 }) // enter, id 3
  const e3 = s.transition.id
  assert.notEqual(e1, e3)
  assert.equal(modeReducer(s, { type: 'complete', id: e1 }).transition.id, e3, 'stale epoch rejected')
})

// -- Finding 6: cancelBeat is actually wired to topology mutation --------------
test('finding 6: a topology mutation during an exit beat cancels it (cancelBeat has a caller)', () => {
  assert.match(shell, /mode\.cancelBeat\(\)/)
  assert.match(shell, /const settleGone = t\.focusedPaneId != null && !workspace\.panes\[t\.focusedPaneId\]/)
  assert.match(shell, /const leavingGone = t\.leavingPaneIds\.some\(id => !workspace\.panes\[id\]\)/)
  // The reducer's cancel-beat clears the descriptor without touching committedMode.
  let s = modeReducer({ committedMode: 'panes', transition: null, nextId: 1 },
    { type: 'toggle', to: 'single', multiPane: true, leavingPaneIds: ['p2'], now: 0 })
  s = modeReducer(s, { type: 'cancel-beat' })
  assert.equal(s.transition, null)
  assert.equal(s.committedMode, 'single')
})

// -- Finding 7: mode-restoring Undo routes through the controller --------------
test('finding 7: undo routes the mode restoration through mode.undo before UNDO_LAST', () => {
  assert.match(shell, /mode\.undo\(\{\s*\n\s*restoredMode,/)
  assert.match(shell, /const restoredMode = undoSlot\.restoreViewMode\s*\n\s*\? undoSlot\.ws\.viewMode : wsState\.ws\.viewMode/)
})

// -- Finding 8: slot-only app gets a synthetic history owner -------------------
test('finding 8: appOwnerPaneId returns the synthetic single-world owner for a slot app', () => {
  assert.match(nav, /const appOwnerPaneId = useCallback/)
  assert.match(nav, /return paneModel\.SINGLE_SLOT_PANE/)
  assert.match(nav, /const ownerPaneId = appOwnerPaneId\(ws, appId\)/)
  assert.match(nav, /if \(ownerPaneId !== paneModel\.SINGLE_SLOT_PANE\) \{\s*\n\s*dispatchWorkspace\(\{ type: 'FOCUS', paneId: ownerPaneId \}\)/)
  assert.equal(paneModel.SINGLE_SLOT_PANE, '__single__')
})

// -- Finding 10: exit chrome is keyboard-inert during the latched deal ---------
test('finding 10: WorkspaceChrome is inert during the exit beat, not just pointer-blocked', () => {
  assert.match(shell, /<WorkspaceChrome[\s\S]*?inert=\{modalDrawerOpen \|\| exitGeometryActive\}/)
})

// -- Finding 11: a live hold cancels on hide/blur/pagehide/lostpointercapture --
test('finding 11: the hold cancels on the page-lifecycle interruptions', () => {
  assert.match(gesture, /window\.addEventListener\('blur', cancel\)/)
  assert.match(gesture, /window\.addEventListener\('pagehide', cancel\)/)
  assert.match(gesture, /document\.addEventListener\('visibilitychange', onHidden\)/)
  assert.match(gesture, /const onLostPointerCapture = useCallback/)
  assert.match(brand, /onLostPointerCapture=\{splitsEnabled \? logoGesture\.onLostPointerCapture : undefined\}/)
})

// -- Finding 12: Shift+Enter e.repeat guard + keyboardModeClickRef cleanup -----
test('finding 12: Shift+Enter ignores auto-repeat and clears its click-suppression on keyup', () => {
  assert.match(brand, /e\.shiftKey && e\.key === 'Enter' && !e\.repeat/)
  assert.match(brand, /onKeyUp=\{\(e\) => \{[\s\S]*?keyboardModeClickRef\.current = false/)
})

// -- Finding 13: reduced motion is reactive (controller + halo) ----------------
test('finding 13: reduced-motion changes settle a live beat and stop the halo rAF', () => {
  assert.match(controller, /matchMedia\('\(prefers-reduced-motion: reduce\)'\)/)
  assert.match(controller, /if \(t && t\.phase !== 'drag-preview'\) dispatch\(\{ type: 'complete', id: t\.id \}\)/)
  assert.match(halo, /const \[reduced, setReduced\] = useState\(prefersReducedMotion\)/)
  assert.match(halo, /\}, \[haloRef, active, reduced\]\)/)
})
