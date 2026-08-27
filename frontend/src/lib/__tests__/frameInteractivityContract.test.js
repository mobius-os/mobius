import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const src = resolve(here, '../..')
const frame = readFileSync(resolve(src, '../public/app-frame.html'), 'utf8')
const canvas = readFileSync(resolve(src, 'components/AppCanvas/AppCanvas.jsx'), 'utf8')
const shell = readFileSync(resolve(src, 'components/Shell/Shell.jsx'), 'utf8')
const frameCacheModel = readFileSync(
  resolve(src, 'components/Shell/appFrameCache.js'),
  'utf8',
)

test('frame suspension reaches the live app before paint', () => {
  assert.match(canvas, /frameVisible = visible/)
  assert.match(canvas, /useEffect\(\(\) => \{[\s\S]*sendVisibility\(swap\.liveVersion, frameVisible\)/)
  assert.match(canvas, /useLayoutEffect\(\(\) => \{[\s\S]*sendInteractivity\(swap\.liveVersion, interactive, frameVisible\)/)
  assert.match(canvas, /suspendScrolling:\s*frameIsVisible\s*&&\s*!enabled/)
  assert.match(canvas, /moebius:frame-interactivity/)
})

test('hidden app-frame history stays device-bounded without limiting open tabs', () => {
  assert.match(frameCacheModel, /const BASE_APP_CACHE_MAX = 6/)
  assert.match(frameCacheModel, /const HIGH_MEMORY_APP_CACHE_MAX = 10/)
  assert.doesNotMatch(shell, /openTabs\.slice\(/)
})

test('iframe history retirement runs at the committed layout boundary, never during render', () => {
  assert.match(
    canvas,
    /useLayoutEffect\(\(\) => \{\s*if \(!appId\) return\s*return \(\) => \{ onNavReset\?\.\(appId\) \}/,
  )
  const cacheDerivation = frameCacheModel.slice(
    frameCacheModel.indexOf('export function deriveRenderedAppIds'),
  )
  assert.ok(cacheDerivation.length > 0)
  assert.doesNotMatch(cacheDerivation, /retireAppHistory/)
})

test('frame suspension cancels compositor momentum without changing the resting offset', () => {
  assert.match(frame, /function cancelScrollerMomentum\(element\)/)
  assert.match(frame, /element\.scrollTop = top < maxTop \? top \+ 1/)
  assert.match(frame, /element\.scrollTop = top;/)
  assert.match(frame, /data-mobius-frame-suspended/)
  assert.match(frame, /suspendedScrollFrame = requestAnimationFrame\(holdSuspendedScroll\)/)
})
