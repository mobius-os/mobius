import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const canvas = readFileSync(
  new URL('../../components/AppCanvas/AppCanvas.jsx', import.meta.url),
  'utf8',
)
const canvasCss = readFileSync(
  new URL('../../components/AppCanvas/AppCanvas.css', import.meta.url),
  'utf8',
)
const frame = readFileSync(
  new URL('../../../public/app-frame.html', import.meta.url),
  'utf8',
)

test('a direct app intent stays covered until the exact live frame applies it', () => {
  assert.match(canvas, /const pendingIntentRef = useRef\(pendingIntent\)/)
  assert.match(canvas, /if \(srcVersion !== liveVersionRef\.current\) return[\s\S]*msg\.type === 'moebius:app-intent-applied'/)
  assert.match(canvas, /msg\.nonce !== pending\.nonce/)
  assert.match(canvas, /onIntentDelivered\?\.\(appId, pending\)/)
  assert.match(canvas, /\(!swap\.liveLoaded \|\| intentHandoffPending\)/)
  assert.match(canvas, /canvas--intent-pending/)
  assert.match(canvasCss, /\.canvas--intent-pending\s*\{[\s\S]*pointer-events:\s*none/)
  assert.match(canvasCss, /\.canvas-loading\s*\{[\s\S]*animation:\s*canvas-loading-in 80ms 120ms ease-out both/)
  assert.match(canvasCss, /\.canvas-loading--intent-handoff\s*\{[\s\S]*animation:\s*none/)
})

test('posting an intent no longer uncovers the app before its acknowledgement', () => {
  const delivery = canvas.slice(
    canvas.indexOf('// One-shot shell intent'),
    canvas.indexOf('// ── P1-A:'),
  )
  assert.match(delivery, /postToFrame\(swap\.liveVersion/)
  assert.doesNotMatch(delivery, /onIntentDelivered/)
})

test('the frame acknowledges after app handlers get two commit turns', () => {
  const acknowledgement = frame.slice(
    frame.indexOf('function acknowledgeAppIntent'),
    frame.indexOf('function requestModuleBytes'),
  )
  assert.match(acknowledgement, /requestAnimationFrame\(\(\) => \{\s*requestAnimationFrame/)
  assert.match(acknowledgement, /setTimeout\(send, 120\)/)
  assert.match(acknowledgement, /type: 'moebius:app-intent-applied'/)
  assert.match(frame, /msg\.type === 'moebius:app-intent'[\s\S]*acknowledgeAppIntent\(msg\.nonce\)/)
})
