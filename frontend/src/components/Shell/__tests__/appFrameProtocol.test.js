import assert from 'node:assert/strict'
import test from 'node:test'

import {
  appMediaSessionEvent,
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
  let fetches = 0
  const accepted = serveModuleRequest({
    message: { type: 'moebius:module-request', requestId: 'r1', appId: 8 },
    source: { postMessage: (...args) => posts.push(args) },
    appId: 7,
    frameVersion: 'v1',
    token: 'secret',
    moduleUrl: '/api/apps/7/module',
    fetchModule: async () => { fetches += 1 },
  })
  assert.equal(accepted, false)
  assert.deepEqual(posts, [])
  assert.equal(fetches, 0)
})


test('an attributed module request acknowledges before its transfer settles', async () => {
  const posts = []
  let resolveTransfer
  const transfer = new Promise(resolve => { resolveTransfer = resolve })
  const source = { postMessage: (...args) => posts.push(args) }

  const accepted = serveModuleRequest({
    message: { type: 'moebius:module-request', requestId: 'r1', appId: 7 },
    source,
    appId: 7,
    frameVersion: 'v1',
    token: 'secret',
    moduleUrl: '/api/apps/7/module',
    fetchModule: () => transfer,
  })

  assert.equal(accepted, true)
  assert.deepEqual(posts, [[{
    type: 'moebius:module-ack', requestId: 'r1', appId: 7,
  }, '*']])

  const bytes = new Uint8Array([1, 2, 3])
  resolveTransfer(bytes)
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(posts.length, 2)
  assert.deepEqual(posts[1][0], {
    type: 'moebius:module-result', requestId: 'r1', appId: 7, ok: true, bytes,
  })
  assert.deepEqual(posts[1].slice(1), ['*', [bytes]])
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


test('media-session messages are bounded to the drawer contract', () => {
  assert.deepEqual(appMediaSessionEvent({
    type: 'moebius:media-session', event: 'open', sessionId: 'digest-1',
    title: ' Daily digest ', subtitle: ' Untrusted app label ', playbackState: 'playing',
  }), {
    event: 'open', sessionId: 'digest-1', title: 'Daily digest',
    playbackState: 'playing',
  })
  assert.deepEqual(appMediaSessionEvent({
    type: 'moebius:media-session', event: 'close', sessionId: 'digest-1',
  }), { event: 'close', sessionId: 'digest-1' })
  assert.equal(appMediaSessionEvent({
    type: 'moebius:media-session', event: 'update', sessionId: '',
  }), null)
  assert.equal(appMediaSessionEvent({
    type: 'moebius:media-session', event: 'open', sessionId: ' padded ',
  }), null)
  assert.equal(appMediaSessionEvent({
    type: 'moebius:media-session', event: 'open', sessionId: 'x'.repeat(161),
  }), null)
  assert.equal(appMediaSessionEvent({ type: 'moebius:other' }), null)
})
