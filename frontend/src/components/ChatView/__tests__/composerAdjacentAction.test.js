import test from 'node:test'
import assert from 'node:assert/strict'
import { composerAdjacentActionProps } from '../composerAdjacentAction.js'

function event(pointerType) {
  let prevented = false
  return {
    pointerType,
    preventDefault() { prevented = true },
    get defaultPrevented() { return prevented },
  }
}

test('touch activation preserves composer focus and does not wait for click', () => {
  let activations = 0
  const props = composerAdjacentActionProps(
    () => { activations++ },
    { activateOnTouchEnd: true },
  )
  const pointerDown = event('touch')
  const touchEnd = event('touch')

  props.onPointerDown(pointerDown)
  assert.equal(pointerDown.defaultPrevented, true)
  assert.equal(activations, 0, 'touch begins without activating the action')

  props.onTouchEnd(touchEnd)
  assert.equal(touchEnd.defaultPrevented, true,
    'touchend suppresses the delayed synthetic click')
  assert.equal(activations, 1, 'touchend activates at the stable target position')
})

test('stable controls preserve focus but wait for their native click', () => {
  let activations = 0
  const props = composerAdjacentActionProps(() => { activations++ })
  const pointerDown = event('touch')

  props.onPointerDown(pointerDown)
  assert.equal(pointerDown.defaultPrevented, true)
  assert.equal(props.onTouchEnd, undefined,
    'stable controls do not manufacture a second touch activation path')
  assert.equal(activations, 0)

  props.onClick()
  assert.equal(activations, 1)
})

test('mouse and keyboard retain the native click path', () => {
  let activations = 0
  const props = composerAdjacentActionProps(() => { activations++ })
  const pointerDown = event('mouse')

  props.onPointerDown(pointerDown)
  assert.equal(pointerDown.defaultPrevented, false,
    'mouse pointerdown keeps native button focus behavior')
  assert.equal(activations, 0)

  props.onClick()
  assert.equal(activations, 1,
    'native click remains the activation path for mouse and keyboard')
})
