import test from 'node:test'
import assert from 'node:assert/strict'

import { serveClipboardWrite } from '../../components/AppCanvas/appFrameProtocol.js'

test('clipboard host returns only the attributed write outcome', async () => {
  const replies = []
  const source = { postMessage: (message) => replies.push(message) }
  assert.equal(serveClipboardWrite({
    message: {
      type: 'moebius:clipboard-write',
      requestId: 'copy-1',
      text: 'one command',
    },
    source,
    writeText: async (text) => text === 'one command',
  }), true)
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.deepEqual(replies, [{
    type: 'moebius:clipboard-write-result', requestId: 'copy-1', ok: true,
  }])
})

test('clipboard host ignores unrelated messages', () => {
  assert.equal(serveClipboardWrite({
    message: { type: 'something-else' },
    source: null,
    writeText: async () => true,
  }), false)
})
