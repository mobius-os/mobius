import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  createNestedScrollHandoff,
} from '../scroll/nestedScrollHandoff.js'

function fixture({
  nestedTop = 120,
  nestedHeight = 300,
  nestedClient = 100,
  onHandoff = null,
} = {}) {
  const nested = {
    scrollTop: nestedTop,
    scrollHeight: nestedHeight,
    clientHeight: nestedClient,
    closest: selector => selector === '[data-chat-scroll-region], .chat__scroll'
      ? nested
      : null,
  }
  const child = { closest: nested.closest }
  const outer = {
    scrollTop: 400,
    contains: node => node === nested,
  }
  const handlers = createNestedScrollHandoff(outer, {
    readStyle: () => ({ lineHeight: '20px', fontSize: '15px' }),
    onHandoff,
  })
  return { child, handlers, nested, outer }
}

function wheel(target, properties = {}) {
  let prevented = false
  return {
    target,
    deltaY: 0,
    deltaMode: 0,
    cancelable: true,
    preventDefault: () => { prevented = true },
    prevented: () => prevented,
    ...properties,
  }
}

test('wheel handoff preserves native scrolling until a pixel delta crosses the edge', () => {
  const ownership = []
  const { child, handlers, nested, outer } = fixture({
    onHandoff: input => ownership.push({ input, outerTop: outer.scrollTop }),
  })
  const within = wheel(child, { deltaY: 40 })
  handlers.onWheel(within)
  assert.equal(nested.scrollTop, 120)
  assert.equal(outer.scrollTop, 400)
  assert.equal(within.prevented(), false)

  nested.scrollTop = 190
  const crossing = wheel(child, { deltaY: 25 })
  handlers.onWheel(crossing)
  assert.equal(nested.scrollTop, 200, 'nested surface consumes its remaining range')
  assert.equal(outer.scrollTop, 415, 'only the residual reaches the transcript')
  assert.equal(crossing.prevented(), true)
  assert.deepEqual(ownership, [{
    input: { delta: 25, type: 'wheel' },
    outerTop: 400,
  }], 'reader ownership is claimed before the outer scroll mutates')
  handlers.dispose()
})

test('line and page wheel handlers hand off in CSS pixels without scale factors', () => {
  const { child, handlers, nested, outer } = fixture({ nestedTop: 200 })
  const lines = wheel(child, { deltaY: 3, deltaMode: 1 })
  handlers.onWheel(lines)
  assert.equal(outer.scrollTop, 460, 'three computed 20px lines stay in CSS pixels')
  assert.equal(lines.prevented(), true)

  outer.scrollTop = 400
  nested.scrollTop = 0
  const page = wheel(child, { deltaY: -1, deltaMode: 2 })
  handlers.onWheel(page)
  assert.equal(outer.scrollTop, 300, 'one page is the nested 100px client height')
  assert.equal(page.prevented(), true)
  handlers.dispose()
})

test('delegation covers descendant targets in every marked nested region', () => {
  const { child, handlers, nested, outer } = fixture({ nestedTop: 200 })
  const event = wheel(child, { deltaY: 30 })
  handlers.onWheel(event)
  assert.equal(outer.scrollTop, 430)
  assert.equal(event.prevented(), true)

  const unrelated = { closest: () => null }
  const outside = wheel(unrelated, { deltaY: 30 })
  handlers.onWheel(outside)
  assert.equal(outer.scrollTop, 430)
  assert.equal(outside.prevented(), false)
  nested.scrollTop = 0
  handlers.dispose()
})

test('ctrl-wheel zoom and scrollable room retain native browser behavior', () => {
  const { child, handlers, nested, outer } = fixture({ nestedTop: 200 })
  const zoom = wheel(child, { deltaY: 50, ctrlKey: true })
  handlers.onWheel(zoom)
  assert.equal(outer.scrollTop, 400)
  assert.equal(zoom.prevented(), false)

  nested.scrollTop = 80
  const native = wheel(child, { deltaY: -30 })
  handlers.onWheel(native)
  assert.equal(outer.scrollTop, 400)
  assert.equal(native.prevented(), false)
  handlers.dispose()
})

test('touch handoff follows one contact and cleanup removes the full lifecycle', () => {
  const ownership = []
  const { child, handlers, nested, outer } = fixture({
    nestedTop: 200,
    onHandoff: input => ownership.push({ input, outerTop: outer.scrollTop }),
  })
  handlers.onTouchStart({
    target: child,
    touches: [{ identifier: 7, clientY: 100 }],
  })
  let prevented = false
  handlers.onTouchMove({
    touches: [{ identifier: 7, clientY: 70 }],
    preventDefault: () => { prevented = true },
  })
  assert.equal(outer.scrollTop, 430)
  assert.equal(prevented, true)
  assert.deepEqual(ownership, [{
    input: { delta: 30, type: 'touchmove' },
    outerTop: 400,
  }], 'reader ownership is claimed with direction before scroll mutation')

  handlers.onTouchEnd({ touches: [] })
  handlers.dispose()
  handlers.onWheel(wheel(child, { deltaY: 30 }))
  assert.equal(outer.scrollTop, 430, 'disposed controller cannot mutate scrolling')
  assert.equal(nested.scrollTop, 200)
})
