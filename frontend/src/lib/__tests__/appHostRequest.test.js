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

test('project host requests retain only the correlated, bounded contract', () => {
  assert.deepEqual(appHostRequest({
    type: 'moebius:projects',
    requestId: 'projects:abc:1',
    action: 'create',
    templateId: 'webstudio:web-app',
    name: '  My site  ',
    ownerToken: 'nope',
  }), {
    type: 'moebius:projects',
    requestId: 'projects:abc:1',
    action: 'create',
    projectId: '',
    templateId: 'webstudio:web-app',
    name: 'My site',
  })
  assert.equal(appHostRequest({
    type: 'moebius:projects', requestId: 'bad', action: 'list',
  }), null)
  assert.equal(appHostRequest({
    type: 'moebius:projects', requestId: 'projects:abc:2', action: 'delete',
  }), null)
  assert.equal(appHostRequest({
    type: 'moebius:projects', requestId: 'projects:abc:3', action: 'migrate',
  })?.action, 'migrate')
})

test('chat controls retain only a correlated status or stop request', () => {
  assert.deepEqual(appHostRequest({
    type: 'moebius:chat-control',
    requestId: 'chat-control:abc:1',
    action: 'stop',
    chatId: ' chat-123 ',
    ownerToken: 'nope',
  }), {
    type: 'moebius:chat-control',
    requestId: 'chat-control:abc:1',
    action: 'stop',
    chatId: 'chat-123',
  })
  assert.equal(appHostRequest({
    type: 'moebius:chat-control', requestId: 'bad', action: 'status', chatId: '1',
  }), null)
  assert.equal(appHostRequest({
    type: 'moebius:chat-control', requestId: 'chat-control:abc:2', action: 'delete', chatId: '1',
  }), null)
})
