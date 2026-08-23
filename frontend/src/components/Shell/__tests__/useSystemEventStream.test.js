import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../ChatView/hooks/__tests__/react-hook-shim.mjs'

function target(extra = {}) {
  return {
    ...extra,
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() {},
  }
}

async function flushMicrotasks() {
  for (let index = 0; index < 10; index += 1) await Promise.resolve()
}

test('a clean system-stream EOF enters the shared reachability owner', async () => {
  globalThis.window = target({ location: { reload() {} } })
  globalThis.document = target({ visibilityState: 'visible' })
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: { onLine: true },
  })
  globalThis.localStorage = {
    getItem(key) { return key === 'token' ? 'owner-token' : null },
    removeItem() {},
  }
  globalThis.sessionStorage = { setItem() {} }

  const requests = []
  let holdHealthProbe
  globalThis.fetch = async (url) => {
    requests.push(String(url))
    if (String(url).endsWith('/api/health')) {
      return new Promise(resolve => { holdHealthProbe = resolve })
    }
    return {
      ok: true,
      status: 200,
      body: { getReader: () => ({ read: async () => ({ done: true }) }) },
    }
  }

  const connectivity = await import('../../../lib/connectivityStore.js')
  const { default: useSystemEventStream } = await import(
    '../../../hooks/useSystemEventStream.js'
  )
  const hook = renderHook(useSystemEventStream, () => {})
  await flushMicrotasks()

  assert.equal(requests.filter(url => url.endsWith('/api/events/system')).length, 1)
  assert.equal(requests.filter(url => url.endsWith('/api/health')).length, 1)
  assert.equal(
    connectivity.getReachabilityPhaseSnapshot(),
    connectivity.ReachabilityPhase.CHECKING,
  )

  holdHealthProbe?.({ ok: true })
  hook.unmount()
})
