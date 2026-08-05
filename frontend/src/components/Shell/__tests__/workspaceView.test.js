import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as paneModel from '../paneModel.js'
import * as tabModel from '../tabModel.js'
import {
  deriveContentVisibility, deriveModeSnapshotPlan, projectFocusedPane,
  MODE_MOTION,
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

function assertPaneSnapshotsClearViewport(plan, projection, bounds) {
  assert.ok(Object.keys(plan.offsets).length > 0, 'the plan has moving panes')
  const boundsX = Number.isFinite(bounds.x) ? bounds.x : 0
  const boundsY = Number.isFinite(bounds.y) ? bounds.y : 0
  for (const [paneId, { x, y }] of Object.entries(plan.offsets)) {
    const rect = projection.rects[paneId]
    if (x < 0) assert.ok(rect.x + rect.w + x < boundsX, 'left-moving pane clears the viewport')
    if (x > 0) assert.ok(rect.x + x > boundsX + bounds.w, 'right-moving pane clears the viewport')
    if (y < 0) assert.ok(rect.y + rect.h + y < boundsY, 'top-moving pane clears the viewport')
    if (y > 0) assert.ok(rect.y + y > boundsY + bounds.h, 'bottom-moving pane clears the viewport')
  }
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
// Single-mode paints its single-screen SLOT full-bleed over a preserved multi-pane
// tree (never the focused pane — two-worlds design). It reuses the immersive/
// single-pane full-bleed path but is driven by viewMode, not an overlay, and it is
// orthogonal to both overlays.

test('single-mode, multi-pane, app slot: chrome off, holder full-bleed, only slot app visible', () => {
  // The slot app (42) is also present in the right pane, but single mode paints it
  // because it is the SLOT, not because that pane is focused.
  const ws = { ...twoPaneChatAndApp(), singleScreen: { kind: 'app', id: '42' } }
  const v = deriveContentVisibility({
    workspace: ws, projection: project(ws),
    settingsOverlayOpen: false, immersiveActive: false, immersiveAppId: null,
    viewMode: 'single',
  })
  assert.equal(v.single, true)
  assert.equal(v.multiPane, true, 'the tree is preserved — still two leaves')
  assert.equal(v.chromeActive, false, 'no strips/dividers over a single surface')
  assert.equal(v.fullBleedKey, 'app:42', 'the slot app paints full-bleed')
  // Only the slot app stays frame-visible; the sibling chat pane hides.
  assert.deepEqual([...v.visibleAppIds], ['42'])
})

test('single-mode with a CHAT slot paints the chat and hides the sibling app frame', () => {
  // The slot is chat 5; app 42 lives in a sibling pane but never paints in single mode.
  const ws = { ...twoPaneChatAndApp(), singleScreen: { kind: 'chat', id: '5' } }
  const v = deriveContentVisibility({
    workspace: ws, projection: project(ws),
    settingsOverlayOpen: false, immersiveActive: false, immersiveAppId: null,
    viewMode: 'single',
  })
  assert.equal(v.single, true)
  assert.equal(v.fullBleedKey, 'chat:5', 'the slot chat is the full-bleed surface')
  // The sibling app 42 is NOT the slot, so its frame goes visibility:false.
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
  const ws = { ...paneModel.seedFromFlatTabs([makeTab('app', '42')]), singleScreen: { kind: 'app', id: '42' } }
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
  const ws = { ...paneModel.seedFromFlatTabs([makeTab('chat', '5')]), singleScreen: { kind: 'chat', id: '5' } }
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

test('legacy (ABSENT slot) single mode paints the New Chat landing — never the focused pane', () => {
  // No singleScreen property → uninitialized. Two-worlds: Standard never borrows the
  // focused Builder pane; an uninitialized slot is the empty home until an item is
  // opened in single mode.
  let ws = twoPaneChatAndApp() // app 42 focused in Builder
  // Strip the seeded slot to model an uninitialized legacy blob (absent marker); the
  // fresh seed now writes a concrete slot, but a genuinely absent one stays home.
  delete ws.singleScreen
  assert.equal('singleScreen' in ws, false)
  const v = singleView(ws)
  assert.equal(v.fullBleedKey, EMPTY_SINGLE_SURFACE_KEY, 'the New Chat landing, not the focused app')
  assert.equal(v.focusedActiveKey, null, 'the landing is not a chat/app tab')
  assert.deepEqual([...v.visibleAppIds], [], 'never borrows the focused pane app')
})

test('a fresh empty legacy seed paints the New Chat landing instead of a blank main', () => {
  const ws = paneModel.seedFromFlatTabs([])
  assert.equal('singleScreen' in ws, false, 'the fresh seed still carries no migration marker')
  const v = singleView(ws)
  assert.equal(v.fullBleedKey, EMPTY_SINGLE_SURFACE_KEY)
  assert.equal(v.focusedActiveKey, null)
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

test('round 4 item 3: an INITIALIZED null slot is the New Chat landing without transition-only routing', () => {
  const nullSlot = { ...twoPaneChatAndApp(), singleScreen: null }
  assert.equal(singleView(nullSlot).fullBleedKey, EMPTY_SINGLE_SURFACE_KEY)
  // A legacy absent-slot is the empty home too — it never borrows the focused Builder
  // surface (here a Settings tab).
  const legacy = paneModel.seedFromFlatTabs([tabModel.settingsTab()])
  assert.equal('singleScreen' in legacy, false, 'absent slot (legacy)')
  assert.equal(singleView(legacy).fullBleedKey, EMPTY_SINGLE_SURFACE_KEY)
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

// ── Native captured-scene motion ───────────────────────────────────────────

test('mode snapshot plan gives every visible pane one linear off-screen vector', () => {
  const ws = twoPaneChatAndApp()
  const projection = project(ws)
  const plan = deriveModeSnapshotPlan({ workspace: ws, projection, contentRect: CONTENT })
  assert.equal(plan.totalMs, MODE_MOTION.slideMs)
  assert.deepEqual(Object.keys(plan.offsets).sort(), [...projection.visibleLeaves].sort())
  assertPaneSnapshotsClearViewport(plan, projection, CONTENT)
})

test('left and right panes preserve their own outward directions', () => {
  const ws = twoPaneChatAndApp()
  const projection = project(ws)
  const plan = deriveModeSnapshotPlan({ workspace: ws, projection, contentRect: CONTENT })
  const ordered = [...projection.visibleLeaves].sort(
    (a, b) => projection.rects[a].x - projection.rects[b].x,
  )
  assert.ok(plan.offsets[ordered[0]].x < 0)
  assert.ok(plan.offsets[ordered[1]].x > 0)
})

test('focused-pane presentation keeps the pane original edge for scene direction', () => {
  const ws = twoPaneChatAndApp()
  const base = project(ws)
  const leftPane = [...base.visibleLeaves].sort(
    (a, b) => base.rects[a].x - base.rects[b].x,
  )[0]
  const focused = projectFocusedPane(base, ws, leftPane, CONTENT)
  const plan = deriveModeSnapshotPlan({ workspace: ws, projection: focused, contentRect: CONTENT })
  assert.ok(plan.offsets[leftPane].x < 0, 'a full-size focused pane still exits toward its durable left edge')
})

test('an empty projection has no decorative mode transaction', () => {
  const ws = paneModel.seedFromFlatTabs([])
  const projection = project(ws)
  assert.equal(deriveModeSnapshotPlan({ workspace: ws, projection, contentRect: CONTENT }), null)
})
