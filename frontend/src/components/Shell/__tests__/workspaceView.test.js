import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as paneModel from '../paneModel.js'
import * as tabModel from '../tabModel.js'
import {
  deriveContentVisibility, deriveExitPlan, deriveEnterPlan, exitSignature, MODE_MOTION,
} from '../workspaceView.js'

const { makeTab, tabKey } = tabModel
const CONTENT = { x: 0, y: 0, w: 1400, h: 900 }

// A two-pane wide workspace: a chat on the left, an app on the right, the app
// pane focused. This is the layout the immersive-solo regression concerns.
function twoPaneChatAndApp() {
  let ws = paneModel.seedFromFlatTabs([makeTab('chat', '5')])
  // Split a fresh app tab (id 42) off the sole pane onto the right edge; the new
  // pane holds the app and takes focus.
  ws = paneModel.splitPaneWithTab(ws, makeTab('app', '42'), {
    paneId: ws.focusedPaneId, edge: 'right',
  })
  return ws
}

function project(ws) {
  return paneModel.projectLayout(ws, paneModel.modeForRect(CONTENT), CONTENT)
}

test('single-pane app: no chrome, holder full-bleed, that app visible', () => {
  const ws = paneModel.seedFromFlatTabs([makeTab('app', '42')])
  const v = deriveContentVisibility({
    workspace: ws, projection: project(ws),
    settingsOverlayOpen: false, immersiveActive: false, immersiveAppId: null,
  })
  assert.equal(v.multiPane, false)
  assert.equal(v.chromeActive, false)
  assert.equal(v.fullBleedKey, 'app:42')
  assert.deepEqual([...v.visibleAppIds], ['42'])
  assert.equal(v.chatPanesVisible, true)
})

test('multi-pane, no overlay: chrome on, no full-bleed, both actives visible', () => {
  const ws = twoPaneChatAndApp()
  const v = deriveContentVisibility({
    workspace: ws, projection: project(ws),
    settingsOverlayOpen: false, immersiveActive: false, immersiveAppId: null,
  })
  assert.equal(v.multiPane, true)
  assert.equal(v.chromeActive, true)
  // Each active tab is positioned into its pane rect, so nothing is full-bleed.
  assert.equal(v.fullBleedKey, null)
  assert.deepEqual([...v.visibleAppIds], ['42'])
  assert.equal(v.chatPanesVisible, true)
})

test('multi-pane immersive solos the holder over the whole workspace', () => {
  const ws = twoPaneChatAndApp()
  // The focused (right) pane's app 42 holds an applied immersive request.
  const holderKey = tabKey(makeTab('app', '42'))
  assert.equal(ws.panes[ws.focusedPaneId].activeTabKey, holderKey)
  const v = deriveContentVisibility({
    workspace: ws, projection: project(ws),
    settingsOverlayOpen: false, immersiveActive: true, immersiveAppId: 42,
    viewMode: 'single', // immersive-solo is a takeover — single-screen mode only
  })
  // Chrome hidden: no strips or dividers paint over the solo.
  assert.equal(v.chromeActive, false)
  // The holder paints full-bleed over the entire content box.
  assert.equal(v.fullBleedKey, holderKey)
  // Only the holder stays frame-visible; the sibling chat pane hides so it
  // stops painting.
  assert.deepEqual([...v.visibleAppIds], ['42'])
  assert.equal(v.chatPanesVisible, false)
})

test('immersive with a NON-holder in the set never leaks the sibling frame', () => {
  // Build two app panes; the focused one (id 7) holds immersive.
  let ws = paneModel.seedFromFlatTabs([makeTab('app', '3')])
  ws = paneModel.splitPaneWithTab(ws, makeTab('app', '7'), {
    paneId: ws.focusedPaneId, edge: 'right',
  })
  const v = deriveContentVisibility({
    workspace: ws, projection: project(ws),
    settingsOverlayOpen: false, immersiveActive: true, immersiveAppId: 7,
    viewMode: 'single',
  })
  // Sibling app 3 must NOT be in the visible set (it would keep painting).
  assert.deepEqual([...v.visibleAppIds], ['7'])
  assert.equal(v.fullBleedKey, tabKey(makeTab('app', '7')))
})

