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
const paneSrc = readFileSync(new URL('../paneModel.js', import.meta.url), 'utf8')

// A minimal v2 exit plan for the reducer's behavioral cases (a plan arms a beat).
function planFor(name = 'shell-mode-deal-out', totalMs = 180) {
  return {
    kind: name === 'shell-mode-deal-in' ? 'enter' : 'exit',
    participants: [{ key: 'chat:2', paneId: 'p2', motion: name === 'shell-mode-deal-in' ? 'deal-in' : 'deal-out', delayMs: 0, durationMs: totalMs }],
    completionNames: [name], totalMs, underlayKey: null, target: null, snapshotSignature: 'sig',
  }
}

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
test('finding 2: the Settings mode-conversion hook is DELETED (nothing converts on flip)', () => {
  // v2 deleted the former no-op convertSettingsForModeTransition entirely: a builder
  // Settings tab SURVIVES the flip and single mode paints its own slot, so there is
  // nothing to convert. No caller, no export, and no mode-convert reducer branch.
  assert.doesNotMatch(nav, /convertSettingsForModeTransition/)
  assert.doesNotMatch(shell, /convertSettingsForModeTransition/)
  assert.doesNotMatch(paneSrc, /reason === 'mode-convert'/)
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

test('bypass hunt: concrete restores funnel; explicit-null restores use the empty-single boundary', () => {
  assert.match(nav, /applyModeDestination\(nextRoute\)/)
  assert.match(nav, /applyModeDestination\(itemRoute\)/)
  // Tombstoned/semantic-home routes deliberately clear the slot directly. They are
  // safe because every workspace dispatch crosses the shared empty-single edge gate.
  assert.match(nav, /SET_SINGLE_SCREEN', item: null/)
  assert.match(shell, /enteredEmptySingleScreen\(\s*prev\.ws, next\.ws/)
  assert.match(shell, /requestEmptySingleNewChatRef\.current\?\.\(\)/)
})

// -- Finding F9 (expanding review): the chat repair/seed paths funnel too --------
test('finding F9: historical-chat repair is builder-only; single mode requests New Chat', () => {
  // Builder repair/boot sites still funnel through the one destination helper and
  // preserve Settings. An emptied single slot must never select fallback/chats[0];
  // the dispatch boundary (or boot policy when no reducer edge occurred) requests
  // the explicit New Chat landing instead.
  assert.match(shell, /applyModeDestination\(\{ view: 'chat', chatId: fallback\.id, appId: null, paneId: ws\.focusedPaneId \}, \{ preserveSettings: true \}\)/)
  assert.match(shell, /applyModeDestination\(\{ view: 'chat', chatId: chats\[0\]\.id, appId: null, paneId: ws\.focusedPaneId \}, \{ preserveSettings: true \}\)/)
  assert.match(shell, /const single = !paneModel\.WORKSPACE_SPLITS_ENABLED \|\| ws\.viewMode === 'single'/)
  assert.match(shell, /if \(single && ws\.singleScreen == null && chats\.length > 0\s*&& pendingNewChatRef\.current == null\) \{\s*requestEmptySingleNewChat\(\)/)
  assert.match(shell, /else if \(!single && focusedPaneEmpty && chats\[0\]\)/)
  assert.match(shell, /const builderEmpty = !single/)
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
  // The completion contract is computed from the COMMITTED render closure, not the
  // render-written ref (W3): the collection effect reads completionContract(state).
  assert.match(controller, /const contract = completionContract\(state\)/)
  // One rAF collection (v2), not two chained frames.
  assert.match(controller, /raf = requestAnimationFrame\(collect\)/)
  // The reducer's id guard rejects a superseded epoch's completion (enter1 -> exit2
  // -> enter3: a delayed complete{enter1} cannot clear enter3).
  let s = { committedMode: 'single', transition: null, nextId: 1 }
  s = modeReducer(s, { type: 'toggle', to: 'panes', presentation: planFor('shell-mode-deal-in'), now: 0 }) // enter, id 1
  const e1 = s.transition.id
  s = modeReducer(s, { type: 'toggle', to: 'single', presentation: planFor('shell-mode-deal-out'), now: 1 }) // exit, id 2
  s = modeReducer(s, { type: 'toggle', to: 'panes', presentation: planFor('shell-mode-deal-in'), now: 2 }) // enter, id 3
  const e3 = s.transition.id
  assert.notEqual(e1, e3)
  assert.equal(modeReducer(s, { type: 'complete', id: e1 }).transition.id, e3, 'stale epoch rejected')
})

// -- Finding 6 / INV 10 / H2: cancelBeat is wired to a plan-signature drift ------
test('finding 6: a topology/geometry/destination change during an exit beat cancels it (INV 10 / H2)', () => {
  assert.match(shell, /mode\.cancelBeat\(\)/)
  // v2: the cancel watcher recomputes the exit signature from the same projection
  // authority AND the live overlay classification, comparing it to the latched
  // snapshotSignature — any drift snaps. H2: the destination inputs (settingsOpenRaw /
  // immersiveAppId) are folded in and in the deps, so a mid-beat destination flip fires it.
  assert.match(shell, /const live = exitSignature\(\{/)
  assert.match(shell, /settingsDestination: settingsOpenRaw/)
  assert.match(shell, /immersiveHolderId: immersiveAppId/)
  assert.match(shell, /\}, \[workspace, projection, contentRect, settingsOpenRaw, immersiveAppId, modeState, mode\]\)/)
  assert.match(shell, /if \(live !== t\.presentation\.snapshotSignature\) mode\.cancelBeat\(\)/)
  // The reducer's cancel-beat clears the descriptor without touching committedMode.
  let s = modeReducer({ committedMode: 'panes', transition: null, nextId: 1 },
    { type: 'toggle', to: 'single', presentation: planFor('shell-mode-deal-out'), now: 0 })
  s = modeReducer(s, { type: 'cancel-beat' })
  assert.equal(s.transition, null)
  assert.equal(s.committedMode, 'single')
})

// -- Finding 7: mode-restoring Undo routes through the controller --------------
test('finding 7: undo routes the mode restoration through mode.undo before UNDO_LAST', () => {
  assert.match(shell, /mode\.undo\(\{ restoredMode, presentation \}\)/)
  assert.match(shell, /const restoredMode = undoSlot\.restoreViewMode\s*\n\s*\? undoSlot\.ws\.viewMode : wsState\.ws\.viewMode/)
  // The undo presentation is derived from the tree the beat animates (v2).
  assert.match(shell, /const presentation = restoredMode === 'panes'\s*\n\s*\? deriveEnterPlan/)
})

// -- Finding R3: the last-tab-close auto-return arms the descriptor same-batch ---
test('finding R3: an emptying close arms the auto-return flip in the SAME batch, no autoFlip API', () => {
  // The auto-return no longer lags a frame: closeTab detects the close will empty
  // the builder tree and dispatches an INSTANT mode flip (cause 'auto') alongside
  // CLOSE_TAB, so committedMode flips to single WITH the tree — not a render later
  // via the passive sync-committed reconcile. It is a normal planned exit, not a
  // separate autoFlip event (that orphaned API was deleted).
  assert.match(shell, /paneModel\.isEmptyTree\(paneModel\.closeTab\(ws, key\)\)\) \{\s*\n\s*mode\.toggle\(\{ cause: 'auto', to: 'single' \}\)/)
  assert.doesNotMatch(controller, /autoFlip/)
  // An emptied-tree flip carries no plan → the machine flips instantly (no beat).
  const flipped = modeReducer({ committedMode: 'panes', transition: null, nextId: 1 },
    { type: 'toggle', cause: 'auto', to: 'single', presentation: null })
  assert.equal(flipped.committedMode, 'single')
  assert.equal(flipped.transition, null)
})

// -- Finding R1: repair/seed preserve an OPEN Settings takeover -----------------
test('finding R1: applyModeDestination only dismisses Settings when NOT preserving it', () => {
  // The setSettingsOpen(false) is gated on !preserveSettings, so a background
  // repair/seed (preserveSettings:true) writes the slot beneath an open takeover
  // without dismissing the owner's Settings view; a user-initiated open still leaves.
  assert.match(nav, /const applyModeDestination = useCallback\(\(route, \{ preserveSettings = false \} = \{\}\)/)
  assert.match(nav, /if \(!preserveSettings\) \{\s*\n\s*setSettingsOpen\(false\)/)
})

// -- Finding R2: a foreground single-world open dismisses the Settings takeover --
test('finding R2: a foreground single-world placement dismisses the Settings takeover', () => {
  // The pure resolver writes the slot BENEATH an open takeover (it cannot clear React
  // state), so Shell dismisses Settings alongside a foreground single-world open —
  // exactly as a user-initiated open does — so the foregrounded item is visible.
  assert.match(shell, /if \(world === 'single'\s*\n\s*&& requests\.some\(r => r && r\.item && r\.activation === ACTIVATE_FOREGROUND\)\) \{\s*\n\s*dismissSettings\(\)/)
  // dismissSettings is a no-op when no takeover is open (guarded in the nav hook).
  assert.match(nav, /const dismissSettings = useCallback\(\(\) => \{\s*\n\s*if \(!settingsOpenRef\.current\) return/)
})

// -- Finding W1: an epoch-keyed completion watchdog bounds a stuck beat ---------
test('finding W1: a bounded completion watchdog force-completes a stranded epoch (INV 7/14)', () => {
  // A finished promise that never resolves while the page stays visible would strand
  // the descriptor (the visibility reconcile only runs on visibilitychange). The
  // watchdog is armed inside the same completion effect, bounded by the plan's
  // totalMs + margin, dispatching complete{captured epoch}; the reducer's stale-epoch
  // guard makes a late fire safe. It is the ONLY correctness timer.
  assert.match(controller, /const watchdog = setTimeout\(settle, contract\.maxMs \+ RECONCILE_SLACK_MS\)/)
  assert.match(controller, /clearTimeout\(watchdog\)/)
  // No other bare setTimeout/setInterval correctness timer lives in the controller.
  const timers = controller.match(/set(Timeout|Interval)\(/g) || []
  assert.equal(timers.length, 1, 'exactly one timer — the watchdog')
})

// -- Finding 8: slot-only app gets a synthetic history owner -------------------
test('finding 8: appOwnerPaneId returns the synthetic single-world owner for a slot app', () => {
  assert.match(nav, /const appOwnerPaneId = useCallback/)
  assert.match(nav, /return paneModel\.SINGLE_SLOT_PANE/)
  assert.match(nav, /const ownerPaneId = appOwnerPaneId\(ws, appId\)/)
  assert.match(nav, /if \(ownerPaneId !== paneModel\.SINGLE_SLOT_PANE\) \{\s*\n\s*dispatchWorkspace\(\{ type: 'FOCUS', paneId: ownerPaneId \}\)/)
  assert.equal(paneModel.SINGLE_SLOT_PANE, '__single__')
})

// -- Finding F5 (expanding review): app visibility + Back are world-aware --------
test('finding F5: appOwnerPaneId is world-aware — single reads the slot, builder the tree', () => {
  // In SINGLE mode the tree/visiblePaneIds branch must NOT run (it names hidden
  // panes); the single branch resolves the slot (or the legacy focused-pane
  // fallback) and returns null for a non-slot app.
  assert.match(nav, /if \(mode === 'single'\) \{[\s\S]*?if \('singleScreen' in ws\)/)
  // The tree-membership + visiblePaneIds check is AFTER the single early-returns,
  // i.e. only reachable in the builder world.
  const single = nav.indexOf("if (mode === 'single')")
  const treeCheck = nav.indexOf('visiblePaneIdsRef.current.has(pane.id)')
  assert.ok(single > 0 && treeCheck > single, 'the visible-set branch is builder-only')
})

test('finding F5: handleBack restores the hidden app through applyModeDestination', () => {
  // The not-visible restore must funnel, not raw OPEN_TAB into the tree, and the
  // FOCUS target is re-derived world-aware (SINGLE_SLOT_PANE skips the tree FOCUS).
  assert.match(nav, /applyModeDestination\(\{\s*\n\s*view: 'canvas', appId: Number\(sourceOwner\.appId\)/)
  assert.match(nav, /const ownerPaneId = appOwnerPaneId\(workspaceStateRef\.current\.ws, sourceOwner\.appId\)/)
  assert.doesNotMatch(nav, /type: 'OPEN_TAB', paneId,\s*\n\s*tab: tabModel\.makeTab\('app', sourceOwner\.appId\)/)
})

// -- Finding 10: exit chrome is keyboard-inert during the latched deal ---------
test('finding 10: WorkspaceChrome is inert during the exit beat, not just pointer-blocked', () => {
  assert.match(shell, /<WorkspaceChrome[\s\S]*?inert=\{modalDrawerOpen \|\| exitBeatActive\}/)
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

// -- Finding F13 (expanding review): the beat carries an HONEST cause -----------
test('finding F13: cause threads from the gesture/keyboard, never a hardcoded hold', () => {
  // The controller forwards the caller's cause instead of hardcoding 'hold'.
  assert.match(controller, /const toggle = useCallback\(\(\{ cause, to, presentation \} = \{\}\)/)
  assert.match(controller, /type: 'toggle', cause, to: dest, from, presentation: plan/)
  assert.doesNotMatch(controller, /type: 'toggle', cause: 'hold'/)
  // Shell forwards the caller's cause into mode.toggle alongside the latched plan.
  assert.match(shell, /mode\.toggle\(\{ cause, presentation \}\)/)
  // Each source layer names its own beat honestly.
  assert.match(gesture, /onToggleMode\?\.\('hold'\)/)
  assert.match(gesture, /onToggleMode\?\.\('swipe'\)/)
  assert.match(brand, /onToggleMode\('keyboard'\)/)
})

// -- Finding 13: reduced motion is reactive (controller + halo) ----------------
test('finding 13: reduced-motion changes settle a live beat and stop the halo rAF', () => {
  assert.match(controller, /matchMedia\('\(prefers-reduced-motion: reduce\)'\)/)
  assert.match(controller, /if \(t && t\.phase !== 'drag-preview'\) dispatch\(\{ type: 'complete', id: t\.id \}\)/)
  assert.match(halo, /const \[reduced, setReduced\] = useState\(prefersReducedMotion\)/)
  assert.match(halo, /\}, \[haloRef, active, reduced\]\)/)
})
