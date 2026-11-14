import test from 'node:test'
import assert from 'node:assert/strict'

import {
  detachedSendFailureMessage,
  isAmbiguousSendFailure,
  isHelperChatSendFailure,
  isModelSelectionRequiredFailure,
  isPendingQuestionSendFailure,
  queueSendFailureMessage,
  sendFailureMessage,
  shouldKeepQueuedAfterSendFailure,
} from '../sendFailure.js'
import {
  ChatHttpError,
  ChatTransportError,
  chatHttpError,
  isQuestionStateChangedError,
} from '../sendErrors.js'
import { renderHook } from '../hooks/__tests__/react-hook-shim.mjs'
import useComposerDraftState from '../hooks/useComposerDraftState.js'
import { loadFailedSendAttempt, saveFailedSendAttempt } from '../sendAttemptRecovery.js'

test('settled question errors trigger authoritative card reconciliation', () => {
  assert.equal(isQuestionStateChangedError({ status: 410 }), true)
  assert.equal(isQuestionStateChangedError({ code: 'question_state_changed' }), true)
  assert.equal(isQuestionStateChangedError({
    status: 409,
    detail: 'This Restart card is stale or no longer authorized.',
  }), true)
  assert.equal(isQuestionStateChangedError({
    status: 409,
    detail: 'This Restart card is stale or no longer accepting a response.',
  }), true)
  assert.equal(isQuestionStateChangedError({
    status: 409,
    detail: 'This Restart card is stale or no longer ready; please retry.',
  }), false)
  assert.equal(isQuestionStateChangedError({
    status: 409,
    detail: "Use one of the Restart card's current buttons.",
  }), false)
})

test('send failures distinguish connection, timeout, service, and generic errors', () => {
  assert.match(
    sendFailureMessage(new ChatTransportError(new TypeError('Failed to fetch'))),
    /Checking whether that message reached the chat/,
  )

  const timeout = new Error('aborted')
  timeout.name = 'AbortError'
  assert.match(sendFailureMessage(timeout), /Checking whether that message reached the chat/)

  const unavailable = new Error('HTTP 503')
  unavailable.status = 503
  assert.match(sendFailureMessage(unavailable), /can’t save messages right now/)

  assert.match(sendFailureMessage(new Error('HTTP 400')), /couldn’t send the message/)
  assert.match(sendFailureMessage({ status: 429 }), /too many requests/)
  assert.match(sendFailureMessage({ status: 401 }), /sign in again/)
})

test('only transport and timeout failures need an authoritative transcript check', () => {
  assert.equal(
    isAmbiguousSendFailure(new ChatTransportError(new TypeError('Failed to fetch'))),
    true,
  )
  const timeout = new Error('aborted')
  timeout.name = 'AbortError'
  assert.equal(isAmbiguousSendFailure(timeout), true)
  assert.equal(isAmbiguousSendFailure({ status: 503 }), false)
})

test('an unrelated programming TypeError is not mislabeled as offline', () => {
  assert.doesNotMatch(
    sendFailureMessage(new TypeError('cannot read property')),
    /check your connection/,
  )
})

test('known offline state wins over the transport error shape', () => {
  assert.match(
    sendFailureMessage(new Error('anything'), { online: false }),
    /You’re offline/,
  )
  assert.match(
    sendFailureMessage({ outboxRetained: true }, { online: false }),
    /queued and will send when you reconnect/,
  )
  assert.match(
    sendFailureMessage(new Error('anything'), { online: false }),
    /back in the composer/,
  )
})

test('the send attempt verdict outranks a connection snapshot that changed later', () => {
  const offline = new ChatTransportError(new TypeError('Failed to fetch'))
  offline.outboxRetained = true
  offline.sendReachability = 'offline'
  assert.equal(
    sendFailureMessage(offline, { online: true }),
    'You’re offline. Your message is queued and will send when you reconnect.',
  )

  const online = new ChatTransportError(new TypeError('Failed to fetch'))
  online.outboxRetained = true
  online.sendReachability = 'online'
  assert.equal(
    sendFailureMessage(online, { online: false }),
    'Möbius couldn’t confirm the send. Your message is queued and will retry automatically.',
  )
})