test('Settings overlay (single mode) hides every pane and frame', () => {
  const ws = twoPaneChatAndApp()
  const v = deriveContentVisibility({
    workspace: ws, projection: project(ws),
    settingsOverlayOpen: true, immersiveActive: false, immersiveAppId: null,
    viewMode: 'single', // the takeover overlay exists ONLY in single-screen mode
  })
  assert.equal(v.chromeActive, false)
  assert.equal(v.focusedActiveKey, null)
  assert.equal(v.visibleAppIds.size, 0)
  assert.equal(v.chatPanesVisible, false)
})

// The ABSOLUTE builder invariant, made structural: in builder mode ('panes') NO
// takeover can seize the workspace — not even if BOTH the overlay flag and an
// immersive request arrive. deriveContentVisibility is the last line of defense,
// so it forces them inert in builder and keeps the single tiled path.
test('builder mode has ZERO takeover branches (overlay + immersive both inert)', () => {
  const ws = twoPaneChatAndApp() // 2 panes, focused pane holds app 42
  const v = deriveContentVisibility({
    workspace: ws, projection: project(ws),
    settingsOverlayOpen: true, immersiveActive: true, immersiveAppId: 42,
    viewMode: 'panes', // builder
  })
  // Tiled render stands: chrome up, sibling panes painting, no solo, no full-bleed.
  assert.equal(v.multiPane, true)
  assert.equal(v.single, false)
  assert.equal(v.chromeActive, true, 'no takeover hides the panes in builder')
  assert.equal(v.chatPanesVisible, true)
  assert.equal(v.fullBleedKey, null, 'tiled — nothing paints over the whole box')
  // The immersive request does NOT solo: both app panes stay visible.
  assert.deepEqual([...v.visibleAppIds].sort(), ['42'])
})

// The named risk, made structural: a builder Settings TAB (overlay closed) must
// NOT hide sibling panes. deriveContentVisibility is blind to the Settings tab —
// it only sees settingsOverlayOpen:false — so the tiled render is unchanged and
// the focused Settings pane is just another full-bleed/paned surface.
test('builder Settings tab does NOT suppress sibling panes', () => {
  let ws = twoPaneChatAndApp()
  // Open Settings into the (focused) app pane, replacing the app as its active tab.
  ws = paneModel.openTab(ws, tabModel.settingsTab(), { paneId: ws.focusedPaneId, activate: true })
  const v = deriveContentVisibility({
    workspace: ws, projection: project(ws),
    settingsOverlayOpen: false, immersiveActive: false, immersiveAppId: null,
  })
  // Two visible leaves → tiled chrome stays on; the sibling chat pane still paints.
  assert.equal(v.multiPane, true)
  assert.equal(v.chromeActive, true, 'panes are NOT hidden behind the Settings tab')
  assert.equal(v.chatPanesVisible, true, 'the sibling chat pane keeps painting')
  // The focused active key is the Settings tab (its wrapper fills that pane rect).
  assert.equal(v.focusedActiveKey, tabModel.SETTINGS_TAB_KEY)
  // Settings is not an app, so it adds no id; a sibling app pane (if active) would
  // still be visible — here the app was replaced by Settings so the set is empty.
  assert.ok(v.visibleAppIds instanceof Set)
})

test('exit restores the ordinary multi-pane view (derivation is stateless)', () => {
  // Re-deriving with immersive cleared returns the exact non-immersive flags —
  // the tree/focus never changed, so exit restores the layout with no remount.
  const ws = twoPaneChatAndApp()
  const projection = project(ws)
  const before = deriveContentVisibility({
    workspace: ws, projection, settingsOverlayOpen: false,
    immersiveActive: false, immersiveAppId: null,
  })
  const after = deriveContentVisibility({
    workspace: ws, projection, settingsOverlayOpen: false,
    immersiveActive: false, immersiveAppId: null,
  })
  assert.equal(after.chromeActive, before.chromeActive)
  assert.equal(after.fullBleedKey, before.fullBleedKey)
  assert.deepEqual([...after.visibleAppIds], [...before.visibleAppIds])
})

