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
  assert.match(ordinaryBack, /type: 'OPEN_TAB'/)
  assert.match(ordinaryBack, /moebius:nav-back/)
})

test('app-entry consumption cannot decrement the same owner twice', () => {
  assert.match(
    navigation,
    /if \(!rec \|\| rec\.status !== 'live'\) \{\s*consumedAppEntryIdsRef\.current\.add\(entryId\)\s*return/,
  )
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
