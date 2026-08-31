import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const src = resolve(here, '../..')
const navigation = readFileSync(resolve(src, 'hooks/useNavigation.js'), 'utf8')
const canvas = readFileSync(resolve(src, 'components/AppCanvas/AppCanvas.jsx'), 'utf8')
const shell = readFileSync(resolve(src, 'components/Shell/Shell.jsx'), 'utf8')
const workspaceSession = readFileSync(
  resolve(src, 'components/Shell/useWorkspaceSession.js'),
  'utf8',
)
const frame = readFileSync(resolve(src, '../public/app-frame.html'), 'utf8')

test('one open drawer owns at most one physical sentinel', () => {
  assert.match(
    navigation,
    /function openDrawer\(\) \{\s*\/\/[\s\S]*?if \(drawerOpenRef\.current\) return[\s\S]*?pushShellEntry\('drawer'/,
  )
})

// closeDrawer hides the panel immediately but keeps drawerOpenRef true until Back
// consumes the sentinel. Safari can classify that popstate as same/unknown when
// shell indices are missing. Resolve the pending close at the event boundary;
// never infer from drawerPushedRef ALONE that the sentinel is still current
// while a traversal is in flight, because the next close could then over-pop a
// real route. A wedged WebKit Navigation API can also LOSE the traversal
// outright (2026-07-29): after a bounded grace window openDrawer reconciles
// from the CLASSIC store — which, unlike the boolean, can prove whether the
// history cursor still sits on the sentinel — so a lost close can never refuse
// every later open until relaunch.
test('a pending drawer close consumes its tagged source before direction fallback', () => {
  const open = navigation.slice(
    navigation.indexOf('function openDrawer()'),
    navigation.indexOf('function closeDrawer('),
  )
  const guard = open.indexOf('if (drawerOpenRef.current) return')
  const pending = open.indexOf('if (drawerClosePendingRef.current) {')
  assert.ok(pending >= 0 && guard > pending,
    'an unresolved traversal is resolved or blocks another open before the ordinary open guard')
  const pendingBlock = open.slice(pending, guard)
  // Within the grace window serialization still wins, but the activation is
  // retained and replayed instead of being discarded.
  assert.match(
    pendingBlock,
    /drawerOpenAfterCloseRef\.current = true[\s\S]*DRAWER_CLOSE_TRAVERSAL_GRACE_MS - elapsed[\s\S]*return/,
    'a fresh pending close queues rather than discards the first open',
  )
  // Re-adopting the sentinel after a LOST traversal requires classic-store
  // proof that the cursor never left it — never the boolean alone.
  assert.match(
    pendingBlock,
    /drawerPushedRef\.current\s*&&\s*isMobiusNavState\(history\.state\) && history\.state\.kind === 'drawer'/,
    're-adoption is gated on the classic store still showing the drawer sentinel',
  )
  assert.doesNotMatch(open, /if \(drawerPushedRef\.current\) \{\s*drawerOpenRef\.current = true/,
    'a boolean cannot prove that an async history cursor still sits on the sentinel')
  // Both traversal paths consume a pending tagged drawer source. The popstate
  // path reads the classic store directly; the Navigation API path recognizes
  // the consumption through its own refs BEFORE the phantom guard, because a
  // wedged mirror store returns untagged reads for our own entries.
  assert.match(navigation,
    /const pendingDrawerClose = drawerClosePendingRef\.current && source\?\.kind === 'drawer'/)
  assert.match(navigation,
    /if \(pendingDrawerClose \|\| unreadableSentinelConsumption\) \{/)
  assert.match(navigation,
    /if \(drawerClosePendingRef\.current && source\?\.kind === 'drawer'\) \{\s*\n\s*handleBack\(destination, source\)/)
  // Whoever else resolves the drawer's history state clears the pending flag too,
  // so a later open cannot remain latched behind a hidden panel.
  assert.match(navigation, /drawerClosePendingRef\.current = false\s*\n\s*drawerOpenRef\.current = false\s*\n\s*setDrawerVisible\(false\)/)
  assert.match(navigation, /function handleForward\([^)]*\) \{\s*\/\/[\s\S]*?if \(drawerClosePendingRef\.current\) \{\s*clearDrawerOpenAfterClose\(\)\s*drawerClosePendingRef\.current = false\s*drawerOpenRef\.current = false/)
})

test('an explicit drawer close starts visually before consuming its history sentinel', () => {
  const close = navigation.slice(
    navigation.indexOf('function closeDrawer('),
    navigation.indexOf('/**\n   * Mini-app nav-bridge'),
  )
  const visualClose = close.indexOf(
    'if (!preserveModalUntilTraversal) setDrawerVisible(false)',
  )
  const traversal = close.indexOf('history.back()', visualClose)
  assert.ok(visualClose >= 0)
  assert.ok(traversal > visualClose,
    'tap/swipe dismissal must acknowledge before the asynchronous Back traversal')
  assert.match(close, /drawerClosePendingRef\.current = true[\s\S]*if \(!preserveModalUntilTraversal\) setDrawerVisible\(false\)[\s\S]*history\.back\(\)/)
})

test('a breakpoint close preserves the mobile modal boundary until history settles', () => {
  assert.match(
    shell,
    /desktopSidebarMode && drawerOpen[\s\S]*closeDrawerRef\.current\(\{ preserveModalUntilTraversal: true \}\)/,
  )
})

test('drawer visual visibility cannot overwrite logical history ownership during render', () => {
  assert.match(navigation, /const \[drawerVisible, setDrawerVisible\] = useState\(false\)/)
  assert.match(navigation, /const drawerOpenRef = useRef\(false\)/)
  assert.doesNotMatch(navigation, /drawerOpenRef\.current = drawerVisible/)
  assert.match(navigation, /drawerOpen: drawerVisible/)
})

test('ordinary Back restores a hidden sentinel owner before messaging it', () => {
  const ordinaryBack = navigation.slice(
    navigation.indexOf('// (4) Ordinary app sentinel'),
    navigation.indexOf('// (5) Plain route'),
  )
  assert.ok(ordinaryBack.length > 0)
  assert.match(ordinaryBack, /if \(!isVisibleApp\(ws, sourceOwner\.appId\)\)/)
  // The restore funnels through applyModeDestination (finding F5) — world-aware, so
  // single mode sets the painted SLOT — NOT a raw OPEN_TAB into the hidden tree,
  // which single mode never paints (Back would then message an invisible iframe).
  assert.match(ordinaryBack, /applyModeDestination\(\{\s*\n\s*view: 'canvas', appId: Number\(sourceOwner\.appId\)/)
  assert.doesNotMatch(ordinaryBack, /type: 'OPEN_TAB'/)
  assert.match(ordinaryBack, /moebius:nav-back/)
})

test('app-entry consumption cannot decrement the same owner twice', () => {
  const consume = navigation.slice(
    navigation.indexOf('const consumeAppEntry'),
    navigation.indexOf('// Retire every live physical entry'),
  )
  const idempotentGuard = consume.indexOf("if (!rec || rec.status !== 'live')")
  const decrement = consume.indexOf('const n = m.get(key) || 0')
  assert.ok(idempotentGuard >= 0)
  assert.ok(decrement > idempotentGuard)
  assert.match(consume, /rec\.status = reversible \? 'dormant' : 'consumed'/)
})

test('removing the live iframe retires its host navigation even without an AppCanvas unmount', () => {
  assert.match(
    canvas,
    /if \(v === liveVersionRef\.current\) onNavReset\?\.\(appId\)[\s\S]*framesRef\.current\.delete\(v\)/,
  )
})

test('pointer input inside an opaque app frame focuses its owning pane', () => {
  assert.match(frame, /pointerdown', notifyParentFocus/)
  assert.match(frame, /type: 'moebius:frame-focus'/)
  assert.match(canvas, /msg\.type === 'moebius:frame-focus'[\s\S]*onAppFocus\?\.\(appId\)/)
  assert.match(shell, /const focusAppPane = useCallback[\s\S]*type: 'FOCUS', paneId: pane\.id/)
  assert.match(shell, /onAppFocus=\{focusAppPane\}/)
})

test('an explicit deep link replaces only a fallback implicit home tab', () => {
  assert.match(
    workspaceSession,
    /const replaceImplicitBootTab = !blobValid[\s\S]*Object\.keys\(workspace\.panes\)\.length === 1[\s\S]*paneModel\.flatten\(workspace\)\.length <= 1/,
  )
  assert.match(
    navigation,
    /dispatchWorkspace\(replaceImplicitBootTab[\s\S]*type: 'RESET_FLAT', tabs: \[tab\][\s\S]*type: 'OPEN_TAB'/,
  )
})

test('the legacy active destination wins every blob-invalid flat-tab boot', () => {
  assert.match(
    navigation,
    /else if \(!blobValid && initialNav\.view === 'canvas'[\s\S]*openBootTab\(tabModel\.makeTab\('app'/,
  )
  assert.match(
    navigation,
    /else if \(!blobValid && initialNav\.chatId != null\)[\s\S]*openBootTab\(tabModel\.makeTab\('chat'/,
  )
  assert.doesNotMatch(
    navigation,
    /!blobValid && bootPaneEmpty/,
    'a legacy flat-tab seed must not suppress the active destination',
  )
})

// ── M1: a SUSPENDED Settings takeover must not poison builder nav/history ─────
// The single-world takeover is suspended in builder (the tree paints), so the two
// nav-bookkeeping consumers that decide "what is visible / what did Back see" must
// read the PAINTED overlay (world-gated), never the raw settingsOpen flag.
test('M1: overlayShowingForWs is the world-gated PAINTED takeover, not the raw flag', () => {
  // Mirrors the render-time overlayShowing derivation. This is the one predicate
  // both consumers below share.
  assert.match(
    navigation,
    /const overlayShowingForWs = useCallback\(\s*\(ws\) => settingsOpenRef\.current\s*&& ws\.viewMode === 'single'/,
  )
})

test('M1: appOwnerPaneId gates on the painted overlay, never the raw settingsOpen flag', () => {
  const owner = navigation.slice(
    navigation.indexOf('const appOwnerPaneId = useCallback'),
    navigation.indexOf('const isVisibleApp = useCallback'),
  )
  assert.ok(owner.length > 0)
  // The early-out consults the world-gated overlay for THIS ws...
  assert.match(owner, /if \(appId == null \|\| overlayShowingForWs\(ws\)\) return null/)
  // ...and never rejects every app on the raw suspended flag (the M1 bug).
  assert.doesNotMatch(owner, /settingsOpenRef\.current/)
})

test('M1: snapshotRoute records Settings only when the takeover actually paints', () => {
  const snap = navigation.slice(
    navigation.indexOf('const snapshotRoute = useCallback'),
    navigation.indexOf('const pushShellEntry = useCallback'),
  )
  assert.ok(snap.length > 0)
  assert.match(snap, /const view = overlayShowingForWs\(ws\) \? 'settings' : content\.view/)
  // Back must never record 'settings' from the raw suspended flag in builder.
  assert.doesNotMatch(snap, /settingsOpenRef\.current \? 'settings'/)
})
