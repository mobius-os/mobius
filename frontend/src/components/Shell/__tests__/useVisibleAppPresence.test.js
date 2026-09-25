import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../ChatView/hooks/__tests__/react-hook-shim.mjs'


function target(extra = {}) {
  return Object.assign(new EventTarget(), extra)
}

function installBrowser() {
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
  const reports = []
  globalThis.fetch = async (url, options) => {
    reports.push({ url, keepalive: options.keepalive, ...JSON.parse(options.body) })
    return { ok: true, status: 204 }
  }
  return reports
}

function setPageVisibility(state) {
  document.visibilityState = state
  document.dispatchEvent(new Event('visibilitychange'))
}


test('the shell reports its visible apps against the live stream only', async () => {
  const reports = installBrowser()
  const { default: useVisibleAppPresence } = await import('../useVisibleAppPresence.js')

  const hook = renderHook(useVisibleAppPresence, null, new Set(['7']))
  assert.deepEqual(reports, [], 'no stream yet, nothing to bind a report to')

  hook.rerender('stream-a', new Set(['7', '3']))
  assert.deepEqual(reports.map(r => [r.url, r.sequence, r.app_ids]), [
    ['/api/events/system/stream-a/visible-apps', 1, ['3', '7']],
  ])
  // Survives the page being frozen right after it hides.
  assert.equal(reports[0].keepalive, true)

  // A re-derived but equal set is not a new report.
  hook.rerender('stream-a', new Set(['3', '7']))
  assert.equal(reports.length, 1)

  hook.rerender('stream-a', new Set(['3']))
  assert.deepEqual(reports.at(-1).app_ids, ['3'])
  hook.unmount()
})

test('a hidden page reports nothing visible, and returning re-reports', async () => {
  const reports = installBrowser()
  const { default: useVisibleAppPresence } = await import('../useVisibleAppPresence.js')

  const hook = renderHook(useVisibleAppPresence, 'stream-a', new Set(['7']))
  setPageVisibility('hidden')
  setPageVisibility('visible')
  assert.deepEqual(reports.map(r => [r.sequence, r.app_ids]), [
    [1, ['7']],
    [2, []],
    [3, ['7']],
  ])
  hook.unmount()
})

test('a reconnected stream is reported afresh with a newer sequence', async () => {
  const reports = installBrowser()
  const { default: useVisibleAppPresence } = await import('../useVisibleAppPresence.js')

  const hook = renderHook(useVisibleAppPresence, 'stream-a', new Set(['7']))
  hook.rerender(null, new Set(['7']))
  hook.rerender('stream-b', new Set(['7']))
  assert.deepEqual(reports.map(r => [r.url, r.sequence]), [
    ['/api/events/system/stream-a/visible-apps', 1],
    ['/api/events/system/stream-b/visible-apps', 2],
  ])
  hook.unmount()
})
