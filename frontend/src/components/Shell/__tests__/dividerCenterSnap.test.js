import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  CENTER_SNAP_ENTER_PX,
  CENTER_SNAP_RELEASE_PX,
  dividerDistanceFromScreenCenter,
  screenCenterRatio,
  snapRatioToScreenCenter,
} from '../dividerCenterSnap.js'

const CONTENT = { x: 20, y: 40, w: 1000, h: 800 }

test('a root divider resolves the content viewport midpoint, including its gap', () => {
  const divider = {
    dir: 'row', origin: 20, span: 993, x: 516, y: 40, w: 7, h: 800, ratio: 0.5,
  }
  assert.equal(screenCenterRatio(divider, CONTENT), 0.5)
  assert.equal(dividerDistanceFromScreenCenter(divider, CONTENT), 0)
})

test('an omitted content origin uses the shell layout contract of local zero', () => {
  const divider = {
    dir: 'col', origin: 0, span: 445, x: 0, y: 223, w: 426, h: 7, ratio: 0.5,
  }
  const content = { w: 426, h: 452 }
  assert.equal(screenCenterRatio(divider, content), 0.5)
  assert.equal(dividerDistanceFromScreenCenter(divider, content), 0)
})

test('a nested divider targets the screen midpoint rather than its local half', () => {
  const divider = {
    dir: 'col', origin: 200, span: 600, x: 20, y: 430, w: 1000, h: 7, ratio: 0.5,
  }
  const target = screenCenterRatio(divider, CONTENT)
  assert.ok(Math.abs(target - ((440 - 200 - 3.5) / 600)) < 1e-12)
  assert.notEqual(target, 0.5)
})

test('a divider whose parent region does not cross screen center has no snap target', () => {
  assert.equal(screenCenterRatio({
    dir: 'row', origin: 650, span: 300, w: 7,
  }, CONTENT), null)
})

test('center snap enters close to the midpoint and uses a wider release threshold', () => {
  const span = 1000
  const target = 0.5
  assert.deepEqual(
    snapRatioToScreenCenter(target + CENTER_SNAP_ENTER_PX / span, target, span),
    { ratio: target, snapped: true },
  )
  assert.equal(
    snapRatioToScreenCenter(target + (CENTER_SNAP_ENTER_PX + 1) / span, target, span).snapped,
    false,
  )
  assert.equal(
    snapRatioToScreenCenter(target + CENTER_SNAP_RELEASE_PX / span, target, span, true).snapped,
    true,
  )
  assert.equal(
    snapRatioToScreenCenter(target + (CENTER_SNAP_RELEASE_PX + 1) / span, target, span, true).snapped,
    false,
  )
  assert.deepEqual(
    snapRatioToScreenCenter(0.01, null, span),
    { ratio: 0.01, snapped: false },
    'a divider that cannot reach screen center never snaps toward ratio zero',
  )
})
