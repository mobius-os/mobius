import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const canvas = readFileSync(
  resolve(here, '../../components/AppCanvas/AppCanvas.jsx'),
  'utf8',
)

test('microphone sessions are bound to one live app document', () => {
  assert.match(canvas, /sourceWindow, sourceVersion: srcVersion/)
  assert.match(canvas, /pending\.sourceVersion !== liveVersionRef\.current/)
  assert.match(
    canvas,
    /useLayoutEffect\(\(\) => \{[\s\S]*?if \(visible && capture\.sourceVersion === swap\.liveVersion\) return[\s\S]*?cancelMicrophoneCapture\(microphoneCaptureRef, \{ notifyFrame: !visible \}\)[\s\S]*?\}, \[visible, swap\.liveVersion\]\)/,
  )
  assert.match(
    canvas,
    /if \(loadedDocsRef\.current\.has\(v\)\) \{[\s\S]*?v === liveVersionRef\.current[\s\S]*?cancelMicrophoneCapture/,
  )
  assert.match(
    canvas,
    /if \(v === liveVersionRef\.current && capture\?\.sourceVersion === v\) \{[\s\S]*?cancelMicrophoneCapture\(microphoneCaptureRef\)/,
  )
})

test('a visible background pane keeps its microphone session', () => {
  assert.match(canvas, /if \(!visibleRef\.current \|\| !requestId\) return/)
  assert.doesNotMatch(canvas, /if \(!activeRef\.current \|\| !requestId\) return/)
  assert.match(canvas, /const mayDeliver = microphoneCaptureRef\.current === pending[\s\S]*?&& visibleRef\.current/)
})

test('deactivation settles the cached frame without releasing samples to it', () => {
  assert.match(canvas, /if \(notifyFrame\) \{[\s\S]*?name: 'AbortError'/)
  assert.match(canvas, /message: 'Recording cancelled because the app is no longer active\.'/)
})

test('microphone results and errors are re-authorized at delivery time', () => {
  const deliveryGuards = canvas.match(/const mayDeliver = microphoneCaptureRef\.current === pending/g) || []
  assert.equal(deliveryGuards.length, 2)
  assert.match(canvas, /mayDeliver[\s\S]*?if \(!mayDeliver\) return[\s\S]*?moebius:microphone-result/)
  assert.match(canvas, /if \(!mayDeliver \|\| error\?\.name === 'AbortError'\) return/)
})

test('invalid microphone correlation ids are rejected instead of truncated', () => {
  assert.match(canvas, /msg\.requestId\.length <= 120/)
  assert.doesNotMatch(canvas, /msg\.requestId\.slice\(0, 120\)/)
})