// ── Single view-mode (design: view-mode toggle) ─────────────────────────────
//
// Single-mode collapses a preserved multi-pane tree to the focused pane's active
// tab, full-bleed. It reuses the immersive/single-pane full-bleed path but is
// driven by viewMode, not an overlay, and it is orthogonal to both overlays.

test('single-mode, multi-pane, focused app: chrome off, holder full-bleed, only focused app visible', () => {
  const ws = twoPaneChatAndApp() // right (app 42) pane focused
  const v = deriveContentVisibility({
    workspace: ws, projection: project(ws),
    settingsOverlayOpen: false, immersiveActive: false, immersiveAppId: null,
    viewMode: 'single',
  })
  assert.equal(v.single, true)
  assert.equal(v.multiPane, true, 'the tree is preserved — still two leaves')
  assert.equal(v.chromeActive, false, 'no strips/dividers over a single surface')
  assert.equal(v.fullBleedKey, 'app:42', 'the focused pane paints full-bleed')
  // Only the focused pane's app stays frame-visible; the sibling chat pane hides.
  assert.deepEqual([...v.visibleAppIds], ['42'])
})

test('single-mode with a focused CHAT pane paints the chat and hides the sibling app frame', () => {
  // Focus p0 (the chat) instead of the app pane.
  const ws = paneModel.focusPane(twoPaneChatAndApp(), 'p0')
  assert.equal(ws.panes.p0.activeTabKey, tabKey(makeTab('chat', '5')))
  const v = deriveContentVisibility({
    workspace: ws, projection: project(ws),
    settingsOverlayOpen: false, immersiveActive: false, immersiveAppId: null,
    viewMode: 'single',
  })
  assert.equal(v.single, true)
  assert.equal(v.fullBleedKey, 'chat:5', 'the focused chat is the full-bleed surface')
  // The sibling app 42 is NOT focused, so its frame goes visibility:false.
  assert.deepEqual([...v.visibleAppIds], [])
})

test('single-mode preserves the tree: a panes -> single -> panes round-trip restores identical flags', () => {
  const ws = twoPaneChatAndApp()
  const projection = project(ws)
  const panesBefore = deriveContentVisibility({
    workspace: ws, projection, settingsOverlayOpen: false,
    immersiveActive: false, immersiveAppId: null, viewMode: 'panes',
  })
  // Flip to single: the derivation changes, but ws + projection are untouched.
  deriveContentVisibility({
    workspace: ws, projection, settingsOverlayOpen: false,
    immersiveActive: false, immersiveAppId: null, viewMode: 'single',
  })
  const panesAfter = deriveContentVisibility({
    workspace: ws, projection, settingsOverlayOpen: false,
    immersiveActive: false, immersiveAppId: null, viewMode: 'panes',
  })
  assert.equal(panesAfter.single, false)
  assert.equal(panesAfter.chromeActive, panesBefore.chromeActive)
  assert.equal(panesAfter.fullBleedKey, panesBefore.fullBleedKey)
  assert.deepEqual([...panesAfter.visibleAppIds], [...panesBefore.visibleAppIds])
})

test('single-mode yields to Settings: the overlay governs and single is inert', () => {
  const ws = twoPaneChatAndApp()
  const v = deriveContentVisibility({
    workspace: ws, projection: project(ws),
    settingsOverlayOpen: true, immersiveActive: false, immersiveAppId: null,
    viewMode: 'single',
  })
  assert.equal(v.single, false, 'Settings takes precedence over view-mode')
  assert.equal(v.chromeActive, false)
  assert.equal(v.focusedActiveKey, null)
  assert.equal(v.visibleAppIds.size, 0)
  assert.equal(v.chatPanesVisible, false)
})

