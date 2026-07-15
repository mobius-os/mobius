import test from 'node:test'
import assert from 'node:assert/strict'

import {
  cidForSendAttempt,
  sendDraftIdentity,
} from '../sendAttemptIdentity.js'

test('an unchanged restored draft reuses its failed send cid', () => {
  const draftIdentity = sendDraftIdentity('chat-1', 'hello', [
    { name: 'note.txt', size: 12, mime_type: 'text/plain' },
  ])
  const cid = cidForSendAttempt({
    failedAttempt: { cid: 'cid-original', draftIdentity },
    draftIdentity,
    mintCid: () => 'cid-new',
  })
  assert.equal(cid, 'cid-original')
})

test('editing text or attachments creates a new send identity', () => {
  const prior = sendDraftIdentity('chat-1', 'hello', [
    { name: 'note.txt', size: 12, mime_type: 'text/plain' },
  ])
  for (const next of [
    sendDraftIdentity('chat-1', 'hello again', [
      { name: 'note.txt', size: 12, mime_type: 'text/plain' },
    ]),
    sendDraftIdentity('chat-1', 'hello', [
      { name: 'note.txt', size: 13, mime_type: 'text/plain' },
    ]),
  ]) {
    assert.equal(cidForSendAttempt({
      failedAttempt: { cid: 'cid-original', draftIdentity: prior },
      draftIdentity: next,
      mintCid: () => 'cid-new',
    }), 'cid-new')
  }
})

test('the same restored draft in another chat gets a new cid', () => {
  const prior = sendDraftIdentity('chat-1', 'hello', [])
  const next = sendDraftIdentity('chat-2', 'hello', [])

  assert.equal(cidForSendAttempt({
    failedAttempt: { cid: 'cid-original', draftIdentity: prior },
    draftIdentity: next,
    mintCid: () => 'cid-new',
  }), 'cid-new')
})
