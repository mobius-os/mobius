/** Brain scrollbar visibility follows movement, never opening or hovering. */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from './react-hook-shim.mjs'
import useScrollActivity, { SCROLL_IDLE_HIDE_MS } from '../useScrollActivity.js'

test('starts hidden, shows on scrolling, and hides after the last movement', t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const panel = new EventTarget()
  panel.dataset = {}
  const ref = { current: panel }
  const { rerender } = renderHook(useScrollActivity, ref, true)
  assert.equal('scrolling' in panel.dataset, false)
  panel.dispatchEvent(new Event('pointerover'))
  assert.equal('scrolling' in panel.dataset, false)
  panel.dispatchEvent(new Event('scroll'))
  assert.equal('scrolling' in panel.dataset, true)
  t.mock.timers.tick(SCROLL_IDLE_HIDE_MS - 100)
  panel.dispatchEvent(new Event('scroll'))
  t.mock.timers.tick(SCROLL_IDLE_HIDE_MS - 100)
  assert.equal('scrolling' in panel.dataset, true, 'renewed movement restarts the grace period')
  t.mock.timers.tick(100)
  assert.equal('scrolling' in panel.dataset, false)

  panel.dispatchEvent(new Event('scroll'))
  rerender(ref, false)
  assert.equal('scrolling' in panel.dataset, false)
  panel.dispatchEvent(new Event('scroll'))
  assert.equal('scrolling' in panel.dataset, false, 'closing removes the listener')
  rerender(ref, true)
  assert.equal('scrolling' in panel.dataset, false, 'reopening starts hidden')
  panel.dispatchEvent(new Event('scroll'))
  t.mock.timers.tick(SCROLL_IDLE_HIDE_MS)
  assert.equal('scrolling' in panel.dataset, false)
  rerender(ref, false)
})
