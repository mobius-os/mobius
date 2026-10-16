/* Unsupported inline apps must preserve a startup tap without replaying Send. */
import assert from 'node:assert/strict'
import test from 'node:test'
import { renderHook } from './react-hook-shim.mjs'
import useAppBlockCapability from '../useAppBlockCapability.js'

function fixture() {
  const fallbacks = []
  const h = renderHook(() => useAppBlockCapability({ allowedKeys: new Set(['send:a']), onFallback: key => fallbacks.push(key) }))
  return { ...h, fallbacks }
}

test('unsupported capability opens the advertised view for one pending startup activation', () => {
  const h = fixture()
  h.result.current.remember({ key: 'send:a', event: 'activate' })
  h.result.current.observe(false)
  assert.equal(h.result.current.supported, false)
  assert.deepEqual(h.fallbacks, ['send:a'])
  h.result.current.observe(false)
  assert.deepEqual(h.fallbacks, ['send:a'])
})

test('passive capability, confirmation, cancellation and unknown keys never activate a view', () => {
  for (const event of [null, { key: 'send:a', event: 'confirm' }, { key: 'send:a', event: 'cancel' }, { key: 'other', event: 'activate' }]) {
    const h = fixture()
    h.result.current.remember(event)
    h.result.current.observe(false)
    assert.deepEqual(h.fallbacks, [])
  }
})

test('supported sessions clear startup activation and never replay it on a later capability change', () => {
  const h = fixture()
  h.result.current.remember({ key: 'send:a', event: 'activate' })
  h.result.current.observe(true)
  h.result.current.remember({ key: 'send:a', event: 'activate' })
  h.result.current.observe(false)
  assert.deepEqual(h.fallbacks, [])
})
