import assert from 'node:assert/strict'
import test from 'node:test'

import {
  applyVirtualStorageMutation,
  attributedFrameVersion,
  serveModuleRequest,
  serveStorageRpc,
} from '../../AppCanvas/appFrameProtocol.js'


test('frame attribution derives identity only from the mounted source window', () => {
  const live = {}
  const hidden = {}
  const frames = new Map([
    ['v1', { contentWindow: live }],
    ['v2', { contentWindow: hidden }],
  ])
  assert.equal(attributedFrameVersion(frames, hidden), 'v2')
  assert.equal(attributedFrameVersion(frames, {}), null)
})


test('module requests reject unbound identity before acknowledging or fetching', () => {
  const posts = []
  const accepted = serveModuleRequest({
    message: { type: 'moebius:module-request', requestId: 'r1', appId: 8 },
    source: { postMessage: (...args) => posts.push(args) },
    appId: 7,
    frameVersion: 'v1',
    token: 'secret',
    moduleUrl: '/api/apps/7/module',
  })
  assert.equal(accepted, false)
  assert.deepEqual(posts, [])
})


test('storage RPC narrows arguments and returns one correlated result', async () => {
  const posts = []
  const source = { postMessage: message => posts.push(message) }
  const calls = []
  const accepted = serveStorageRpc({
    message: { requestId: 'rpc-1', method: 'get', args: 'not-an-array' },
    source,
    host: { handleRpc: async (...args) => { calls.push(args); return 42 } },
  })
  assert.equal(accepted, true)
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.deepEqual(calls, [[source, 'get', []]])
  assert.deepEqual(posts, [{
    type: 'moebius:storage-rpc-result', requestId: 'rpc-1', ok: true, result: 42,
  }])
})


test('unknown virtual-storage messages remain outside the storage owner', () => {
  assert.equal(applyVirtualStorageMutation(7, { type: 'other' }, () => {}), false)
})