test('single-mode yields to immersive: the holder solo governs and single is inert', () => {
  const ws = twoPaneChatAndApp() // app 42 focused, holds immersive
  const v = deriveContentVisibility({
    workspace: ws, projection: project(ws),
    settingsOverlayOpen: false, immersiveActive: true, immersiveAppId: 42,
    viewMode: 'single',
  })
  assert.equal(v.single, false, 'immersive takes precedence over view-mode')
  assert.equal(v.chromeActive, false)
  assert.equal(v.fullBleedKey, tabKey(makeTab('app', '42')))
  assert.deepEqual([...v.visibleAppIds], ['42'])
  assert.equal(v.chatPanesVisible, false)
})

test('single-mode on a single-pane workspace is a no-op (already full-bleed)', () => {
  const ws = paneModel.seedFromFlatTabs([makeTab('app', '42')])
  const panes = deriveContentVisibility({
    workspace: ws, projection: project(ws), settingsOverlayOpen: false,
    immersiveActive: false, immersiveAppId: null, viewMode: 'panes',
  })
  const singleV = deriveContentVisibility({
    workspace: ws, projection: project(ws), settingsOverlayOpen: false,
    immersiveActive: false, immersiveAppId: null, viewMode: 'single',
  })
  // Same render either way — one pane always paints full-bleed.
  assert.equal(singleV.fullBleedKey, panes.fullBleedKey)
  assert.equal(singleV.chromeActive, panes.chromeActive)
  assert.deepEqual([...singleV.visibleAppIds], [...panes.visibleAppIds])
})

test('viewMode defaults to panes when omitted (back-compat with the pre-toggle signature)', () => {
  const ws = twoPaneChatAndApp()
  const v = deriveContentVisibility({
    workspace: ws, projection: project(ws),
    settingsOverlayOpen: false, immersiveActive: false, immersiveAppId: null,
  })
  assert.equal(v.single, false)
  assert.equal(v.chromeActive, true, 'absent viewMode tiles as before')
})

// ── Builder mode strip visibility (item 3: builder invisible with one item) ──
//
// The owner's phone bug: entering builder with a SINGLE leaf changed nothing but
// the logo, because the tiled WorkspaceChrome needs multiPane. The strip is the
// builder SURFACE and Shell now shows the single-pane .shell__tabstrip whenever
// builderModeActive (see workspaceUi source-lock). The DERIVATION's job here is
// only to NOT block it: builder single-leaf must not seize a full-screen takeover
// and must not claim tiled chrome (that is multi-pane only) — it leaves the leaf
// full-bleed beneath the Shell-drawn strip.
test('single-leaf builder: not single, no tiled chrome, the leaf is full-bleed (strip is Shell-level)', () => {
  const ws = paneModel.seedFromFlatTabs([makeTab('chat', '5')])
  const v = deriveContentVisibility({
    workspace: ws, projection: project(ws),
    settingsOverlayOpen: false, immersiveActive: false, immersiveAppId: null,
    viewMode: 'panes', // builder
  })
  assert.equal(v.multiPane, false)
  assert.equal(v.single, false, 'builder is not the single-mode collapse')
  assert.equal(v.chromeActive, false, 'WorkspaceChrome is multi-pane only; the single-pane strip is Shell-level')
  assert.equal(v.fullBleedKey, tabKey(makeTab('chat', '5')), 'the sole leaf paints full-bleed beneath the strip')
})

// The single-SCREEN single-leaf case stays a plain full-bleed with NO strip
// forcing (byte-identical to before): same content flags as builder, the only
// difference (the strip) lives in Shell's builderModeActive gate, not here.
test('single-leaf single-screen matches builder content flags (strip difference is Shell-only)', () => {
  const ws = paneModel.seedFromFlatTabs([makeTab('chat', '5')])
  const builder = deriveContentVisibility({
    workspace: ws, projection: project(ws), settingsOverlayOpen: false,
    immersiveActive: false, immersiveAppId: null, viewMode: 'panes',
  })
  const single = deriveContentVisibility({
    workspace: ws, projection: project(ws), settingsOverlayOpen: false,
    immersiveActive: false, immersiveAppId: null, viewMode: 'single',
  })
  assert.equal(single.chromeActive, builder.chromeActive)
  assert.equal(single.fullBleedKey, builder.fullBleedKey)
  assert.deepEqual([...single.visibleAppIds], [...builder.visibleAppIds])
})

