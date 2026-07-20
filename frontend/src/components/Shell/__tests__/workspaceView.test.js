import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as paneModel from '../paneModel.js'
import * as tabModel from '../tabModel.js'
import { deriveContentVisibility } from '../workspaceView.js'

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
