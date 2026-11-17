import { after, afterEach, test } from 'node:test'
import assert from 'node:assert/strict'
import { act } from 'react'
import { h, mount, unmountAll } from './mountedRender.js'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
after(() => vite.close())
const clock = await vite.ssrLoadModule('/src/components/ChatView/countdownClock.js')

test('wait countdowns share one quarter-minute clock', () => {
  assert.equal(clock.COUNTDOWN_TICK_MS, 15_000)
  // Server rendering reads the shared time without starting a timer.
  assert.equal(typeof clock.useCountdownNow, 'function')
  assert.equal(clock._countdownListenerCount(), 0)
})

afterEach(unmountAll)

test('countdowns start one timer, share ticks, and stop after the last active subscriber', t => {
  let time = 1000
  let tick
  let starts = 0
  let stops = 0
  t.mock.method(Date, 'now', () => time)
  t.mock.method(globalThis, 'setInterval', (callback, delay) => {
    assert.equal(delay, clock.COUNTDOWN_TICK_MS)
    tick = callback
    starts += 1
    return 123
  })
  t.mock.method(globalThis, 'clearInterval', id => {
    assert.equal(id, 123)
    stops += 1
  })
  function Countdown({ active = true }) {
    return h('span', null, String(clock.useCountdownNow(active)))
  }
  const first = mount(h(Countdown))
  const second = mount(h(Countdown))
  const inactive = mount(h(Countdown, { active: false }))
  assert.equal(starts, 1)
  assert.equal(clock._countdownListenerCount(), 2)
  assert.equal(first.container.textContent, '1000')
  time = 16000
  act(() => tick())
  assert.equal(first.container.textContent, '16000')
  assert.equal(second.container.textContent, '16000')
  first.unmount()
  assert.equal(stops, 0)
  second.rerender(h(Countdown, { active: false }))
  assert.equal(stops, 1)
  assert.equal(clock._countdownListenerCount(), 0)
  time = 31000
  second.rerender(h(Countdown))
  assert.equal(starts, 2)
  assert.equal(second.container.textContent, '31000', 'a sleeping clock catches up on subscription')
  second.unmount()
  inactive.unmount()
  assert.equal(stops, 2)
  assert.equal(clock._countdownListenerCount(), 0)
})

test('reply footers share one minute clock that also refreshes when the tab is shown', t => {
  let time = 1000
  let tick
  let starts = 0
  const shown = []
  t.mock.method(Date, 'now', () => time)
  t.mock.method(globalThis, 'setInterval', (callback, delay) => {
    assert.equal(delay, clock.MINUTE_TICK_MS)
    tick = callback
    starts += 1
    return 456
  })
  t.mock.method(globalThis, 'clearInterval', () => {})
  t.mock.method(document, 'addEventListener', (type, listener) => { if (type === 'visibilitychange') shown.push(listener) })
  t.mock.method(document, 'removeEventListener', (type, listener) => {
    if (type === 'visibilitychange') shown.splice(shown.indexOf(listener), 1)
  })
  function Footer() {
    return h('span', null, String(clock.useMinuteNow(true)))
  }
  const footers = [mount(h(Footer)), mount(h(Footer)), mount(h(Footer))]
  assert.equal(starts, 1, 'one timer for every footer')
  assert.equal(shown.length, 1, 'one visibility listener for every footer')
  assert.equal(clock._minuteListenerCount(), 3)
  time = 61000
  act(() => tick())
  assert.deepEqual(footers.map(view => view.container.textContent), ['61000', '61000', '61000'])
  time = 500000
  document.visibilityState = 'visible'
  act(() => shown[0]())
  assert.equal(footers[0].container.textContent, '500000', 'a shown tab reads the time at once')
  // A footer joining the running clock reads the time now, not the last tick
  // up to a minute ago, and the footers already shown move with it.
  time = 530000
  footers.push(mount(h(Footer)))
  assert.equal(starts, 1, 'still one timer')
  assert.deepEqual(footers.map(view => view.container.textContent), ['530000', '530000', '530000', '530000'])
  footers.forEach(view => view.unmount())
  assert.equal(clock._minuteListenerCount(), 0)
  assert.equal(shown.length, 0, 'the last footer leaving removes the listener')
})
