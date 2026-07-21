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
const frame = readFileSync(resolve(src, '../public/app-frame.html'), 'utf8')

test('one open drawer owns at most one physical sentinel', () => {
  assert.match(
    navigation,
    /function openDrawer\(\) \{\s*\/\/[\s\S]*?if \(drawerOpenRef\.current\) return[\s\S]*?pushShellEntry\('drawer'/,
  )
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
    shell,
    /const replaceImplicitBootTab = !blobValid[\s\S]*legacyOpenTabs\.length === 0[\s\S]*paneModel\.flatten\(workspace\)\.length <= 1/,
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
  // Mirrors the render-time overlayShowing derivation: single world OR builder-
  // Settings flag off. This is the one predicate both consumers below share.
  assert.match(
    navigation,
    /const overlayShowingForWs = useCallback\(\s*\(ws\) => settingsOpenRef\.current\s*&& \(ws\.viewMode === 'single' \|\| !paneModel\.BUILDER_SETTINGS_ENABLED\)/,
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