// ── Two-worlds: single mode paints the SLOT, not the focused pane ────────────

function singleView(ws) {
  return deriveContentVisibility({
    workspace: ws, projection: project(ws),
    settingsOverlayOpen: false, immersiveActive: false, immersiveAppId: null,
    viewMode: 'single',
  })
}

test('single mode with an APP slot paints that app full-bleed, even when the focused pane is a chat', () => {
  // Builder tree: chat 5 (focused), app 42 in a sibling pane. The single slot is a
  // DIFFERENT app (99) not in the tree at all.
  let ws = twoPaneChatAndApp()
  const chatPane = paneModel.paneOf(ws, 'chat:5')
  ws = paneModel.focusPane(ws, chatPane.id) // focus the chat pane
  ws = { ...ws, singleScreen: { kind: 'app', id: '99' } }
  const v = singleView(ws)
  assert.equal(v.single, true)
  assert.equal(v.fullBleedKey, 'app:99', 'the slot app, not the focused chat')
  assert.deepEqual([...v.visibleAppIds], ['99'], 'only the slot app paints')
  assert.equal(v.chromeActive, false)
})

test('single mode with a CHAT slot paints no app frame', () => {
  const ws = { ...twoPaneChatAndApp(), singleScreen: { kind: 'chat', id: '7' } }
  const v = singleView(ws)
  assert.equal(v.fullBleedKey, 'chat:7')
  assert.deepEqual([...v.visibleAppIds], [], 'a chat slot paints no app')
  assert.equal(v.chatPanesVisible, true)
})

test('single mode with a NULL slot is the empty/home screen', () => {
  const ws = { ...twoPaneChatAndApp(), singleScreen: null }
  const v = singleView(ws)
  assert.equal(v.fullBleedKey, null, 'nothing painted full-bleed')
  assert.deepEqual([...v.visibleAppIds], [])
})

test('legacy (ABSENT slot) single mode falls back to the focused pane', () => {
  // No singleScreen property → uninitialized → the pre-two-worlds collapse.
  let ws = twoPaneChatAndApp() // app 42 focused
  assert.equal('singleScreen' in ws, false)
  const v = singleView(ws)
  assert.equal(v.fullBleedKey, 'app:42', 'falls back to the focused pane app')
  assert.deepEqual([...v.visibleAppIds], ['42'])
})

// ── Settings takeover is EFFECTIVE-mode gated (finding F3) ───────────────────
//
// The returned `settingsOverlay` is the ONE honest "is the takeover PAINTING now"
// flag: true only when the takeover actually paints. It is FALSE in builder AND
// during a single-mode drag preview / exit beat (viewMode 'panes' while the
// committed world is single). Shell's PAINT gates read this so those transient
// windows paint the tiled world with Settings suspended.

test('settingsOverlay true when the takeover paints in single mode', () => {
  const ws = twoPaneChatAndApp()
  const v = deriveContentVisibility({
    workspace: ws, projection: project(ws),
    settingsOverlayOpen: true, immersiveActive: false, immersiveAppId: null,
    viewMode: 'single',
  })
  assert.equal(v.settingsOverlay, true, 'the takeover paints in the single world')
})

test('settingsOverlay SUSPENDED when the effective mode is panes (drag preview / exit beat)', () => {
  const ws = twoPaneChatAndApp()
  // The nav flag says the overlay is up (committed world single), but the effective
  // mode is 'panes' — a single-mode drag preview or exit beat holds the tiled world.
  const v = deriveContentVisibility({
    workspace: ws, projection: project(ws),
    settingsOverlayOpen: true, immersiveActive: false, immersiveAppId: null,
    viewMode: 'panes',
  })
  assert.equal(v.settingsOverlay, false, 'the takeover is suspended while the tiled world paints')
  // And the derivation paints the tiled world, not the takeover.
  assert.equal(v.single, false)
  assert.equal(v.chromeActive, true, 'panes deal out with Settings suspended, not covered')
})

