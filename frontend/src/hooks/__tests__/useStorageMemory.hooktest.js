// Resource refresh failures preserve earlier readings and never masquerade as fresh success.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'
import { api } from '../../api/client.js'
import useStorageMemory from '../../components/SettingsView/storageMemory/useStorageMemory.js'

const snapshot = { storage: { free_bytes: 1060 }, memory: { used_bytes: 1410 }, cleanable: [], kept: [], running: [] }
function install(t) {
  const original = { ...api.resources, stop: api.chats.stop }
  api.resources.overview = async () => snapshot
  api.chats.stop = async () => new Response('{"stopped": true, "cleared_pending_cids": []}', { headers: { 'Content-Type': 'application/json' } })
  t.after(() => { Object.assign(api.resources, original); api.chats.stop = original.stop; delete api.resources.stop })
  return renderHook(useStorageMemory, { active: false })
}

test('a failed remeasurement preserves the prior snapshot', async t => {
  const { result } = install(t)
  await result.current.measure()
  assert.equal(result.current.overview, snapshot)
  api.resources.overview = async () => { throw new Error('Disk reading failed') }
  await result.current.measure()
  assert.equal(result.current.overview, snapshot)
  assert.equal(result.current.error, 'Disk reading failed')
  assert.equal(result.current.measuring, false)
  api.resources.overview = async () => snapshot
  await result.current.measure()
  assert.equal(result.current.error, '')
})

test('a failed first reading stays absent and can be retried', async t => {
  const { result } = install(t)
  api.resources.overview = async () => { throw new Error('Unavailable') }
  await result.current.measure()
  assert.equal(result.current.overview, null)
  api.resources.overview = async () => snapshot
  await result.current.measure()
  assert.equal(result.current.overview, snapshot)
})

test('cleanup uses its returned reading and clears an earlier measurement error', async t => {
  const { result } = install(t)
  api.resources.overview = async () => { throw new Error('Unavailable') }
  await result.current.measure()
  api.resources.runCleanup = async () => ({ cleanup: { bytes: 684 }, resources: snapshot })
  const outcome = await result.current.clear('agent_sessions')
  assert.equal(outcome.cleanup.bytes, 684)
  assert.equal(result.current.overview, snapshot)
  assert.equal(result.current.error, '')
})

test('a failed Stop preserves its exact reason and does not report or reload success', async t => {
  const { result } = install(t)
  let reads = 0
  api.resources.overview = async () => { reads += 1; return snapshot }
  api.chats.stop = async () => new Response(JSON.stringify({ detail: 'Cannot stop this turn now' }), { status: 500 })
  await assert.rejects(result.current.stopTurn('example'), /Cannot stop this turn now/)
  assert.equal(reads, 0)
})

test('Settings asks the server to refuse when typed messages are queued, then rereads', async t => {
  const { result } = install(t)
  let reads = 0
  let body = null
  api.resources.overview = async () => { reads += 1; return snapshot }
  api.chats.stop = async (_chatId, options) => {
    body = options
    return new Response(JSON.stringify({ detail: 'This chat has queued messages. Stop it from the chat so they are kept.' }), { status: 409 })
  }
  await assert.rejects(result.current.stopTurn('example'), /Stop it from the chat/)
  assert.deepEqual(body, { refuseIfQueued: true })
  assert.equal(reads, 1)
})

test('successful Stop followed by a failed read leaves a visible stale-reading error', async t => {
  const { result } = install(t)
  await result.current.measure()
  api.resources.overview = async () => { throw new Error('Memory reading failed') }
  await result.current.stopTurn('example')
  assert.equal(result.current.overview, snapshot)
  assert.equal(result.current.error, 'Memory reading failed')
})

test('a Stop that timed out is a failure to retry, never reported as stopped', async t => {
  const { result } = install(t)
  let reads = 0
  api.resources.overview = async () => { reads += 1; return snapshot }
  api.chats.stop = async () => new Response('{"stopped": false, "cleared_pending_cids": []}', { headers: { 'Content-Type': 'application/json' } })
  await assert.rejects(result.current.stopTurn('example'), /did not stop in time/)
  assert.equal(reads, 0)
})
