import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import * as paneModel from '../paneModel.js'
import * as tabModel from '../tabModel.js'
import { modeReducer } from '../modeMachine.js'
import { softStripKeyframes } from '../useModeViewTransition.js'

// Permanent regression locks for Codex's flagship review round 2 (15 findings).
// Each fix is pinned here — a grep-level lock on the sanctioned funnels plus
// behavioral reducer cases — so a future edit that reintroduces a bypass, a
// destructive Settings conversion, a stale drag epoch, or an un-clamped nav fails
// loudly. DOM/timing cells (hold boundaries, BFCache, drag blur) are covered by
// tests/mode-transition.spec.mjs.

const shell = readFileSync(new URL('../Shell.jsx', import.meta.url), 'utf8')
const workspaceSession = readFileSync(
  new URL('../useWorkspaceSession.js', import.meta.url),
  'utf8',
)
const nav = readFileSync(new URL('../../../hooks/useNavigation.js', import.meta.url), 'utf8')
const controller = readFileSync(new URL('../useModeController.js', import.meta.url), 'utf8')
const scene = readFileSync(new URL('../useModeViewTransition.js', import.meta.url), 'utf8')
const gesture = readFileSync(new URL('../useLogoModeGesture.js', import.meta.url), 'utf8')
const brand = readFileSync(new URL('../ShellBrand.jsx', import.meta.url), 'utf8')
const paneSrc = readFileSync(new URL('../paneModel.js', import.meta.url), 'utf8')

const { makeTab } = tabModel
function reduce(state, action) { return paneModel.workspaceReducer(state, action) }
function init(ws) { return paneModel.initialWorkspaceState(ws) }