test('builder mode ignores the slot entirely (tree drives the render)', () => {
  const ws = { ...twoPaneChatAndApp(), singleScreen: { kind: 'app', id: '99' } }
  const v = deriveContentVisibility({
    workspace: ws, projection: project(ws),
    settingsOverlayOpen: false, immersiveActive: false, immersiveAppId: null,
    viewMode: 'panes',
  })
  assert.equal(v.single, false)
  assert.equal(v.chromeActive, true, 'tiled builder chrome')
  assert.equal(v.visibleAppIds.has('99'), false, 'the slot app does not leak into builder')
  assert.equal(v.visibleAppIds.has('42'), true, 'the tree app is what paints')
})

// ── Exit-presentation v2: the latched plan (deriveExitPlan / deriveEnterPlan) ──

test('deriveExitPlan: PROMOTE when the target is a visible pane active key (INV 3 honest destination)', () => {
  // twoPaneChatAndApp: chat 5 left (unfocused), app 42 right (focused). Legacy
  // absent-slot → target seeds from the focused item = app:42, which IS the active
  // key of the right pane → promote it, deal the left sibling out, no underlay.
  const ws = twoPaneChatAndApp()
  const plan = deriveExitPlan({ workspace: ws, projection: project(ws), contentRect: CONTENT })
  assert.equal(plan.target, 'app:42')
  assert.equal(plan.underlayKey, null, 'physical continuity — no world reveal')
  const promote = plan.participants.find(p => p.motion === 'promote')
  assert.equal(promote.key, 'app:42')
  assert.ok(plan.completionNames.includes('shell-mode-promote'))
  // The FLIP grows the promote pane's content rect to the full destination.
  assert.equal(promote.flip.sx > 1, true, 'a half-width pane scales up to full width')
  const dealOut = plan.participants.filter(p => p.motion === 'deal-out')
  assert.equal(dealOut.length, 1)
  assert.equal(dealOut[0].key, 'chat:5')
})

test('deriveExitPlan: WORLD-REVEAL when the slot is tree-absent (underlay + all deal out)', () => {
  const ws = { ...twoPaneChatAndApp(), singleScreen: { kind: 'chat', id: '99' } }
  const plan = deriveExitPlan({ workspace: ws, projection: project(ws), contentRect: CONTENT })
  assert.equal(plan.target, 'chat:99')
  assert.equal(plan.underlayKey, 'chat:99', 'the mounted destination is revealed beneath')
  assert.equal(plan.participants.every(p => p.motion === 'deal-out'), true, 'no false promotion')
  assert.deepEqual(plan.completionNames, ['shell-mode-deal-out'])
  // The FOCUSED pane (app 42) is the LAST card put away (world-reveal focus-last).
  const last = plan.participants.reduce((a, b) => (b.delayMs > a.delayMs ? b : a))
  assert.equal(last.key, 'app:42')
})

test('deriveExitPlan: WORLD-REVEAL when the slot tab is INACTIVE in a pane (never promote it)', () => {
  // app 42 active in the right pane; put chat 5 as the slot but make chat 5 an
  // INACTIVE tab of the left pane (its active is chat 5 though — so instead use a
  // genuinely inactive case): the slot points at an item that is not any pane's
  // active key.
  let ws = paneModel.seedFromFlatTabs([makeTab('chat', '5')])
  ws = paneModel.openTab(ws, makeTab('chat', '7'), { paneId: ws.focusedPaneId, activate: false })
  ws = paneModel.splitPaneWithTab(ws, makeTab('app', '42'), { paneId: ws.focusedPaneId, edge: 'right' })
  ws = { ...ws, singleScreen: { kind: 'chat', id: '7' } } // 7 is an inactive tab
  const plan = deriveExitPlan({ workspace: ws, projection: project(ws), contentRect: CONTENT })
  assert.equal(plan.underlayKey, 'chat:7', 'an inactive-tab slot world-reveals, never promotes')
  assert.equal(plan.participants.some(p => p.motion === 'promote'), false)
})

