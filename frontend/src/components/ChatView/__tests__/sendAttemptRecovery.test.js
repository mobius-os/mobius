import test from 'node:test'
import assert from 'node:assert/strict'

import {
  SEND_ATTEMPT_MISSING_MESSAGE,
  SEND_ATTEMPT_UNCONFIRMED_MESSAGE,
  clearFailedSendAttempt,
  failedSendReconciliation,
  loadFailedSendAttempt,
  saveFailedSendAttempt,
  sameSendAttempt,
  sendAttemptIsDurable,
  settleFailedSendConfirmation,
} from '../sendAttemptRecovery.js'

function storageStub() {
  const values = new Map()
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  }
}

test('failed send identity and uploaded attachment metadata survive reload', () => {
  const previous = globalThis.sessionStorage
  globalThis.sessionStorage = storageStub()
  try {
    saveFailedSendAttempt('chat-1', {
      cid: 'cid-1',
      draftIdentity: 'draft-1',
      text: 'hello',
      attachments: [{
        id: 'file-1', name: 'note.txt', size: 12, mime_type: 'text/plain',
        status: 'done', objectUrl: 'blob:temporary',
      }],
    })

    assert.deepEqual(loadFailedSendAttempt('chat-1'), {
      cid: 'cid-1',
      draftIdentity: 'draft-1',
      text: 'hello',
      attachments: [{
        id: 'file-1', name: 'note.txt', size: 12, mime_type: 'text/plain',
        status: 'done', error: null, objectUrl: null,
      }],
    })
  } finally {
    if (previous === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previous
  }
})

test('authoritative transcript or pending queue settles an ambiguous send', () => {
  const attempt = { cid: 'cid-1' }
  assert.equal(sendAttemptIsDurable(attempt, [
    { role: 'assistant', cid: 'cid-1' },
    { role: 'user', cid: 'other' },
  ], []), false)
  assert.equal(sendAttemptIsDurable(attempt, [
    { role: 'user', cid: 'cid-1' },
  ], []), true)
  assert.equal(sendAttemptIsDurable(attempt, [], [
    { role: 'user', cid: 'cid-1' },
  ]), true)
})

test('confirmation ownership requires the same cid and draft identity', () => {
  const first = { cid: 'cid-1', draftIdentity: 'draft-1' }
  assert.equal(sameSendAttempt(first, { ...first }), true)
  assert.equal(sameSendAttempt(first, { ...first, cid: 'cid-2' }), false)
  assert.equal(sameSendAttempt(first, { ...first, draftIdentity: 'draft-2' }), false)
})

test('a stale confirmation cannot settle a newer failed send', () => {
  assert.deepEqual(
    failedSendReconciliation(
      { cid: 'cid-new', draftIdentity: 'draft-new' },
      [],
      [],
      {
        reportUnavailable: true,
        expectedAttempt: { cid: 'cid-old', draftIdentity: 'draft-old' },
      },
    ),
    { status: 'superseded' },
  )
  assert.deepEqual(
    failedSendReconciliation(
      { cid: 'cid-new', draftIdentity: 'draft-new' },
      [],
      [],
      { reportMissing: true, expectedAttempt: null },
    ),
    { status: 'superseded' },
    'a continuation confirmation launched with no draft cannot touch a later send',
  )
})

test('a later visible transcript update retires the restored ambiguous draft', () => {
  const attempt = { cid: 'cid-1' }

  assert.deepEqual(
    failedSendReconciliation(attempt, [], []),
    { status: 'missing' },
    'an unconfirmed send keeps its restored draft while server truth is pending',
  )
  assert.deepEqual(
    failedSendReconciliation(attempt, [{ role: 'user', cid: 'cid-1' }], []),
    { status: 'durable', sendFailure: null },
    'the same cid arriving later clears the duplicate draft and warning together',
  )
})

test('a confirmed missing send keeps the draft and offers a safe retry', () => {
  assert.deepEqual(
    failedSendReconciliation(
      { cid: 'cid-1' },
      [{ role: 'user', cid: 'other' }],
      [],
      { reportMissing: true },
    ),
    {
      status: 'missing',
      sendFailure: 'That message didn’t reach the chat. It’s safe here—send it again when ready.',
    },
  )
})

test('an unavailable confirmation settles without claiming the send is missing', async () => {
  const reconciliations = []
  const result = await settleFailedSendConfirmation(
    async () => null,
    options => {
      reconciliations.push(options)
      return failedSendReconciliation(
        { cid: 'cid-1' },
        [],
        [],
        options,
      )
    },
  )

  assert.deepEqual(reconciliations, [{ reportUnavailable: true }])
  assert.deepEqual(result, {
    status: 'unconfirmed',
    sendFailure: SEND_ATTEMPT_UNCONFIRMED_MESSAGE,
  })
})

test('a rejected confirmation reaches the same bounded settlement', async () => {
  const result = await settleFailedSendConfirmation(
    async () => { throw new Error('confirmation unavailable') },
    options => failedSendReconciliation(
      { cid: 'cid-1' },
      [],
      [],
      options,
    ),
  )

  assert.equal(result.status, 'unconfirmed')
  assert.equal(result.sendFailure, SEND_ATTEMPT_UNCONFIRMED_MESSAGE)
})

test('a successful confirmation leaves the authoritative refresh in control', async () => {
  const confirmation = { running: true }
  let missingReconciliations = 0

  const result = await settleFailedSendConfirmation(
    async () => confirmation,
    () => { missingReconciliations += 1 },
  )

  assert.equal(result, confirmation)
  assert.equal(missingReconciliations, 0)
})

test('a continuation confirmation retires its provisional state when unavailable', async () => {
  let settled = 0
  const result = await settleFailedSendConfirmation(
    async () => null,
    options => failedSendReconciliation(
      null,
      [],
      [],
      { ...options, expectedAttempt: null },
    ),
    () => { settled += 1 },
  )

  assert.deepEqual(result, { status: 'none' })
  assert.equal(settled, 1)
})

test('a continuation confirmation retires its provisional state after success', async () => {
  let settled = 0
  const confirmation = { running: true }
  const result = await settleFailedSendConfirmation(
    async () => confirmation,
    () => assert.fail('successful confirmation must not use the fallback'),
    () => { settled += 1 },
  )

  assert.equal(result, confirmation)
  assert.equal(settled, 1)
})

test('clearing a failed attempt prevents stale cid reuse', () => {
  const previous = globalThis.sessionStorage
  globalThis.sessionStorage = storageStub()
  try {
    saveFailedSendAttempt('chat-1', {
      cid: 'cid-1', draftIdentity: 'draft-1', text: 'hello', attachments: [],
    })
    clearFailedSendAttempt('chat-1')
    assert.equal(loadFailedSendAttempt('chat-1'), null)
  } finally {
    if (previous === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previous
  }
})
