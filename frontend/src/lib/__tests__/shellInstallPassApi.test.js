import test from 'node:test'
import assert from 'node:assert/strict'

import {
  api,
  SHELL_INSTALL_PASS_TIMEOUT_MS,
} from '../../api/client.js'

function neverSettlingFetch(_url, { signal }) {
  return new Promise((_resolve, reject) => {
    if (signal.aborted) {
      reject(signal.reason)
      return
    }
    signal.addEventListener('abort', () => reject(signal.reason), { once: true })
  })
}

test('shell handoff requests bound never-settling transports', async (t) => {
  const originalFetch = globalThis.fetch
  t.after(() => { globalThis.fetch = originalFetch })
  globalThis.fetch = neverSettlingFetch
  assert.equal(SHELL_INSTALL_PASS_TIMEOUT_MS, 5000)

  for (const request of [
    api.auth.shellInstallPass.prepare,
    api.auth.shellInstallPass.redeem,
    api.auth.shellInstallPass.revoke,
  ]) {
    await assert.rejects(
      request({ timeoutMs: 5 }),
      error => error?.name === 'TimeoutError',
    )
  }
})

test('shell handoff requests honor caller cancellation', async (t) => {
  const originalFetch = globalThis.fetch
  t.after(() => { globalThis.fetch = originalFetch })
  globalThis.fetch = neverSettlingFetch
  const controller = new AbortController()
  const pending = api.auth.shellInstallPass.prepare({
    signal: controller.signal,
  })
  controller.abort()

  await assert.rejects(pending, error => error?.name === 'AbortError')
})