test('deriveExitPlan: NULL slot reveals home (underlayKey null), empty tree is instant (null plan)', () => {
  const home = { ...twoPaneChatAndApp(), singleScreen: null }
  const homePlan = deriveExitPlan({ workspace: home, projection: project(home), contentRect: CONTENT })
  assert.equal(homePlan.target, null)
  assert.equal(homePlan.underlayKey, null, 'home reveal uses the opaque background, no underlay wrapper')
  assert.ok(homePlan.participants.length >= 1)
  // Empty tree → no participants → null plan → an INSTANT flip (no descriptor).
  const empty = paneModel.seedFromFlatTabs([])
  assert.equal(deriveExitPlan({ workspace: empty, projection: project(empty), contentRect: CONTENT }), null)
})

test('deriveExitPlan: siblings deal out on a 20ms visual-order stagger', () => {
  const ws = { ...twoPaneChatAndApp(), singleScreen: { kind: 'chat', id: '99' } }
  const plan = deriveExitPlan({ workspace: ws, projection: project(ws), contentRect: CONTENT })
  const delays = plan.participants.map(p => p.delayMs).sort((a, b) => a - b)
  assert.deepEqual(delays, [0, MODE_MOTION.staggerMs])
  assert.ok(plan.participants.every(p => p.durationMs === MODE_MOTION.exitItemMs))
  assert.equal(plan.totalMs, MODE_MOTION.staggerMs + MODE_MOTION.exitItemMs)
})

// ── M2: exit plans must describe takeover / immersive destinations ────────────
test('deriveExitPlan: M2 a suspended Settings takeover reveals to the Settings underlay, not the slot', () => {
  // The single world paints Settings OVER the slot on completion, so the exit must
  // reveal to the mounted-hidden Settings surface — never promote/reveal the slot
  // the takeover then covers (the M2 honest-destination break).
  const ws = { ...twoPaneChatAndApp(), singleScreen: { kind: 'app', id: '42' } }
  const plan = deriveExitPlan({
    workspace: ws, projection: project(ws), contentRect: CONTENT,
    settingsDestination: true,
  })
  assert.equal(plan.target, tabModel.SETTINGS_TAB_KEY)
  assert.equal(plan.underlayKey, tabModel.SETTINGS_TAB_KEY, 'reveal to the Settings surface, not the slot')
  assert.equal(plan.participants.some(p => p.motion === 'promote'), false, 'never a promote of the covered slot')
  assert.ok(plan.participants.length >= 1 && plan.participants.every(p => p.motion === 'deal-out'))
})

test('deriveExitPlan: M2 an immersive-holder destination is an honest instant (null plan), not a false FLIP', () => {
  // The single world will solo app 42 over the WHOLE viewport (header gone) — a
  // rect the beat cannot honestly latch — so classify instant rather than FLIP to
  // the content box and jump at completion.
  const ws = { ...twoPaneChatAndApp(), singleScreen: { kind: 'app', id: '42' } }
  const plan = deriveExitPlan({
    workspace: ws, projection: project(ws), contentRect: CONTENT,
    immersiveHolderId: 42,
  })
  assert.equal(plan, null)
})

test('deriveExitPlan: M2 an immersive holder that is NOT the exit slot animates normally', () => {
  // app 42 holds an immersive REQUEST, but the exit lands on chat 5 (the slot), so
  // immersive will not apply — the plan is the ordinary promote, not a false instant.
  const ws = { ...twoPaneChatAndApp(), singleScreen: { kind: 'chat', id: '5' } }
  const plan = deriveExitPlan({
    workspace: ws, projection: project(ws), contentRect: CONTENT,
    immersiveHolderId: 42,
  })
  assert.ok(plan, 'a non-slot immersive request does not suppress the beat')
  assert.equal(plan.target, 'chat:5')
  assert.ok(plan.participants.some(p => p.motion === 'promote' && p.key === 'chat:5'))
})

