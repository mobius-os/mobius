import assert from 'node:assert/strict'
import test from 'node:test'
import { fetchLazyText } from '../lazySidecar.js'

test('a pending sidecar retries and returns the eventual response', async t => {
  const originalFetch = globalThis.fetch
  t.after(() => { globalThis.fetch = originalFetch })
  let calls = 0
  globalThis.fetch = async () => {
    calls += 1
    if (calls === 1) {
      return new Response('', {
        status: 202,
        headers: { 'Retry-After': '0' },
      })
    }
    return new Response('final output', { status: 200 })
  }

  const result = await fetchLazyText('/sidecar')

  assert.equal(calls, 2)
  assert.equal(result.text, 'final output')
})

test('aborting during pending backoff releases the retry immediately', async t => {
  const originalFetch = globalThis.fetch
  t.after(() => { globalThis.fetch = originalFetch })
  let calls = 0
  globalThis.fetch = async () => {
    calls += 1
    return new Response('', {
      status: 202,
      headers: { 'Retry-After': '5' },
    })
  }
  const controller = new AbortController()
  const pending = fetchLazyText('/sidecar', { signal: controller.signal })

  controller.abort()

  await assert.rejects(pending, error => error?.name === 'AbortError')
  assert.equal(calls, 1)
})
