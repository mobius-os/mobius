import assert from 'node:assert/strict'
import test from 'node:test'

import { agentViewport } from '../agentViewport.js'


test('agent viewport preserves CSS geometry and physical display density', () => {
  const viewport = agentViewport({
    innerWidth: 426,
    innerHeight: 860,
    devicePixelRatio: 2.625,
    visualViewport: { height: 812.5 },
  })

  assert.deepEqual(viewport, {
    width: 426,
    height: 812.5,
    pixelRatio: 2.625,
  })
})


test('agent viewport accepts the caller’s keyboard-corrected height', () => {
  const viewport = agentViewport({
    innerWidth: 390,
    innerHeight: 844,
    devicePixelRatio: 3,
    visualViewport: { height: 544 },
  }, 844)

  assert.equal(viewport.height, 844)
  assert.equal(viewport.pixelRatio, 3)
})


test('agent viewport gives non-browser callers an executable density', () => {
  assert.deepEqual(agentViewport({}, 915), {
    width: 1,
    height: 915,
    pixelRatio: 1,
  })
})