test('deriveExitPlan: M2 Settings takes precedence over an immersive holder', () => {
  // Both flags set: the takeover paints over everything (Settings wins), so classify
  // as the Settings world reveal, NOT the immersive instant.
  const ws = { ...twoPaneChatAndApp(), singleScreen: { kind: 'app', id: '42' } }
  const plan = deriveExitPlan({
    workspace: ws, projection: project(ws), contentRect: CONTENT,
    settingsDestination: true, immersiveHolderId: 42,
  })
  assert.ok(plan, 'Settings destination still animates (it is representable as an underlay)')
  assert.equal(plan.underlayKey, tabModel.SETTINGS_TAB_KEY)
})

test('deriveExitPlan: M2 a builder Settings tab that IS the destination does not also deal out', () => {
  // A visible Settings pane equals the takeover destination — it is the stationary
  // underlay, so it is excluded from the dealing-out participants (never two roles).
  let ws = paneModel.seedFromFlatTabs([makeTab('chat', '5')])
  ws = paneModel.splitPaneWithTab(ws, tabModel.settingsTab(), { paneId: ws.focusedPaneId, edge: 'right' })
  const plan = deriveExitPlan({
    workspace: ws, projection: project(ws), contentRect: CONTENT,
    settingsDestination: true,
  })
  assert.equal(plan.underlayKey, tabModel.SETTINGS_TAB_KEY)
  assert.equal(plan.participants.some(p => p.key === tabModel.SETTINGS_TAB_KEY), false, 'the underlay surface never deals out')
  assert.ok(plan.participants.some(p => p.key === 'chat:5'), 'the sibling pane still deals out')
})

test('deriveEnterPlan: each visible leaf deals in, 20ms stagger, single-leaf longer', () => {
  const two = twoPaneChatAndApp()
  const twoPlan = deriveEnterPlan({ workspace: two, projection: project(two) })
  assert.deepEqual(twoPlan.completionNames, ['shell-mode-deal-in'])
  assert.equal(twoPlan.participants.length, 2)
  assert.ok(twoPlan.participants.every(p => p.durationMs === MODE_MOTION.enterItemMs))
  assert.deepEqual(twoPlan.participants.map(p => p.delayMs).sort((a, b) => a - b), [0, MODE_MOTION.staggerMs])
  const one = paneModel.seedFromFlatTabs([makeTab('app', '42')])
  const onePlan = deriveEnterPlan({ workspace: one, projection: project(one) })
  assert.equal(onePlan.participants.length, 1)
  assert.equal(onePlan.participants[0].durationMs, MODE_MOTION.enterSingleMs, 'the sole leaf uses the paired gesture duration')
})

test('exitSignature is stable for the same tree and drifts on a topology/geometry change (INV 10)', () => {
  const ws = { ...twoPaneChatAndApp(), singleScreen: { kind: 'chat', id: '99' } }
  const base = exitSignature({ workspace: ws, projection: project(ws), contentRect: CONTENT })
  assert.equal(base, exitSignature({ workspace: ws, projection: project(ws), contentRect: CONTENT }))
  // A content-box resize drifts the signature → the beat cancels.
  const resized = exitSignature({ workspace: ws, projection: project(ws), contentRect: { x: 0, y: 0, w: 800, h: 600 } })
  assert.notEqual(base, resized)
  // A different slot target drifts it too.
  const retargeted = exitSignature({ workspace: { ...ws, singleScreen: { kind: 'chat', id: '5' } }, projection: project(ws), contentRect: CONTENT })
  assert.notEqual(base, retargeted)
})

test('deriveContentVisibility augments visibleAppIds with an app underlay (exit reveal)', () => {
  // During a world-reveal exit the effective mode is still 'panes'; the underlay app
  // is not a visible tree pane, so it must be unioned in or it paints a blank frame.
  const ws = twoPaneChatAndApp()
  const v = deriveContentVisibility({
    workspace: ws, projection: project(ws),
    settingsOverlayOpen: false, immersiveActive: false, immersiveAppId: null,
    viewMode: 'panes', exitUnderlayKey: 'app:99',
  })
  assert.equal(v.exitUnderlayKey, 'app:99')
  assert.equal(v.visibleAppIds.has('99'), true, 'the underlay app is painted beneath the deal')
  assert.equal(v.visibleAppIds.has('42'), true, 'the tree apps still paint')
})
