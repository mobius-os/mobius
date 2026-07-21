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
  const sizeStart = controller.indexOf('function sizeSpacer()')
  const sizeEnd = controller.indexOf('\n    function maybeApplyMode()', sizeStart)
  assert.ok(sizeStart >= 0 && sizeEnd > sizeStart, 'sizeSpacer block exists')

  const sizeSpacer = controller.slice(sizeStart, sizeEnd)
  const sync = sizeSpacer.indexOf('syncComposerGeometry?.()')
  const measure = sizeSpacer.indexOf('_computeSpacerH(')
  assert.ok(sync >= 0, 'sizeSpacer synchronizes the committed composer height')
  assert.ok(measure > sync,
    'composer height must be published before list/spacer geometry is measured')
})

test('ChatView gives its existing composer measurement to the scroll owner', () => {
  const callStart = chatView.indexOf('} = useScrollMode({')
  const callEnd = chatView.indexOf('\n  })', callStart)
  assert.ok(callStart >= 0 && callEnd > callStart, 'useScrollMode call exists')
  const args = chatView.slice(callStart, callEnd)
  assert.match(args, /syncComposerGeometry:\s*measureComposerHeight/)
})