test('automatic replay is promised only when the durable write succeeded', () => {
  const transport = new ChatTransportError(new TypeError('Failed to fetch'))
  transport.outboxRetained = true
  assert.match(sendFailureMessage(transport), /queued and will retry automatically/)

  const unavailable = new Error('HTTP 503')
  unavailable.status = 503
  unavailable.outboxRetained = true
  assert.match(sendFailureMessage(unavailable), /queued and will retry automatically/)

  assert.match(
    sendFailureMessage({ status: 401, outboxRetained: true }),
    /queued for this owner and will resume afterward/,
  )
})

test('only an outbox-retained failure stays with the durable queue', () => {
  assert.equal(shouldKeepQueuedAfterSendFailure({ outboxRetained: true }), true)
  // A restart gap or offline state without a retained write has nothing that
  // will replay the message, so it must return to the composer.
  assert.equal(shouldKeepQueuedAfterSendFailure({}), false)
  assert.equal(shouldKeepQueuedAfterSendFailure({ outboxRetained: false }), false)
  const retained = Object.assign(new Error('restart gap'), { outboxRetained: true })
  assert.match(
    sendFailureMessage(retained),
    /queued and will retry automatically/,
  )
  assert.match(
    sendFailureMessage(new Error('restart gap')),
    /back in the composer/,
  )
})

test('HTTP failures retain a safe server detail for diagnostics', async () => {
  const error = await chatHttpError({
    status: 503,
    async json() {
      return { detail: { code: 'writer_unavailable', message: 'writer unavailable' } }
    },
  })
  assert.equal(error instanceof ChatHttpError, true)
  assert.equal(error.status, 503)
  assert.equal(error.code, 'writer_unavailable')
  assert.equal(error.detail, 'writer unavailable')
})

test('a pending-question conflict explains the recovery instead of reporting a generic failure', () => {
  const error = new ChatHttpError(409, { code: 'pending_question_open' })

  assert.equal(isPendingQuestionSendFailure(error), true)
  assert.match(sendFailureMessage(error), /Answer the pending question above/)
  assert.match(sendFailureMessage(error), /safe in the composer/)
  assert.equal(isPendingQuestionSendFailure(new ChatHttpError(409, {
    code: 'another_conflict',
  })), false)
})

test('a missing model is a distinct recoverable send conflict', () => {
  const error = new ChatHttpError(409, { code: 'model_selection_required' })

  assert.equal(isModelSelectionRequiredFailure(error), true)
  assert.match(sendFailureMessage(error), /Choose a model before sending/)
  assert.match(sendFailureMessage(error), /safe in the composer/)
  assert.equal(isModelSelectionRequiredFailure(new ChatHttpError(409, {
    code: 'another_conflict',
  })), false)
})

test('a send that never took the composer draft never says its message is in the composer', () => {
  const failures = [
    new ChatTransportError(new TypeError('Failed to fetch')),
    Object.assign(new Error('aborted'), { name: 'AbortError' }),
    { status: 400 }, { status: 401 }, { status: 429 }, { status: 503 },
    { status: 409, code: 'pending_question_open' },
  ]
  for (const failure of failures) {
    for (const online of [true, false]) {
      assert.doesNotMatch(detachedSendFailureMessage(failure, { online }), /composer/)
    }
  }
  assert.equal(detachedSendFailureMessage({ status: 400 }, { online: false }),
    'You’re offline. Try again when you reconnect.')
  assert.equal(detachedSendFailureMessage(failures[0]),
    'Möbius couldn’t confirm the send. It’s safe to try again.')
  assert.equal(detachedSendFailureMessage({ status: 400 }),
    'Möbius couldn’t send the message. Try again in a moment.')
  // A durable outbox write owns the replay either way, so it says so.
  const retained = Object.assign(new ChatTransportError(new TypeError('Failed to fetch')), {
    outboxRetained: true,
  })
  assert.equal(detachedSendFailureMessage(retained), sendFailureMessage(retained))
})

