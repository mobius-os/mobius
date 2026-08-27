/* Regression contract for the one-pass send landing: the scroll controller
 * must publish the committed composer height before it measures the dynamic
 * reservation. Otherwise the foot's post-paint ResizeObserver can clamp a new
 * pin and produce the visible "up, pause, tiny bit further up" correction. */

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const controller = readFileSync(new URL('../useScrollMode.js', import.meta.url), 'utf8')
const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')

test('send landing synchronizes composer geometry before reservation math', () => {
  const sizeStart = controller.indexOf('function sizeSpacer(')
  const sizeEnd = controller.indexOf('\n    function maybeApplyMode(', sizeStart)
  assert.ok(sizeStart >= 0 && sizeEnd > sizeStart, 'sizeSpacer block exists')

  const sizeSpacer = controller.slice(sizeStart, sizeEnd)
  const gate = sizeSpacer.indexOf('layoutOwnsScroll(authorityVersion)')
  const sync = sizeSpacer.indexOf("style.setProperty('--composer-h'")
  const measure = sizeSpacer.indexOf('_computeSpacerH(')
  assert.ok(gate >= 0 && sync > gate,
    'sizeSpacer owns composer clearance behind reader authority')
  assert.ok(measure > sync,
    'composer height must be published before list/spacer geometry is measured')
})

test('ChatView gives its composer elements to the scroll owner', () => {
  const callStart = chatView.indexOf('} = useScrollMode({')
  const callEnd = chatView.indexOf('\n  })', callStart)
  assert.ok(callStart >= 0 && callEnd > callStart, 'useScrollMode call exists')
  const args = chatView.slice(callStart, callEnd)
  assert.match(args, /\bchatRef,\s*\n\s*footRef,/)
  assert.doesNotMatch(chatView, /style\.setProperty\(\s*['"]--composer-h['"]/,
    'ChatView must notify geometry changes without publishing scroll geometry')
})

test('footer resizes are owned by the scroll controller observer', () => {
  assert.match(controller, /ro\.observe\(scrollEl\)/)
  assert.match(controller, /ro\.observe\(footRef\.current\)/)
  assert.doesNotMatch(controller, /querySelector\(['"]\.queued['"]\)/,
    'footer descendants must not add duplicate geometry observers')
  assert.doesNotMatch(chatView, /new ResizeObserver\(applySoon\)|composerResized/,
    'ChatView must not bridge footer geometry through a second observer')
})

test('gesture settlement replays deferred footer geometry and mode in one task', () => {
  const replayStart = controller.indexOf('const replayDeferredLayoutNow = () => {')
  const replayEnd = controller.indexOf('\n    const resumeLayoutAfterGesture', replayStart)
  assert.ok(replayStart >= 0 && replayEnd > replayStart, 'atomic replay exists')
  const replay = controller.slice(replayStart, replayEnd)
  assert.match(replay, /syncLayout\(\{ forceApply: true, authorityVersion \}\)/)

  const settleStart = controller.indexOf('const settleReaderScroll = () => {')
  const settleEnd = controller.indexOf('\n    const releasePendingGesture', settleStart)
  assert.ok(settleStart >= 0 && settleEnd > settleStart, 'reader settlement exists')
  const settle = controller.slice(settleStart, settleEnd)
  assert.match(settle, /if \(!replayDeferredLayoutNow\(\)\)\s*\{[\s\S]*?syncLayout\(\{ forceApply: true, authorityVersion: currentAuthority\(\) \}\)/,
    'settlement must not publish footer geometry before its compensating mode write')
})
