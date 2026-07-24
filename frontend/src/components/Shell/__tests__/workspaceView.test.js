import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as paneModel from '../paneModel.js'
import * as tabModel from '../tabModel.js'
import {
  deriveContentVisibility, deriveExitPlan, deriveEnterPlan, projectFocusedPane,
  transitionSignature, MODE_MOTION,
  EMPTY_SINGLE_SURFACE_KEY,
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

test('focused pane view is a reversible presentation projection, not a tree rewrite', () => {
  const ws = twoPaneChatAndApp()
  const base = project(ws)
  const focused = projectFocusedPane(base, ws, ws.focusedPaneId, CONTENT)
  assert.deepEqual(focused.visibleLeaves, [ws.focusedPaneId])
  assert.deepEqual(focused.rects[ws.focusedPaneId], CONTENT)
  assert.deepEqual(focused.dividers, [])
  assert.equal(focused.focusedPaneView, true)
  assert.equal(focused.motionRects, base.rects,
    'the durable pane geometry remains available to directional mode motion')
  assert.equal(Object.keys(ws.panes).length, 2, 'the durable pane tree is untouched')

  const v = deriveContentVisibility({
    workspace: ws, projection: focused,
    settingsOverlayOpen: false, immersiveActive: false, immersiveAppId: null,
    viewMode: 'panes', focusedPaneView: true,
  })
  assert.equal(v.chromeActive, true, 'the selected pane keeps its own tab strip')
  assert.equal(v.fullBleedKey, null, 'content stays below that strip instead of covering it')
  assert.deepEqual([...v.visibleAppIds], ['42'], 'hidden sibling panes stop painting')
})

test('focused pane projection falls back safely after its pane disappears', () => {
  const ws = twoPaneChatAndApp()
  const base = project(ws)
  assert.equal(projectFocusedPane(base, ws, 'missing-pane', CONTENT), base)
})

test('multi-pane immersive solos the holder over the whole workspace', () => {
  const ws = twoPaneChatAndApp()
  // The focused (right) pane's app 42 holds an applied immersive request.
  const holderKey = tabKey(makeTab('app', '42'))
  assert.equal(ws.panes[ws.focusedPaneId].activeTabKey, holderKey)
  const v = deriveContentVisibility({
    workspace: ws, projection: project(ws),
    settingsOverlayOpen: false, immersiveActive: true, immersiveAppId: 42,
    viewMode: 'single',
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

// Settings remains structurally inert in builder, while a focused app's explicit
// immersive lease is a temporary overlay over the untouched pane world.
test('builder immersive temporarily solos its holder while Settings stays inert', () => {
  const ws = twoPaneChatAndApp() // 2 panes, focused pane holds app 42
  const v = deriveContentVisibility({
    workspace: ws, projection: project(ws),
    settingsOverlayOpen: true, immersiveActive: true, immersiveAppId: 42,
    viewMode: 'panes', // builder
  })
  // The temporary lease solos the holder without turning builder into single mode.
  assert.equal(v.multiPane, true)
  assert.equal(v.single, false)
  assert.equal(v.settingsOverlay, false, 'Settings never becomes a builder takeover')
  assert.equal(v.chromeActive, false, 'immersive hides pane chrome temporarily')
  assert.equal(v.chatPanesVisible, false)
  assert.equal(v.fullBleedKey, 'app:42')
  assert.deepEqual([...v.visibleAppIds].sort(), ['42'])
})

test('releasing builder immersive restores the exact tiled derivation', () => {
  const ws = twoPaneChatAndApp()
  const projection = project(ws)
  const before = deriveContentVisibility({
    workspace: ws, projection,
    settingsOverlayOpen: false, immersiveActive: false, immersiveAppId: null,
    viewMode: 'panes',
  })
  const during = deriveContentVisibility({
    workspace: ws, projection,
    settingsOverlayOpen: false, immersiveActive: true, immersiveAppId: 42,
    viewMode: 'panes',
  })
  const after = deriveContentVisibility({
    workspace: ws, projection,
    settingsOverlayOpen: false, immersiveActive: false, immersiveAppId: null,
    viewMode: 'panes',
  })

  assert.equal(during.fullBleedKey, 'app:42')
  assert.equal(during.chromeActive, false)
  assert.equal(after.fullBleedKey, before.fullBleedKey)
  assert.equal(after.chromeActive, before.chromeActive)
  assert.equal(after.focusedActiveKey, before.focusedActiveKey)
  assert.deepEqual([...after.visibleAppIds], [...before.visibleAppIds])
  assert.equal(after.chatPanesVisible, before.chatPanesVisible)
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

test('single mode with a NULL slot is the first-class New Chat landing (round 4 item 3)', () => {
  const ws = { ...twoPaneChatAndApp(), singleScreen: null }
  const v = singleView(ws)
  // The empty single slot paints the New Chat surface, never chats[0]...
  assert.equal(v.fullBleedKey, EMPTY_SINGLE_SURFACE_KEY, 'the New Chat landing paints full-bleed')
  // ...but focusedActiveKey stays NULL so nav + AppCanvas never treat it as a tab.
  assert.equal(v.focusedActiveKey, null, 'the landing is not a chat/app tab')
  assert.deepEqual([...v.visibleAppIds], [], 'no app paints for the New Chat landing')
})

test('legacy (ABSENT slot) single mode falls back to the focused pane', () => {
  // No singleScreen property → uninitialized → the pre-two-worlds collapse.
  let ws = twoPaneChatAndApp() // app 42 focused
  assert.equal('singleScreen' in ws, false)
  const v = singleView(ws)
  assert.equal(v.fullBleedKey, 'app:42', 'falls back to the focused pane app')
  assert.deepEqual([...v.visibleAppIds], ['42'])
})

test('round 4 item 3: a null slot renders home:new-chat while its ROUTE stays chat:null (no chats[0])', () => {
  // Even with populated chats in the tree, an empty single slot NEVER selects a chat —
  // the render key is the New Chat landing and the semantic route is still chat:null.
  const ws = { ...twoPaneChatAndApp(), singleScreen: null }
  const v = singleView(ws)
  assert.equal(v.fullBleedKey, EMPTY_SINGLE_SURFACE_KEY, 'render key is the New Chat landing')
  assert.equal(v.fullBleedKey.startsWith('chat:'), false, 'never a chat key (never chats[0])')
  // The persisted slot stays null; singleScreenRoute keeps reporting chat:null.
  assert.deepEqual(paneModel.singleScreenRoute(ws), {
    view: 'chat', chatId: null, appId: null, paneId: ws.focusedPaneId,
  })
})

test('round 4 item 3: an INITIALIZED null slot targets home:new-chat; a legacy Settings-only absent slot stays null', () => {
  // An initialized empty slot → New Chat landing target/underlay (world reveal).
  const nullSlot = { ...twoPaneChatAndApp(), singleScreen: null }
  const nullPlan = deriveExitPlan({ workspace: nullSlot, projection: project(nullSlot), contentRect: CONTENT })
  assert.equal(nullPlan.target, EMPTY_SINGLE_SURFACE_KEY)
  assert.equal(nullPlan.underlayKey, EMPTY_SINGLE_SURFACE_KEY)
  // A LEGACY absent-slot whose sole pane is Settings seeds NO concrete item, so the
  // target stays null (the opaque-background reveal) — unchanged by item 3.
  const legacy = paneModel.seedFromFlatTabs([tabModel.settingsTab()])
  assert.equal('singleScreen' in legacy, false, 'absent slot (legacy)')
  const legacyPlan = deriveExitPlan({ workspace: legacy, projection: project(legacy), contentRect: CONTENT })
  assert.equal(legacyPlan.target, null, 'a legacy Settings-only absent slot is not the New Chat landing')
  assert.equal(legacyPlan.underlayKey, null, 'opaque background reveal, no underlay wrapper')
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

// ── Assemble/scatter v3: latched plans (deriveExitPlan / deriveEnterPlan) ─────

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
  assert.equal(promote.delayMs, 0, 'the assembled world moves as one beat')
  assert.ok(plan.completionNames.includes('shell-mode-promote'))
  // The FLIP grows the promote pane's content rect to the full destination.
  assert.equal(promote.flip.sx > 1, true, 'a half-width pane scales up to full width')
  const dealOut = plan.participants.filter(p => p.motion === 'deal-out')
  assert.equal(dealOut.length, 1)
  assert.equal(dealOut[0].key, 'chat:5')
  assert.ok(dealOut[0].offset.x < 0, 'left sibling scatters past the left edge')
  assert.equal(dealOut[0].offset.y, 0)
})

test('deriveExitPlan: WORLD-REVEAL when the slot is tree-absent (underlay + all deal out)', () => {
  const ws = { ...twoPaneChatAndApp(), singleScreen: { kind: 'chat', id: '99' } }
  const plan = deriveExitPlan({ workspace: ws, projection: project(ws), contentRect: CONTENT })
  assert.equal(plan.target, 'chat:99')
  assert.equal(plan.underlayKey, 'chat:99', 'the mounted destination is revealed beneath')
  assert.equal(plan.participants.every(p => p.motion === 'deal-out'), true, 'no false promotion')
  assert.deepEqual(plan.completionNames, ['shell-mode-deal-out'])
  assert.ok(plan.participants.every(p => p.delayMs === 0), 'all panes leave together')
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

test('deriveExitPlan: NULL slot reveals the New Chat landing (round 4 item 3), empty tree is instant', () => {
  const home = { ...twoPaneChatAndApp(), singleScreen: null }
  const homePlan = deriveExitPlan({ workspace: home, projection: project(home), contentRect: CONTENT })
  // A null slot is a definite New Chat destination now — a WORLD REVEAL to the
  // home:new-chat underlay, never the freshest chat and never the opaque-only home.
  assert.equal(homePlan.target, EMPTY_SINGLE_SURFACE_KEY)
  assert.equal(homePlan.underlayKey, EMPTY_SINGLE_SURFACE_KEY, 'the New Chat landing is revealed beneath the deal')
  assert.ok(homePlan.participants.every(p => p.motion === 'deal-out'), 'every painted leaf deals out')
  assert.ok(homePlan.participants.length >= 1)
  // Empty tree → no participants → null plan → an INSTANT flip (no descriptor).
  const empty = paneModel.seedFromFlatTabs([])
  assert.equal(deriveExitPlan({ workspace: empty, projection: project(empty), contentRect: CONTENT }), null)
})

test('deriveExitPlan: siblings deal out together in one short beat', () => {
  const ws = { ...twoPaneChatAndApp(), singleScreen: { kind: 'chat', id: '99' } }
  const plan = deriveExitPlan({ workspace: ws, projection: project(ws), contentRect: CONTENT })
  const delays = plan.participants.map(p => p.delayMs).sort((a, b) => a - b)
  assert.deepEqual(delays, [0, 0])
  assert.equal(MODE_MOTION.staggerMs, undefined)
  assert.ok(plan.participants.every(p => p.durationMs === MODE_MOTION.exitItemMs))
  assert.equal(plan.totalMs, MODE_MOTION.exitItemMs)
})

test('deriveExitPlan: a world reveal has no delayed destination phase', () => {
  const ws = { ...twoPaneChatAndApp(), singleScreen: { kind: 'chat', id: '99' } }
  const plan = deriveExitPlan({ workspace: ws, projection: project(ws), contentRect: CONTENT })
  assert.deepEqual(plan.completionNames, ['shell-mode-deal-out'])
  assert.equal('destinationMotion' in plan, false)
  assert.equal(plan.totalMs, MODE_MOTION.exitItemMs)
})

test('deriveExitPlan: a promote keeps its seamless continuity', () => {
  // twoPaneChatAndApp legacy absent-slot → seeds app:42 (focused) → PROMOTE.
  const ws = twoPaneChatAndApp()
  const plan = deriveExitPlan({ workspace: ws, projection: project(ws), contentRect: CONTENT })
  assert.ok(plan.participants.some(p => p.motion === 'promote'))
  assert.equal('destinationMotion' in plan, false)
  assert.equal(plan.completionNames.includes('shell-mode-destination-arrive'), false)
})

test('deriveExitPlan: four panes cost the same 180ms beat as one pane', () => {
  // Build MAX_PANES visible leaves (a balanced 2×2 within MAX_DEPTH) and a tree-absent
  // slot so all four deal out over a revealed underlay. Tied to MAX_PANES so a future
  // pane-count change can't silently blow the beat budget.
  let ws = paneModel.seedFromFlatTabs([makeTab('chat', '1')])
  ws = paneModel.splitPaneWithTab(ws, makeTab('chat', '2'), { paneId: ws.focusedPaneId, edge: 'right' })
  const leftId = paneModel.paneOf(ws, 'chat:1').id
  const rightId = paneModel.paneOf(ws, 'chat:2').id
  ws = paneModel.splitPaneWithTab(ws, makeTab('chat', '3'), { paneId: leftId, edge: 'bottom' })
  ws = paneModel.splitPaneWithTab(ws, makeTab('chat', '4'), { paneId: rightId, edge: 'bottom' })
  const proj = project(ws)
  assert.equal(proj.visibleLeaves.length, paneModel.MAX_PANES, 'four visible leaves')
  ws = { ...ws, singleScreen: { kind: 'chat', id: 'ghost' } } // tree-absent → world reveal
  const plan = deriveExitPlan({ workspace: ws, projection: proj, contentRect: CONTENT })
  assert.ok(plan.participants.every(p => p.motion === 'deal-out'), 'all four deal out')
  assert.equal(plan.totalMs, MODE_MOTION.exitItemMs)
  assert.equal(plan.totalMs, 180)
})

test('four-pane assemble/scatter preserves all four outer-edge vectors', () => {
  let ws = paneModel.seedFromFlatTabs([makeTab('chat', '1')])
  ws = paneModel.splitPaneWithTab(ws, makeTab('chat', '2'), { paneId: ws.focusedPaneId, edge: 'right' })
  const leftId = paneModel.paneOf(ws, 'chat:1').id
  const rightId = paneModel.paneOf(ws, 'chat:2').id
  ws = paneModel.splitPaneWithTab(ws, makeTab('chat', '3'), { paneId: leftId, edge: 'bottom' })
  ws = paneModel.splitPaneWithTab(ws, makeTab('chat', '4'), { paneId: rightId, edge: 'bottom' })
  ws = { ...ws, singleScreen: { kind: 'chat', id: 'ghost' } }
  const projection = project(ws)

  for (const [plan, duration] of [
    [deriveExitPlan({ workspace: ws, projection, contentRect: CONTENT }), MODE_MOTION.exitItemMs],
    [deriveEnterPlan({ workspace: ws, projection, contentRect: CONTENT }), MODE_MOTION.enterItemMs],
  ]) {
    const offsets = new Map(plan.participants.map(p => [p.key, p.offset]))
    assert.ok(offsets.get('chat:1').x < 0 && offsets.get('chat:1').y < 0, 'top-left owns top+left')
    assert.ok(offsets.get('chat:2').x > 0 && offsets.get('chat:2').y < 0, 'top-right owns top+right')
    assert.ok(offsets.get('chat:3').x < 0 && offsets.get('chat:3').y > 0, 'bottom-left owns bottom+left')
    assert.ok(offsets.get('chat:4').x > 0 && offsets.get('chat:4').y > 0, 'bottom-right owns bottom+right')
    assert.ok(plan.participants.every(p => p.delayMs === 0),
      'world-reveal edge panes move together; no pane-count stagger')
    assert.equal(plan.totalMs, duration)
  }
})

test('uneven entry normalizes pane velocity and lands every edge on one frame', () => {
  // A deliberately asymmetric tree reproduces the production complaint: without
  // distance-aware timing, the shortest and longest edge vectors differ by almost
  // 2× while sharing one duration, so one pane visibly rushes into the seam.
  let ws = paneModel.seedFromFlatTabs([makeTab('chat', '1')])
  ws = paneModel.splitPaneWithTab(ws, makeTab('chat', '2'), {
    paneId: ws.focusedPaneId, edge: 'right',
  })
  const rightId = paneModel.paneOf(ws, 'chat:2').id
  ws = paneModel.splitPaneWithTab(ws, makeTab('chat', '3'), {
    paneId: rightId, edge: 'bottom',
  })
  ws = paneModel.setRatio(ws, ws.layout.id, 0.7)
  ws = paneModel.setRatio(ws, ws.layout.b.id, 0.25)
  ws = { ...ws, singleScreen: { kind: 'chat', id: 'ghost' } }

  const plan = deriveEnterPlan({ workspace: ws, projection: project(ws), contentRect: CONTENT })
  assert.equal(plan.totalMs, MODE_MOTION.enterItemMs)
  assert.ok(plan.participants.some(p => p.delayMs > 0),
    'a shorter trip waits offscreen instead of racing a longer trip')
  assert.ok(plan.participants.every(
    p => p.delayMs + p.durationMs === MODE_MOTION.enterItemMs,
  ), 'every pane lands on the same terminal frame')

  const speeds = plan.participants.map((p) => (
    Math.hypot(p.offset.x, p.offset.y) / p.durationMs
  ))
  assert.ok(Math.max(...speeds) / Math.min(...speeds) < 1.15,
    'average pane velocities stay perceptually aligned despite asymmetric geometry')

  // The valid 10/90 extreme used to fall through the 120ms duration floor:
  // one pane then moved more than 2× slower than its siblings. Short trips now
  // begin farther beyond their same edge, preserving both the readable floor
  // and the velocity contract.
  ws = paneModel.setRatio(ws, ws.layout.id, 0.1)
  ws = paneModel.setRatio(ws, ws.layout.b.id, 0.1)
  const extreme = deriveEnterPlan({ workspace: ws, projection: project(ws), contentRect: CONTENT })
  const extremeSpeeds = extreme.participants.map((p) => (
    Math.hypot(p.offset.x, p.offset.y) / p.durationMs
  ))
  assert.ok(extreme.participants.every(p => p.durationMs >= MODE_MOTION.enterMinItemMs))
  assert.ok(Math.max(...extremeSpeeds) / Math.min(...extremeSpeeds) < 1.15,
    'the minimum readable duration must not reintroduce velocity spread')
})

test('N1: MODE_MOTION drops the unused chromeMs constant', () => {
  assert.equal(MODE_MOTION.chromeMs, undefined)
  // The live timings the plan builders use are still present.
  assert.equal(typeof MODE_MOTION.promoteMs, 'number')
  assert.equal(typeof MODE_MOTION.exitItemMs, 'number')
})

// ── M4: the single-leaf promote FLIP must not overshoot ───────────────────────
test('deriveExitPlan: M4 the single-leaf promote FLIPs identity (no STRIP_H overshoot)', () => {
  // One visible leaf → its strip is a flex SIBLING outside .shell__content, so the
  // sole wrapper already fills the content box. The promote FLIP must be identity,
  // not inset by STRIP_H (which overshot y:-STRIP_H, sy>1 then snapped back).
  const ws = { ...paneModel.seedFromFlatTabs([makeTab('app', '42')]), singleScreen: { kind: 'app', id: '42' } }
  const plan = deriveExitPlan({ workspace: ws, projection: project(ws), contentRect: CONTENT })
  const promote = plan.participants.find(p => p.motion === 'promote')
  assert.ok(promote, 'the sole leaf promotes')
  assert.equal(Math.abs(promote.flip.x), 0) // -from.x can be -0; compare magnitude
  assert.equal(Math.abs(promote.flip.y), 0, 'no STRIP_H vertical inset')
  assert.equal(promote.flip.sx, 1)
  assert.equal(promote.flip.sy, 1, 'no vertical overshoot — the wrapper is already full-bleed')
})

test('deriveExitPlan: M4 a multi-pane promote KEEPS the STRIP_H inset (strip is inside the pane rect)', () => {
  // >=2 leaves → WorkspaceChrome strips sit INSIDE each pane rect, so the wrapper is
  // inset by STRIP_H and the FLIP legitimately scales up. The single-leaf fix must
  // not touch this multi-pane case.
  const ws = twoPaneChatAndApp() // legacy absent-slot → seeds app:42 (focused right pane)
  const plan = deriveExitPlan({ workspace: ws, projection: project(ws), contentRect: CONTENT })
  const promote = plan.participants.find(p => p.motion === 'promote')
  assert.ok(promote && promote.key === 'app:42')
  assert.ok(promote.flip.sy > 1, 'STRIP_H inset stays for a real multi-pane strip')
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

test('deriveEnterPlan: the shared Standard surface stays still while siblings assemble', () => {
  const two = twoPaneChatAndApp() // the app:42 pane is focused
  const twoPlan = deriveEnterPlan({ workspace: two, projection: project(two), contentRect: CONTENT })
  assert.deepEqual(twoPlan.completionNames, ['shell-mode-deal-in'])
  assert.equal(twoPlan.underlayKey, 'app:42')
  assert.equal(twoPlan.participants.length, 1)
  assert.ok(twoPlan.participants.every(p => p.durationMs === MODE_MOTION.enterItemMs))
  const gather = twoPlan.participants.find(p => p.motion === 'deal-in')
  assert.equal(gather.key, 'chat:5')
  assert.ok(gather.offset.x < 0, 'the left pane assembles from the left edge')
  assert.ok(twoPlan.participants.every(p => p.delayMs === 0))
  assert.equal(twoPlan.totalMs, MODE_MOTION.enterItemMs)

  // There is no visible assembly when the Standard surface is the only leaf.
  // Returning null makes the controller commit that world flip instantly.
  const one = paneModel.seedFromFlatTabs([makeTab('app', '42')])
  const onePlan = deriveEnterPlan({ workspace: one, projection: project(one), contentRect: CONTENT })
  assert.equal(onePlan, null)
})

test('focused-pane mode keeps its original edge and its in-pane strip geometry', () => {
  const ws = twoPaneChatAndApp() // chat:5 left, focused app:42 right
  const base = project(ws)
  const focused = projectFocusedPane(base, ws, ws.focusedPaneId, CONTENT)

  // When Standard targets the focused app, the shared wrapper is not an identity
  // FLIP: focused-pane chrome still occupies STRIP_H inside the full-size pane.
  const shared = deriveExitPlan({ workspace: ws, projection: focused, contentRect: CONTENT })
  const promote = shared.participants.find(p => p.motion === 'promote')
  assert.ok(promote)
  assert.equal(promote.flip.y, -paneModel.STRIP_H)
  assert.ok(promote.flip.sy > 1)
  assert.equal(promote.delayMs, 0)

  // When Standard targets the left chat instead, the focused right pane scatters
  // right. Its full-size painted rect must clear the viewport completely; it must
  // never fall back to the centred-pane "top" direction.
  const toLeft = { ...ws, singleScreen: { kind: 'chat', id: '5' } }
  const exit = deriveExitPlan({ workspace: toLeft, projection: focused, contentRect: CONTENT })
  const departing = exit.participants.find(p => p.key === 'app:42')
  assert.equal(departing.motion, 'deal-out')
  assert.ok(departing.offset.x > CONTENT.w)
  assert.equal(departing.offset.y, 0)

  const enter = deriveEnterPlan({ workspace: toLeft, projection: focused, contentRect: CONTENT })
  const arriving = enter.participants.find(p => p.key === 'app:42')
  assert.equal(arriving.motion, 'deal-in')
  assert.equal(arriving.offset.x, departing.offset.x, 'entry is the directional inverse')
  assert.equal(arriving.offset.y, 0)
})

test('deriveEnterPlan: a tree-absent single surface stays beneath the assembling panes', () => {
  const ws = { ...twoPaneChatAndApp(), singleScreen: { kind: 'chat', id: '99' } }
  const input = { workspace: ws, projection: project(ws), contentRect: CONTENT }
  const plan = deriveEnterPlan(input)
  assert.equal(plan.target, 'chat:99')
  assert.equal(plan.underlayKey, 'chat:99')
  assert.deepEqual(plan.completionNames, ['shell-mode-deal-in'])
  assert.ok(plan.participants.every(p => p.motion === 'deal-in'))
  const left = plan.participants.find(p => p.key === 'chat:5')
  const right = plan.participants.find(p => p.key === 'app:42')
  assert.ok(left.offset.x < 0)
  assert.ok(right.offset.x > 0)
  assert.equal(plan.snapshotSignature, transitionSignature(input), 'entry latches its input snapshot')
  assert.notEqual(
    plan.snapshotSignature,
    transitionSignature({ ...input, contentRect: { ...CONTENT, w: CONTENT.w - 120 } }),
    'a mid-entry content resize invalidates the latched edge offsets',
  )
})

test('edge motions accept Shell\'s origin-free live content rect', () => {
  const ws = { ...twoPaneChatAndApp(), singleScreen: { kind: 'chat', id: '99' } }
  const contentRect = { w: CONTENT.w, h: CONTENT.h }
  const projection = paneModel.projectLayout(ws, paneModel.modeForRect(contentRect), contentRect)
  for (const plan of [
    deriveExitPlan({ workspace: ws, projection, contentRect }),
    deriveEnterPlan({ workspace: ws, projection, contentRect }),
  ]) {
    assert.ok(plan.participants.length > 0)
    for (const participant of plan.participants) {
      assert.ok(Number.isFinite(participant.offset.x), 'horizontal offset stays finite')
      assert.ok(Number.isFinite(participant.offset.y), 'vertical offset stays finite')
    }
  }
})

test('transitionSignature is stable and drifts on topology/content-bound changes (INV 10)', () => {
  const ws = { ...twoPaneChatAndApp(), singleScreen: { kind: 'chat', id: '99' } }
  const base = transitionSignature({ workspace: ws, projection: project(ws), contentRect: CONTENT })
  assert.equal(base, transitionSignature({ workspace: ws, projection: project(ws), contentRect: CONTENT }))
  // A content-box resize drifts the signature → the beat cancels.
  const resized = transitionSignature({ workspace: ws, projection: project(ws), contentRect: { x: 0, y: 0, w: 800, h: 600 } })
  assert.notEqual(base, resized)
  const movedBounds = transitionSignature({
    workspace: ws, projection: project(ws), contentRect: { ...CONTENT, x: 12 },
  })
  assert.notEqual(base, movedBounds, 'edge offsets include the content origin when supplied')
  // A divider ratio changes edge offsets without changing the content bounds, so
  // per-pane rects are part of the invalidation signature too.
  const resizedPane = paneModel.setRatio(ws, ws.layout.id, 0.62)
  assert.notEqual(base, transitionSignature({ workspace: resizedPane, projection: project(resizedPane), contentRect: CONTENT }))
  // Focused presentation geometry stays full-size across a divider change, but its
  // durable source edge can move. That source rect must invalidate the live beat too.
  const baseProjection = project(ws)
  const focused = projectFocusedPane(baseProjection, ws, ws.focusedPaneId, CONTENT)
  const focusedSig = transitionSignature({ workspace: ws, projection: focused, contentRect: CONTENT })
  const sourceRect = focused.motionRects[ws.focusedPaneId]
  const movedSource = {
    ...focused,
    motionRects: {
      ...focused.motionRects,
      [ws.focusedPaneId]: { ...sourceRect, x: sourceRect.x - 12 },
    },
  }
  assert.notEqual(focusedSig,
    transitionSignature({ workspace: ws, projection: movedSource, contentRect: CONTENT }))
  // A different slot target drifts it too.
  const retargeted = transitionSignature({ workspace: { ...ws, singleScreen: { kind: 'chat', id: '5' } }, projection: project(ws), contentRect: CONTENT })
  assert.notEqual(base, retargeted)
})

test('transitionSignature folds the destination so a mid-beat destination change cancels (H2)', () => {
  // The audit case: a live exit plan built toward the chat slot must cancel the moment
  // a Settings takeover suspends over the slot mid-beat — the two signatures differ.
  const ws = { ...twoPaneChatAndApp(), singleScreen: { kind: 'chat', id: '5' } }
  const proj = project(ws)
  const toChat = transitionSignature({ workspace: ws, projection: proj, contentRect: CONTENT })
  const toSettings = transitionSignature({ workspace: ws, projection: proj, contentRect: CONTENT, settingsDestination: true })
  assert.notEqual(toChat, toSettings, 'chat-target vs settings:settings destinations must differ')
  // And the PLAN's stored snapshot equals a live recompute at the SAME destination, so
  // the watcher never false-cancels while the destination holds (structural coupling:
  // deriveExitPlan feeds its own input object to transitionSignature).
  const settingsPlan = deriveExitPlan({ workspace: ws, projection: proj, contentRect: CONTENT, settingsDestination: true })
  assert.equal(settingsPlan.snapshotSignature, toSettings)
  const chatPlan = deriveExitPlan({ workspace: ws, projection: proj, contentRect: CONTENT })
  assert.equal(chatPlan.snapshotSignature, toChat)
  // An immersive holder that solos the exit slot is an INSTANT destination — its
  // signature differs from the ordinary reveal, so a mid-beat immersive request cancels.
  const app42 = { ...twoPaneChatAndApp(), singleScreen: { kind: 'app', id: '42' } }
  const projApp = project(app42)
  const reveal = transitionSignature({ workspace: app42, projection: projApp, contentRect: CONTENT })
  const immersive = transitionSignature({ workspace: app42, projection: projApp, contentRect: CONTENT, immersiveHolderId: 42 })
  assert.notEqual(reveal, immersive, 'an immersive-instant destination must drift the signature')
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
