import test from 'node:test'
import assert from 'node:assert/strict'

import {
  SEND_ATTEMPT_MISSING_MESSAGE,
  SEND_ATTEMPT_QUEUED_MESSAGE,
  SEND_ATTEMPT_UNCONFIRMED_MESSAGE,
  clearFailedSendAttempt,
  coordinateFailedSendRecovery,
  failedSendReconciliation,
  failedSendOutboxReport,
  loadFailedSendAttempt,
  saveFailedSendAttempt,
  sameSendAttempt,
  sendAttemptIsDurable,
  settleFailedSendConfirmation,
} from '../sendAttemptRecovery.js'

function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

function recoveryOwner(attempt, overrides = {}) {
  return {
    chatId: 'chat-1',
    chatStale: false,
    generation: 4,
    inspection: 9,
    attempt,
    visibleMessages: [],
    pendingMessages: [],
    terminal: null,
    ...overrides,
  }
}

function coordinate(attempt, inspectIntent, readCurrent, overrides = {}) {
  return coordinateFailedSendRecovery({
    expectedAttempt: attempt,
    expectedChatId: 'chat-1',
    expectedGeneration: 4,
    expectedInspection: 9,
    visibleMessages: [],
    pendingMessages: [],
    inspectIntent,
    readCurrent,
    ...overrides,
  })
}

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
      transportContent: 'hello\n\n<app_state>\n  <selection>row 4</selection>\n</app_state>',
      attachments: [{
        id: 'file-1', name: 'note.txt', size: 12, mime_type: 'text/plain',
        status: 'done', objectUrl: 'blob:temporary',
      }],
    })

    assert.deepEqual(loadFailedSendAttempt('chat-1'), {
      cid: 'cid-1',
      draftIdentity: 'draft-1',
      text: 'hello',
      transportContent: 'hello\n\n<app_state>\n  <selection>row 4</selection>\n</app_state>',
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

test('confirmation ownership requires the same cid, draft, and transport content', () => {
  const first = {
    cid: 'cid-1', draftIdentity: 'draft-1', transportContent: 'content-1',
  }
  assert.equal(sameSendAttempt(first, { ...first }), true)
  assert.equal(sameSendAttempt(first, { ...first, cid: 'cid-2' }), false)
  assert.equal(sameSendAttempt(first, { ...first, draftIdentity: 'draft-2' }), false)
  assert.equal(sameSendAttempt(first, { ...first, transportContent: 'content-2' }), false)
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

test('async inspection cannot settle a newer attempt, chat, generation, or inspection owner', async () => {
  const expected = { cid: 'cid-old', draftIdentity: 'draft-old' }
  const staleOwners = [
    recoveryOwner({ cid: 'cid-new', draftIdentity: 'draft-new' }),
    recoveryOwner(expected, { chatId: 'chat-2' }),
    recoveryOwner(expected, { generation: 5 }),
    recoveryOwner(expected, { inspection: 10 }),
  ]

  for (const owner of staleOwners) {
    const inspection = deferred()
    let current = recoveryOwner(expected)
    const result = coordinate(
      expected,
      () => inspection.promise,
      () => current,
      { authoritative: true },
    )
    current = owner
    inspection.resolve('absent')
    assert.deepEqual(await result, { status: 'superseded' })
  }
})

test('transcript or pending evidence arriving during inspection outranks retained intent', async () => {
  const attempt = { cid: 'cid-1', draftIdentity: 'draft-1' }
  for (const evidence of [
    { visibleMessages: [{ role: 'user', cid: 'cid-1' }] },
    { pendingMessages: [{ role: 'user', cid: 'cid-1' }] },
  ]) {
    const inspection = deferred()
    let current = recoveryOwner(attempt)
    const result = coordinate(
      attempt,
      () => inspection.promise,
      () => current,
      { authoritative: true },
    )
    current = recoveryOwner(attempt, evidence)
    inspection.resolve('retained')
    assert.deepEqual(await result, { status: 'durable', sendFailure: null })
  }
})

test('async recovery reports terminal, delivered-not-visible, and unavailable states honestly', async () => {
  const attempt = { cid: 'cid-1', draftIdentity: 'draft-1' }
  const cases = [
    {
      intentStatus: 'absent',
      options: { authoritative: true, terminalOutcome: 'failed' },
      expected: { status: 'missing', sendFailure: SEND_ATTEMPT_MISSING_MESSAGE },
    },
    {
      intentStatus: 'absent',
      options: { authoritative: true, terminalOutcome: 'delivered' },
      expected: { status: 'unconfirmed', sendFailure: SEND_ATTEMPT_UNCONFIRMED_MESSAGE },
    },
    {
      intentStatus: 'unknown',
      options: { authoritative: true, terminalOutcome: 'failed' },
      expected: { status: 'unconfirmed', sendFailure: SEND_ATTEMPT_UNCONFIRMED_MESSAGE },
    },
    {
      intentStatus: 'absent',
      options: { authoritative: false },
      expected: { status: 'unconfirmed', sendFailure: SEND_ATTEMPT_UNCONFIRMED_MESSAGE },
    },
    {
      intentStatus: 'retained',
      options: { authoritative: true },
      expected: { status: 'queued', sendFailure: SEND_ATTEMPT_QUEUED_MESSAGE },
    },
  ]

  for (const fixture of cases) {
    assert.deepEqual(await coordinate(
      attempt,
      async () => fixture.intentStatus,
      () => recoveryOwner(attempt),
      fixture.options,
    ), fixture.expected)
  }
})

test('manual continuation without a failed visible attempt remains outside recovery ownership', async () => {
  let inspected = false
  const result = await coordinateFailedSendRecovery({
    expectedAttempt: null,
    expectedChatId: 'chat-1',
    expectedGeneration: 4,
    expectedInspection: 9,
    inspectIntent: async () => { inspected = true; return 'retained' },
    readCurrent: () => recoveryOwner({ cid: 'new', draftIdentity: 'new' }),
  })
  assert.deepEqual(result, { status: 'none' })
  assert.equal(inspected, false)
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

test('a retained reload keeps automatic replay ownership while preserving the draft', () => {
  assert.equal(failedSendOutboxReport({
    intentStatus: 'retained',
    authoritative: true,
  }), 'queued')
  assert.deepEqual(
    failedSendReconciliation(
      { cid: 'cid-1' },
      [],
      [],
      { reportQueued: true },
    ),
    { status: 'queued', sendFailure: SEND_ATTEMPT_QUEUED_MESSAGE },
  )
})

test('terminal and unavailable intent states never overclaim delivery', () => {
  assert.equal(failedSendOutboxReport({
    intentStatus: 'absent', terminalOutcome: 'failed',
  }), 'missing')
  assert.equal(failedSendOutboxReport({
    intentStatus: 'absent', terminalOutcome: 'delivered', authoritative: true,
  }), 'unconfirmed')
  assert.equal(failedSendOutboxReport({
    intentStatus: 'unknown', terminalOutcome: 'failed', authoritative: true,
  }), 'unconfirmed')
  assert.equal(failedSendOutboxReport({
    intentStatus: 'absent', authoritative: false,
  }), 'unconfirmed')
  assert.equal(failedSendOutboxReport({
    intentStatus: 'absent', authoritative: true,
  }), 'missing')
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
