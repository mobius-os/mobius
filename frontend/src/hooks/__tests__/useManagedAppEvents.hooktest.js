import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook, __setRerender } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'
import {
  useManagedAppEvents,
  useManagedAppFrameForwarding,
} from '../useManagedAppEvents.js'

function fakeFrames() {
  const posted = []
  const frame = {
    contentWindow: {
      postMessage: (message, origin) => posted.push({ message, origin }),
    },
  }
  return { framesRef: { current: new Map([[0, frame]]) }, posted }
}

function renderShellAndFrame(framesRef, capabilityContract) {
  return renderHook((contract) => {
    const [subscribe, observe] = useManagedAppEvents()
    useManagedAppFrameForwarding(framesRef, subscribe, contract)
    return observe
  }, capabilityContract)
}

test('a finished app update reaches a manager frame in the Store shape', () => {
  const { framesRef, posted } = fakeFrames()
  const hook = renderShellAndFrame(framesRef, { data: { manage_apps: true } })
  assert.deepEqual(posted, [])

  hook.result.current({ type: 'theme_updated' })
  assert.deepEqual(posted, [])

  hook.result.current({ type: 'app_updated', appId: 42 })
  assert.deepEqual(posted, [{
    message: {
      type: 'moebius:managed-app-event',
      event: { type: 'app_updated', appId: '42' },
    },
    origin: '*',
  }])

  // A second completion for the same app is delivered again.
  hook.result.current({ type: 'app_updated', appId: 42 })
  assert.equal(posted.length, 2)
  assert.deepEqual(posted[1].message, posted[0].message)
  hook.unmount()
})

test('frames without manage_apps never receive app lifecycle events', () => {
  for (const contract of [
    null, { data: {} }, { data: { manage_apps: false } },
    { data: { manage_apps: 'true' } },
  ]) {
    const { framesRef, posted } = fakeFrames()
    const hook = renderShellAndFrame(framesRef, contract)
    hook.result.current({ type: 'app_updated', appId: 7 })
    assert.deepEqual(posted, [])
    hook.unmount()
  }
})

test('capability rerenders do not redeliver an event to the same frame', () => {
  const { framesRef, posted } = fakeFrames()
  const hook = renderShellAndFrame(framesRef, { data: { manage_apps: true } })
  hook.result.current({ type: 'app_updated', appId: 7 })
  hook.rerender({ data: { manage_apps: true } })
  hook.rerender({ data: { manage_apps: true } })
  assert.deepEqual(posted.map(item => item.message.event.appId), ['7'])

  // An incidental rerender must not replay the old event to a new frame.
  const replacement = fakeFrames()
  framesRef.current = replacement.framesRef.current
  hook.rerender({ data: { manage_apps: true } })
  assert.equal(replacement.posted.length, 0)
  hook.rerender({ data: { manage_apps: true } })
  assert.equal(replacement.posted.length, 0)
  hook.result.current({ type: 'app_updated', appId: 7 })
  assert.deepEqual(replacement.posted.map(item => item.message.event.appId), ['7'])
  hook.unmount()
})


test('late manager capability does not replay a previously observed event', () => {
  const { framesRef, posted } = fakeFrames()
  const hook = renderShellAndFrame(framesRef, { data: { manage_apps: false } })
  hook.result.current({ type: 'app_updated', appId: 7 })
  hook.rerender({ data: { manage_apps: true } })
  assert.deepEqual(posted, [])
  hook.result.current({ type: 'app_updated', appId: 8 })
  assert.equal(posted.length, 1)
  assert.equal(posted[0].message.event.appId, '8')
  hook.unmount()
})

test('a newly mounted canvas does not replay an earlier completion', () => {
  const { framesRef, posted } = fakeFrames()
  const hook = renderHook((mounted) => {
    const [subscribe, observe] = useManagedAppEvents()
    useManagedAppFrameForwarding(
      framesRef, mounted ? subscribe : null, { data: { manage_apps: true } },
    )
    return observe
  }, false)
  const observe = hook.result.current
  observe({ type: 'app_updated', appId: 7 })
  hook.rerender(true)
  assert.deepEqual(posted, [])
  observe({ type: 'app_updated', appId: 8 })
  assert.deepEqual(posted.map(item => item.message.event.appId), ['8'])
  hook.unmount()
  observe({ type: 'app_updated', appId: 9 })
  assert.equal(posted.length, 1)
})

test('two completions in one system-event batch both reach every manager frame without rendering', () => {
  const { framesRef, posted } = fakeFrames()
  const incoming = fakeFrames()
  framesRef.current.set(1, incoming.framesRef.current.get(0))
  const hook = renderShellAndFrame(framesRef, { data: { manage_apps: true } })
  let pendingRenders = 0
  // Hold state-driven renders until the network chunk has been handled, as
  // React batching does. Delivery must not depend on an intermediate render.
  __setRerender(() => { pendingRenders++ })
  for (const appId of [7, 8]) hook.result.current({ type: 'app_updated', appId })
  for (const messages of [posted, incoming.posted]) {
    assert.deepEqual(messages.map(item => item.message), [
      { type: 'moebius:managed-app-event', event: { type: 'app_updated', appId: '7' } },
      { type: 'moebius:managed-app-event', event: { type: 'app_updated', appId: '8' } },
    ])
  }
  assert.equal(pendingRenders, 0)
  hook.unmount()
})

test('null app ids are dropped and capability revocation stops delivery', () => {
  const { framesRef, posted } = fakeFrames()
  const hook = renderShellAndFrame(framesRef, { data: { manage_apps: true } })
  hook.result.current({ type: 'app_updated', appId: null })
  hook.result.current({ type: 'app_updated' })
  assert.deepEqual(posted, [])
  hook.result.current({ type: 'app_updated', appId: 7 })
  hook.rerender({ data: { manage_apps: false } })
  hook.result.current({ type: 'app_updated', appId: 8 })
  assert.deepEqual(posted.map(item => item.message.event.appId), ['7'])
  hook.unmount()
})