test('a refused empty Send says the queue is unchanged, never that a draft came back', () => {
  for (const error of [
    new ChatHttpError(409, { code: 'pending_question_open' }),
    new ChatHttpError(409, { code: 'model_selection_required' }),
    new ChatTransportError(new Error('offline')),
  ]) {
    const message = queueSendFailureMessage(error)
    assert.match(message, /queued messages are unchanged|still queued/)
    assert.doesNotMatch(message, /composer/)
  }
  assert.match(
    queueSendFailureMessage(new ChatHttpError(409, { code: 'pending_question_open' })),
    /pending question/,
  )
})

test('a refused empty Send shows the server\'s reason instead of inviting a retry', () => {
  const message = queueSendFailureMessage(new ChatHttpError(409, {
    code: 'goal_clear_retired',
    detail: 'Clear the Goal from its confirmed × control instead.',
  }))
  assert.match(message, /confirmed × control/)
  assert.match(message, /unchanged/)
  assert.doesNotMatch(message, /try again/i)
})

test('a helper chat refusal says whose chat it is and never invites a retry', async () => {
  const response = new Response(JSON.stringify({ detail: {
    code: 'delegation_managed',
    message: "This is a helper's chat. Message it from its parent chat.",
    parent_chat_id: 'parent-chat',
  } }), { status: 409 })
  const error = await chatHttpError(response)
  assert.equal(isHelperChatSendFailure(error), true)
  assert.equal(error.parentChatId, 'parent-chat')
  for (const message of [
    sendFailureMessage(error),
    detachedSendFailureMessage(error),
    queueSendFailureMessage(error),
  ]) {
    assert.match(message, /^This is a helper’s chat\. Message it from its parent chat\./)
    assert.doesNotMatch(message, /try again|send it again|evaluator|workflow/i)
  }
  assert.match(queueSendFailureMessage(error), /queued messages are unchanged/)
  // Other conflicts keep their own handling.
  assert.equal(isHelperChatSendFailure(new ChatHttpError(409, { code: 'pending_question_open' })), false)
  assert.equal(new ChatHttpError(409).parentChatId, null)
})

test('a helper chat refusal settles an earlier lost send so reconciliation keeps the notice', () => {
  const previousStorage = globalThis.sessionStorage
  const previousFrame = globalThis.requestAnimationFrame
  globalThis.requestAnimationFrame = () => 0
  const values = new Map()
  globalThis.sessionStorage = {
    get length() { return values.size },
    key(index) { return [...values.keys()][index] ?? null },
    getItem(key) { return values.has(key) ? values.get(key) : null },
    setItem(key, value) { values.set(key, String(value)) },
    removeItem(key) { values.delete(key) },
  }
  try {
    // An earlier send of this draft was lost, or an older build saved it.
    saveFailedSendAttempt('helper-chat', {
      cid: 'cid-lost', draftIdentity: 'draft-lost', text: 'hello helper', attachments: [],
    })
    const hook = renderHook(() => useComposerDraftState({
      chatId: 'helper-chat', hidden: false, inputRef: { current: null },
    }))
    assert.equal(hook.result.current.failedSendAttemptRef.current?.cid, 'cid-lost')

    // ChatView's keepFailedDraft for the unedited resend the server refused.
    const error = new ChatHttpError(409, { code: 'delegation_managed', parentChatId: 'parent' })
    assert.equal(isHelperChatSendFailure(error), true)
    hook.result.current.clearFailedAttempt()
    hook.result.current.restoreComposerText('hello helper', { preserveFailedAttempt: true })
    hook.result.current.setSendFailure(sendFailureMessage(error))

    assert.equal(hook.result.current.failedSendAttemptRef.current, null)
    assert.equal(loadFailedSendAttempt('helper-chat'), null)
    // A later authoritative transcript read has nothing to report missing.
    assert.equal(hook.result.current.reconcileFailedAttempt([], [], { reportMissing: true }), 'none')
    assert.match(hook.result.current.sendFailure, /^This is a helper’s chat\./)
    assert.equal(hook.result.current.input, 'hello helper')
    hook.unmount()
  } finally {
    if (previousStorage === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previousStorage
    globalThis.requestAnimationFrame = previousFrame
  }
})
