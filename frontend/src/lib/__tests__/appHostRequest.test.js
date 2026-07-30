import test from 'node:test'
import assert from 'node:assert/strict'

import { appHostRequest } from '../appHostRequest.js'

test('app host requests expose only the reviewed navigation contract', () => {
  assert.deepEqual(appHostRequest({
    type: 'moebius:new-chat', draft: 'hello', autoSend: 1, secret: 'drop-me',
  }), {
    type: 'moebius:new-chat', draft: 'hello', autoSend: false,
  })
  assert.deepEqual(appHostRequest({
    type: 'moebius:open-app', appId: 'atlas', intent: 'setup', extra: true,
  }), {
    type: 'moebius:open-app', appId: 'atlas', intent: 'setup',
  })
  assert.equal(appHostRequest({ type: 'moebius:open-chat', chatId: '' }), null)
  assert.equal(appHostRequest({ type: 'unexpected', appId: 1 }), null)
})
