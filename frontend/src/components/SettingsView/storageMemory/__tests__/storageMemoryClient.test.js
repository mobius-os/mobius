// The Storage and Memory box gets decoded data from its calls, and refusals keep their reason.
import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { api } from '../../../../api/client.js'

const originalFetch = globalThis.fetch
afterEach(() => { globalThis.fetch = originalFetch })

const json = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})

function stubFetch(response) {
  const calls = []
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ path: String(url).replace(/^.*\/api/, ''), method: init.method || 'GET' })
    return response
  }
  return calls
}

test('the overview call returns the decoded overview, not a raw response', async () => {
  const calls = stubFetch(json({ storage: { free_bytes: 1 }, memory: null, cleanable: [], kept: [], running: [] }))
  const overview = await api.resources.overview({ refresh: true })
  assert.equal(overview.storage.free_bytes, 1)
  assert.deepEqual(calls, [{ path: '/resources?refresh=true', method: 'GET' }])
})

test('a cleanup runs as a POST and returns what it freed', async () => {
  const calls = stubFetch(json({ cleanup: { target: 'work_files', bytes: 5 }, resources: {} }))
  const result = await api.resources.runCleanup('work_files')
  assert.equal(result.cleanup.bytes, 5)
  assert.deepEqual(calls, [{ path: '/resources/cleanup/work_files', method: 'POST' }])
})

test('a busy cleanup refusal keeps the server reason', async () => {
  stubFetch(json({ detail: 'Another cleanup is still running.' }, 409))
  await assert.rejects(api.resources.runCleanup('agent_sessions'), /Another cleanup is still running\./)
})