// -- Finding 1 (BLOCKER): dragArm computes the epoch id BEFORE dispatch --------
test('finding 1: dragArm returns the epoch it WILL assign, computed before dispatch', () => {
  // Reading stateRef AFTER an async useReducer dispatch returns the stale
  // pre-dispatch epoch, so cancel/blur would carry the wrong id and never clear the
  // preview -- the wedge reincarnated. The id is read from nextId before dispatch.
  assert.match(controller, /const id = current\.committedMode === 'single' \? current\.nextId : null\s*\n\s*dispatch\(\{ type: 'drag-arm'/)
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
  assert.match(nav, /if \(mode === 'single'\) \{\s*if \(!blobValid\) openBootTab\(tab\)\s*applyModeDestination\(route\)/,
    'a fresh Standard deep link seeds both the implicit tree and the single-screen slot')
})

test('finding 4: non-history newChat routes through applyModeDestination (not a direct OPEN_TAB)', () => {
  assert.match(shell, /applyModeDestination\(\{ view: 'chat', chatId, appId: null, paneId: ws\.focusedPaneId \}\)/)
  assert.doesNotMatch(shell, /type: 'OPEN_TAB', paneId: ws\.focusedPaneId,\s*\n\s*tab: tabModel\.makeTab\('chat', chatId\)/)
})

test('finding 9: applyModeDestination branches on the canonical workspace world', () => {
  assert.match(nav, /const mode = ws\.viewMode/)
  assert.match(nav, /if \(mode === 'single'\) \{[\s\S]*?SET_SINGLE_SCREEN/)
})

test('bypass hunt: concrete restores funnel; explicit-null restores use the empty-single boundary', () => {
  assert.match(nav, /applyModeDestination\(nextRoute\)/)
  assert.match(nav, /applyModeDestination\(itemRoute\)/)
  // Tombstoned/semantic-home routes deliberately clear the slot directly. They are
  // safe because every workspace dispatch crosses the shared empty-single edge gate.
  assert.match(nav, /SET_SINGLE_SCREEN', item: null/)
  assert.match(workspaceSession, /enteredEmptySingleScreen\(\s*prev\.ws, next\.ws/)
  assert.match(workspaceSession, /requestEmptySingleNewChatRef\.current\?\.\(\)/)
})

// -- Finding F9 (expanding review): the chat repair/seed paths funnel too --------
test('finding F9: historical-chat repair is builder-only; single mode requests New Chat', () => {
  // Builder repair/boot sites still funnel through the one destination helper and
  // preserve Settings. An emptied single slot must never select fallback/chats[0];
  // the dispatch boundary (or boot policy when no reducer edge occurred) requests
  // the explicit New Chat landing instead.
  assert.match(shell, /applyModeDestination\(\{ view: 'chat', chatId: fallback\.id, appId: null, paneId: ws\.focusedPaneId \}, \{ preserveSettings: true \}\)/)
  assert.match(shell, /applyModeDestination\(\{ view: 'chat', chatId: chats\[0\]\.id, appId: null, paneId: ws\.focusedPaneId \}, \{ preserveSettings: true \}\)/)
  assert.match(shell, /const single = ws\.viewMode === 'single'/)
  assert.match(shell, /if \(single && ws\.singleScreen == null && chats\.length > 0\s*&& pendingNewChatRef\.current == null\) \{\s*requestEmptySingleNewChat\(\)/)
  assert.match(shell, /else if \(!single && focusedPaneEmpty && chats\[0\]\)/)
  assert.match(shell, /const builderEmpty = !single/)
  // No repair path OPEN_TABs a fallback/seed chat into the tree any more.
  assert.doesNotMatch(shell, /type: 'OPEN_TAB', paneId: ws\.focusedPaneId,\s*\n\s*tab: tabModel\.makeTab\('chat', fallback\.id\)/)
  assert.doesNotMatch(shell, /type: 'OPEN_TAB', paneId: ws\.focusedPaneId,\s*\n\s*tab: tabModel\.makeTab\('chat', chats\[0\]\.id\)/)
})

// -- Finding 5: scene completion is epoch-keyed -------------------------------
test('finding 5: completion settles only the originating browser scene', () => {
  assert.match(scene, /const id = nextIdRef\.current\+\+/)
  assert.match(scene, /if \(liveRef\.current\?\.descriptor\.id !== id\) return/)
  assert.match(scene, /transition\.finished\.then\([\s\S]*?settle\(id\)/)
  assert.doesNotMatch(scene, /animationend|setTimeout/)
})

// -- Finding 6: scene geometry is captured once, never retargeted live ---------
test('finding 6: participant snapshots are collected once at the world boundary', () => {
  assert.match(scene, /let snapshots = direction === 'exit'\s*\n\s*\? participantSnapshots/)
  assert.match(scene, /if \(direction === 'enter'\) snapshots = participantSnapshots/)
  assert.doesNotMatch(shell, /transitionSignature|cancelBeat|snapshotSignature/)
})

test('finding 6: exit enables capture names before collecting departing panes', () => {
  const enableAt = scene.indexOf('html.dataset.modeViewTransition = direction')
  const collectAt = scene.indexOf("let snapshots = direction === 'exit'")
  assert.ok(enableAt >= 0, 'root capture attribute should be enabled')
  assert.ok(collectAt >= 0, 'departing pane snapshots should be collected')
  assert.ok(enableAt < collectAt, 'capture names must exist before exit reads them')
})

// -- Finding 7: every actual reducer mode change synchronizes presentation -----
test('finding 7: undo relies on the shared actual-transition synchronizer', () => {
  assert.match(shell, /modeView\.run\(\{[\s\S]*?cause: 'undo'/)
  assert.match(shell, /onWorkspaceTransitionRef\.current = \(prevWs, nextWs\) => \{[\s\S]*?mode\.syncCommitted\(nextWs\.viewMode\)/)
  assert.match(shell, /update: \(\) => \{\s*\n\s*dispatchWorkspace\(\{ type: 'UNDO_LAST' \}\)/)
  assert.doesNotMatch(shell, /mode\.undo/)
  assert.match(shell, /const restoredMode = undoSlot\.restoreViewMode\s*\n\s*\? undoSlot\.ws\.viewMode : wsState\.ws\.viewMode/)
  assert.match(shell, /const plan = deriveModeSnapshotPlan\(\{/)
})

// -- Finding R3: the last-tab-close auto-return arms the descriptor same-batch ---
test('finding R3: an emptying close presents the reducer actual auto-return in the same batch', () => {
  assert.match(shell, /const closeTab = useCallback\(\(tab, \{ reason \} = \{\}\) => \{[\s\S]*?dispatchWorkspace\(\{ type: 'CLOSE_TAB', tabKey: key, reason \}\)/)
  assert.match(shell, /prevWs\.viewMode !== nextWs\.viewMode[\s\S]*?mode\.syncCommitted\(nextWs\.viewMode\)/)
  assert.doesNotMatch(shell, /mode\.toggle/)
  assert.doesNotMatch(controller, /autoFlip/)
  // The reducer's actual result, not the close request, owns presentation.
  const flipped = modeReducer({ committedMode: 'panes', transition: null, nextId: 1 },
    { type: 'sync-committed', committedMode: 'single' })
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

// -- Finding W1: browser completion replaces a guessed watchdog ----------------
test('finding W1: transition.finished owns completion with no correctness timer', () => {
  assert.match(scene, /transition\.finished\.then\([\s\S]*?settle\(id\)/)
  assert.match(scene, /transition\.skipTransition\(\)/)
  assert.doesNotMatch(scene, /setTimeout|setInterval/)
  assert.doesNotMatch(controller, /setTimeout|setInterval|getAnimations/)
})

test('mode preparation is hidden behind the captured old scene', () => {
  assert.match(scene, /html\.dataset\.modeViewTransition = direction[\s\S]*?let snapshots[\s\S]*?let transition/)
  assert.match(scene, /document\.startViewTransition\(\(\) => \{[\s\S]*?flushSync/)
  assert.match(scene, /transition\.ready\.then\(\(\) => \{/)
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
test('finding 10: WorkspaceChrome is inert during either mode beat, not just pointer-blocked', () => {
  assert.match(shell, /<WorkspaceChrome[\s\S]*?inert=\{navigationSurfaceOpen \|\| modeBeatActive\}/)
})

// -- Finding 11: a live hold cancels on hide/blur/pagehide/lostpointercapture --
test('finding 11: the hold cancels on the page-lifecycle interruptions', () => {
  assert.match(gesture, /window\.addEventListener\('blur', cancel\)/)
  assert.match(gesture, /window\.addEventListener\('pagehide', cancel\)/)
  assert.match(gesture, /document\.addEventListener\('visibilitychange', onHidden\)/)
  assert.match(gesture, /const onLostPointerCapture = useCallback/)
  assert.match(brand, /onLostPointerCapture=\{logoGesture\.onLostPointerCapture\}/)
})

// -- Finding 12: Shift+Enter e.repeat guard + keyboardModeClickRef cleanup -----
test('finding 12: Shift+Enter ignores auto-repeat and clears its click-suppression on keyup', () => {
  assert.match(brand, /shortcutMatches\(e, SHELL_SHORTCUTS\.toggleBuilder\)/)
  assert.match(brand, /onKeyUp=\{\(e\) => \{[\s\S]*?keyboardModeClickRef\.current = false/)
})

// -- Finding F13 (expanding review): the beat carries an HONEST cause -----------
test('finding F13: cause threads from the gesture/keyboard, never a hardcoded hold', () => {
  // Cause belongs to the browser scene only; committed mode comes from workspace.
  assert.doesNotMatch(controller, /cause|type: 'toggle'/)
  assert.match(shell, /cause,\s*\n\s*plan,\s*\n\s*update:/)
  assert.match(shell, /dispatchWorkspace\(\{ type: 'SET_VIEW_MODE', mode: to \}\)/)
  // Each source layer names its own beat honestly.
  assert.match(gesture, /onToggleMode\?\.\('hold'\)/)
  assert.match(gesture, /onToggleMode\?\.\('swipe'\)/)
  assert.match(brand, /onToggleMode\('keyboard'\)/)
})

// -- Finding 13: reduced motion takes the instant scene path ------------------
test('finding 13: reduced motion bypasses capture; the brand has no perpetual rAF', () => {
  assert.match(scene, /!prefersReducedMotion\(\)/)
  assert.match(scene, /if \(!supported\) \{\s*\n\s*flushSync\(update\)/)
  assert.doesNotMatch(brand, /requestAnimationFrame|useLivingHalo|logo-halo/)
})

test('single-pane Builder strip enters from above using its rendered height', () => {
  assert.deepEqual(softStripKeyframes('enter', 34), [
    { opacity: 1, transform: 'translate3d(0, -34px, 0)' },
    { opacity: 1, transform: 'translate3d(0, 0, 0)' },
  ])
  assert.match(scene, /getBoundingClientRect\(\)\.height/)
  assert.doesNotMatch(scene, /translate3d\(0, 12px, 0\)/)
})

test('single-pane Builder strip exits upward on the same vertical path', () => {
  assert.deepEqual(softStripKeyframes('exit', 34), [
    { opacity: 1, transform: 'translate3d(0, 0, 0)' },
    { opacity: 1, transform: 'translate3d(0, -34px, 0)' },
  ])
})
