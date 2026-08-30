import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  answerKeepsCurrentTurn,
  answerTurnDisposition,
  canFastForwardQueue,
  shouldFreezeStreamingReturn,
  coldTranscriptRenderFrames,
  continuationRowsFromPromotedMessage,
  isContinuationMessage,
  isOwnerUserMessage,
  startsFollowingTurn,
  jumpToLatestShown,
  runtimeStreamAttachAction,
  serverSnapshotBehindLocal,
  shouldAttachRunningStream,
  shouldAdoptRuntimeAssistantOwner,
  shouldRetireRestoredQuestionSnapshot,
  shouldRecoverSettledRuntime,
  shouldRetryStopAfterConfirm,
  startedMessagesFromResponse,
  stopConfirmedIdle,
  stopRequestSucceeded,
  stripInternalUserMessageFields,
  systemEventForChat,
} from '../chatRuntimeState.js'
import { mergeRecentMessagesIntoLoadedWindow } from '../../../lib/chatDetailCache.js'

test('R5a: jump-to-latest shows only away from the physical tail and yields to attention nudges', () => {
  // At the physical tail there is nothing to jump to.
  assert.equal(jumpToLatestShown({ awayFromTail: false }), false)
  // Scrolled up with no competing nudge: show.
  assert.equal(jumpToLatestShown({ awayFromTail: true }), true)
  // A visible attention nudge navigates to the same tail with more context —
  // never stack two controls for one action.
  assert.equal(
    jumpToLatestShown({ awayFromTail: true, questionNudgeShown: true }),
    false,
  )
  assert.equal(
    jumpToLatestShown({ awayFromTail: true, resumeNudgeShown: true }),
    false,
  )
  // Fails closed on an empty call.
  assert.equal(jumpToLatestShown(), false)
})

test('automatic and manual continuations are product markers, not owner messages', () => {
  const marker = {
    role: 'user',
    content: 'continue',
    kind: 'continuation',
    continuation_reason: 'manual',
  }
  assert.equal(isContinuationMessage(marker), true)
  assert.equal(isContinuationMessage({ ...marker, kind: 'auto_continuation' }), true)
  assert.equal(isOwnerUserMessage(marker), false)
  assert.equal(isOwnerUserMessage({ role: 'user', content: 'hello' }), true)
  assert.equal(isOwnerUserMessage({ role: 'user', hidden: true }), false)
  // A continuation marker is still a turn boundary even though it is not an
  // owner message — this is what releases the mount bridge after auto-resume.
  assert.equal(startsFollowingTurn(marker), true)
  assert.equal(startsFollowingTurn({ role: 'user', content: 'hello' }), true)
  assert.equal(startsFollowingTurn({ role: 'assistant', content: 'reply' }), false)
})

test('the initial pageshow cannot retire a fast first-send pin', () => {
  assert.equal(shouldFreezeStreamingReturn({
    eventType: 'pageshow',
    pagePersisted: false,
    visibilityState: 'visible',
    turnActive: true,
  }), false)
})

test('genuine foreground return edges freeze an active reader position', () => {
  assert.equal(shouldFreezeStreamingReturn({
    eventType: 'pageshow',
    pagePersisted: true,
    visibilityState: 'visible',
    turnActive: true,
  }), true)
  assert.equal(shouldFreezeStreamingReturn({
    eventType: 'online',
    visibilityState: 'visible',
    turnActive: true,
  }), true)
  assert.equal(shouldFreezeStreamingReturn({
    eventType: 'visibilitychange',
    visibilityState: 'hidden',
    turnActive: true,
  }), false)
  assert.equal(shouldFreezeStreamingReturn({
    eventType: 'visibilitychange',
    visibilityState: 'visible',
    turnActive: false,
  }), false)
})

test('an in-process question answer keeps ownership of the active assistant turn', () => {
  assert.equal(answerTurnDisposition({
    status: 'answer_delivered',
    answer_turn: 'same',
  }), 'same')
  assert.equal(answerKeepsCurrentTurn({
    status: 'answer_delivered',
    answer_turn: 'same',
  }), true)
})

test('only a recovered question answer starts a new hidden turn', () => {
  assert.equal(answerTurnDisposition({
    status: 'started',
    answer_turn: 'new',
  }), 'new')
  assert.equal(answerKeepsCurrentTurn({
    status: 'started',
    answer_turn: 'new',
  }), false)
  assert.equal(answerKeepsCurrentTurn(null), false)
})

test('answer turn ownership requires the explicit semantic field', () => {
  assert.equal(answerTurnDisposition({ status: 'answer_delivered' }), 'unknown')
  assert.equal(answerTurnDisposition({ status: 'started' }), 'unknown')
})

test('a parked owner question uses compact history until its answer resumes the turn', () => {
  assert.equal(shouldAttachRunningStream({
    running: true,
    pendingQuestionId: 'question-1',
  }), false)
  assert.equal(shouldAttachRunningStream({
    running: true,
    pendingQuestionId: null,
  }), true)
  assert.equal(shouldAttachRunningStream({
    running: false,
    pendingQuestionId: null,
  }), false)
})

test('a known server run settling recovers a live stream that missed its terminal event', () => {
  assert.equal(shouldRecoverSettledRuntime({
    runtimeWasObservedRunning: true,
    runtimeRunning: false,
    pendingCount: 0,
    streamStillActive: true,
  }), true)

  assert.equal(shouldRecoverSettledRuntime({
    runtimeWasObservedRunning: false,
    runtimeRunning: false,
    pendingCount: 0,
    streamStillActive: true,
  }), false, 'the optimistic send window is not mistaken for a settled turn')

  assert.equal(shouldRecoverSettledRuntime({
    runtimeWasObservedRunning: true,
    runtimeRunning: false,
    pendingCount: 1,
    streamStillActive: true,
  }), false, 'a queued continuation still owns the handoff')

  assert.equal(shouldRecoverSettledRuntime({
    runtimeWasObservedRunning: true,
    runtimeRunning: false,
    pendingCount: 0,
    streamStillActive: true,
    stopInFlight: true,
  }), false, 'the explicit stop flow owns its own settlement')

  assert.equal(shouldRecoverSettledRuntime({
    runtimeWasObservedRunning: true,
    runtimeRunning: false,
    pendingCount: 0,
    streamStillActive: true,
    localStartInFlight: true,
  }), false, 'an unacknowledged local start owns the idle-snapshot race')
})

test('a fresh running verdict repairs only an exhausted visible stream', () => {
  assert.equal(runtimeStreamAttachAction({
    running: true,
    connectionError: 'disconnected',
  }), 'retry')
  assert.equal(runtimeStreamAttachAction({
    running: true,
    connectionError: null,
  }), 'connect')
  assert.equal(runtimeStreamAttachAction({
    running: true,
    connectionError: 'retrying',
  }), 'none', 'the bounded retry loop keeps sole ownership while active')
  assert.equal(runtimeStreamAttachAction({
    running: true,
    pendingQuestionId: 'question-1',
    connectionError: 'disconnected',
  }), 'none', 'a parked question has no live output to attach to')
  assert.equal(runtimeStreamAttachAction({
    running: true,
    connectionError: 'disconnected',
    hidden: true,
  }), 'none', 'only the visible pane owns transport recovery')
  assert.equal(runtimeStreamAttachAction({
    running: false,
    connectionError: 'disconnected',
  }), 'none', 'an idle server verdict must not resurrect a stream')
})

test('assistant ownership ignores only idle responses captured behind a local transition', () => {
  assert.equal(shouldAdoptRuntimeAssistantOwner({
    runtimeRunning: false,
    localAuthoritative: true,
  }), false, 'a pre-StartTurn idle response cannot retire the new local owner')
  assert.equal(shouldAdoptRuntimeAssistantOwner({
    runtimeRunning: true,
    localAuthoritative: true,
  }), true, 'a running response has crossed the atomic start boundary')
  assert.equal(shouldAdoptRuntimeAssistantOwner({
    runtimeRunning: false,
    localAuthoritative: false,
  }), true, 'an ordinary idle response retires the settled owner')
  assert.equal(shouldAdoptRuntimeAssistantOwner({
    runtimeRunning: false,
    localAuthoritative: true,
    authoritativeRefresh: true,
  }), true, 'a canonical transcript refresh may settle a missed terminal event')
})

test('only a cold stream prefix missing the durable question is retired', () => {
  const pendingQuestionId = 'question-1'
  const messages = [{
    role: 'assistant',
    blocks: [{ type: 'question', question_id: pendingQuestionId, questions: [] }],
  }]
  const stalePrefix = [{ type: 'text', content: 'older prefix' }]
  const completeSnapshot = [
    ...stalePrefix,
    { type: 'question', question_id: pendingQuestionId, questions: [] },
  ]

  assert.equal(shouldRetireRestoredQuestionSnapshot({
    messages,
    streamItems: stalePrefix,
    pendingQuestionId,
  }), true)
  assert.equal(shouldRetireRestoredQuestionSnapshot({
    isStreaming: true,
    messages,
    streamItems: stalePrefix,
    pendingQuestionId,
  }), false, 'a live socket keeps the buffer needed by same-turn continuation')
  assert.equal(shouldRetireRestoredQuestionSnapshot({
    messages,
    streamItems: completeSnapshot,
    pendingQuestionId,
  }), false)
  assert.equal(shouldRetireRestoredQuestionSnapshot({
    messages: [],
    streamItems: stalePrefix,
    pendingQuestionId,
  }), false)
})

test('a pathological cold transcript is prepared as stable prefix frames', () => {
  const large = {
    role: 'assistant',
    ts: 2,
    blocks: Array.from({ length: 9 }, (_, index) => ({
      type: 'text',
      content: `report-${index}-${'x'.repeat(4000)}`,
    })),
  }
  const messages = [{ role: 'user', ts: 1, content: 'audit' }, large]
  const frames = coldTranscriptRenderFrames(messages, {
    minCost: 1,
    frameBudget: 4,
  })

  assert.equal(frames.length, 5)
  assert.deepEqual(
    frames.slice(0, -1).map(frame => frame.at(-1).blocks.length),
    [2, 4, 6, 8],
  )
  assert.equal(frames.at(-1), messages,
    'the reveal frame is the authoritative transcript array')
  assert.equal(frames[0][0], messages[0],
    'already-complete prefix messages retain identity across frames')
})

test('ordinary cold transcripts keep the one-commit path', () => {
  const messages = [{
    role: 'assistant',
    blocks: [{ type: 'text', content: 'Short answer' }],
  }]
  assert.deepEqual(coldTranscriptRenderFrames(messages), [messages])
})

test('collapsed activity runs are prepared as the one row they present', () => {
  const blocks = Array.from({ length: 360 }, (_, index) => ({
    type: index % 2 === 0 ? 'thinking' : 'tool',
    ...(index % 2 === 0
      ? { content: `reasoning ${index}` }
      : { tool: 'Bash', status: 'done', output: `step ${index}` }),
  }))
  const messages = [{ role: 'assistant', ts: 1, blocks }]

  assert.deepEqual(coldTranscriptRenderFrames(messages), [messages],
    'one collapsed ActivityStretch must not become ninety hidden prefix commits')
})

test('one long markdown block grows by token fractions instead of one giant frame', () => {
  const block = { type: 'text', content: 'x'.repeat(48000) }
  const messages = [{ role: 'assistant', ts: 1, blocks: [block] }]
  const frames = coldTranscriptRenderFrames(messages, {
    minCost: 1,
    frameBudget: 4,
  })

  assert.deepEqual(
    frames.slice(0, -1).map(frame => frame[0].blocks[0]._coldRenderFraction),
    [4 / 12, 8 / 12],
  )
  assert.equal(frames.at(-1), messages)
  assert.equal('_coldRenderFraction' in block, false,
    'the read-side render plan never mutates transcript data')
})

test('an unknown explicit answer-turn value fails closed to a separate boundary', () => {
  assert.equal(answerTurnDisposition({
    status: 'started',
    answer_turn: 'future-mode',
  }), 'unknown')
  assert.equal(answerKeepsCurrentTurn({
    status: 'started',
    answer_turn: 'future-mode',
  }), false)
})

test('R4: a recent-page refresh preserves the loaded prefix containing the return anchor', () => {
  const loaded = Array.from({ length: 40 }, (_, index) => ({
    role: index % 2 ? 'assistant' : 'user',
    cid: index % 2 ? undefined : `message-cid-${index + 5}`,
    ts: 1700000000000 + index + 5,
    content: `Loaded ${index + 5}`,
  }))
  const recent = Array.from({ length: 20 }, (_, index) => ({
    role: index % 2 ? 'assistant' : 'user',
    cid: index % 2 ? undefined : `message-cid-${index + 25}`,
    ts: 1700000000000 + index + 25,
    content: `Fresh ${index + 25}`,
  }))

  const restored = mergeRecentMessagesIntoLoadedWindow({
    loadedMessages: loaded,
    loadedOffset: 5,
    recentMessages: recent,
    recentOffset: 25,
  })

  assert.equal(restored.offset, 5)
  assert.equal(restored.messages.length, 40)
  assert.equal(restored.messages[0].content, 'Loaded 5',
    'the older row that can own the saved ANCHOR_AT remains mounted')
  assert.equal(restored.messages[20].content, 'Fresh 25',
    'the overlapping recent page still refreshes from server truth')
})

test('R4: a non-overlapping or rewritten recent page replaces stale loaded history', () => {
  const loaded = Array.from({ length: 20 }, (_, index) => ({
    id: `old-${index}`,
    role: 'assistant',
    content: `Old ${index}`,
  }))
  const recent = Array.from({ length: 20 }, (_, index) => ({
    id: `new-${index}`,
    role: 'assistant',
    content: `New ${index}`,
  }))

  assert.deepEqual(mergeRecentMessagesIntoLoadedWindow({
    loadedMessages: loaded,
    loadedOffset: 0,
    recentMessages: recent,
    recentOffset: 20,
  }), {
    messages: recent,
    offset: 20,
    verified: false,
  })
})

test('a local turn refreshes completed history while preserving its optimistic suffix', () => {
  const loaded = [
    { role: 'user', cid: 'u1', ts: 1, content: 'Earlier question' },
    { role: 'assistant', ts: 2, content: 'Stale partial' },
    { role: 'user', cid: 'u2', ts: 3, content: 'New local turn', optimistic: true },
  ]
  const recent = [
    { role: 'user', cid: 'u1', ts: 1, content: 'Earlier question' },
    { role: 'assistant', ts: 2, content: 'Completed previous reply' },
  ]

  const refreshed = mergeRecentMessagesIntoLoadedWindow({
    loadedMessages: loaded,
    loadedOffset: 0,
    recentMessages: recent,
    recentOffset: 0,
    preserveLocalSuffix: true,
  })

  assert.deepEqual(refreshed.messages, [
    recent[0],
    recent[1],
    loaded[2],
  ])
})

test('stripInternalUserMessageFields KEEPS cid and drops the envelope fields', () => {
  const kept = stripInternalUserMessageFields({
    role: 'user', content: 'hi', ts: 7, cid: 'keep-me',
    queued: true, position: 2, _consumed_cids: ['a'], _messages: [{}],
    _agent_content: 'x',
  })
  assert.equal(kept.cid, 'keep-me')
  assert.equal(kept.queued, undefined)
  assert.equal(kept.position, undefined)
  assert.equal(kept._consumed_cids, undefined)
  assert.equal(kept._messages, undefined)
  assert.equal(kept._agent_content, undefined)
  assert.equal(kept.content, 'hi')
  assert.equal(kept.ts, 7)
})

test('startedMessagesFromResponse preserves backend _messages as separate visible rows', () => {
  const rows = startedMessagesFromResponse({
    message: {
      role: 'user',
      content: 'A\nB',
      ts: 1,
      _messages: [
        { role: 'user', content: 'A', ts: 10, queued: true, cid: 'x' },
        { role: 'user', content: 'B', ts: 11, queued: true, cid: 'y' },
      ],
    },
  })
  assert.deepEqual(rows.map(r => r.content), ['A', 'B'])
  assert.deepEqual(rows.map(r => r.ts), [10, 11])
  assert.equal(rows[0].queued, undefined)
  // cid now SURVIVES the strip — it is the durable row identity.
  assert.equal(rows[0].cid, 'x')
})

test('continuationRowsFromPromotedMessage prefers backend _messages over local combined row', () => {
  const rows = continuationRowsFromPromotedMessage(
    { role: 'user', content: 'server combined', ts: 1, _messages: [
      { role: 'user', content: 'first', ts: 101 },
      { role: 'user', content: 'second', ts: 102 },
    ] },
    { role: 'user', content: 'local combined', ts: 2 },
  )
  assert.deepEqual(rows.map(r => r.content), ['first', 'second'])
})

test('canFastForwardQueue requires active turn and server-confirmed queued rows', () => {
  assert.equal(canFastForwardQueue([], true), false)
  assert.equal(canFastForwardQueue([{ ts: 1, serverTs: true }], false), false)
  assert.equal(canFastForwardQueue([{ ts: 1, serverTs: false }], true), false)
  assert.equal(canFastForwardQueue([{ ts: '1', serverTs: true }], true), false)
  assert.equal(canFastForwardQueue([{ ts: 1, serverTs: true }, { ts: 2, serverTs: true }], true), true)
})

test('systemEventForChat annotates forwarded stream events with their chat id', () => {
  assert.deepEqual(systemEventForChat({ type: 'app_updated', appId: '7' }, 'chat-a'), {
    type: 'app_updated',
    appId: '7',
    chatId: 'chat-a',
  })
  assert.deepEqual(systemEventForChat({ type: 'app_updated', chatId: 'old' }, 'chat-a'), {
    type: 'app_updated',
    chatId: 'chat-a',
  })
  assert.equal(systemEventForChat(null, 'chat-a'), null)
  assert.deepEqual(systemEventForChat({ type: 'app_updated' }, null), { type: 'app_updated' })
})

test('serverSnapshotBehindLocal only preserves explicit unsaved local rows', () => {
  const server = [
    { role: 'user', content: 'saved one', ts: 1 },
    { role: 'assistant', content: 'saved two', ts: 2 },
  ]

  assert.equal(serverSnapshotBehindLocal(server, [
    ...server,
    { role: 'assistant', content: 'stale duplicate from old client', ts: 3 },
  ]), false)

  assert.equal(serverSnapshotBehindLocal(server, [
    ...server,
    { role: 'user', content: 'posting', ts: 4, optimistic: true },
  ]), true)

  assert.equal(serverSnapshotBehindLocal(server, [
    ...server,
    { role: 'user', content: 'queued', ts: 5, queued: true },
  ]), true)

  assert.equal(serverSnapshotBehindLocal(server, [
    ...server,
    { role: 'user', content: 'waiting for canonical ts', ts: 6, serverTs: false },
  ]), true)
})

test('stopRequestSucceeded requires a confirmed backend stop', () => {
  assert.equal(stopRequestSucceeded({ responseOk: true, data: { stopped: true } }), true)
  assert.equal(stopRequestSucceeded({ responseOk: true, data: {} }), true,
    'legacy 200/non-json stop responses are accepted')
  assert.equal(stopRequestSucceeded({ responseOk: true, data: { stopped: false } }), false)
  assert.equal(stopRequestSucceeded({ responseOk: false, data: null }), false)
  assert.equal(stopRequestSucceeded({ fetchFailed: true }), false)
})

test('stopConfirmedIdle requires the chat runtime to report idle', () => {
  assert.equal(stopConfirmedIdle({
    stopSucceeded: true,
    confirmRunning: false,
  }), true)
  assert.equal(stopConfirmedIdle({
    stopSucceeded: true,
    confirmRunning: true,
  }), false)
  assert.equal(stopConfirmedIdle({
    stopSucceeded: true,
    confirmRunning: undefined,
  }), false)
  assert.equal(stopConfirmedIdle({
    stopSucceeded: false,
    confirmRunning: false,
  }), false)
  assert.equal(stopConfirmedIdle({
    stopSucceeded: true,
    confirmRunning: false,
    confirmFailed: true,
  }), false)
})

test('shouldRetryStopAfterConfirm retries only the start-window running race', () => {
  assert.equal(shouldRetryStopAfterConfirm({
    requestSucceeded: true,
    confirmRunning: true,
  }), true)
  assert.equal(shouldRetryStopAfterConfirm({
    requestSucceeded: true,
    confirmRunning: false,
  }), false)
  assert.equal(shouldRetryStopAfterConfirm({
    requestSucceeded: false,
    confirmRunning: true,
  }), false)
  assert.equal(shouldRetryStopAfterConfirm({
    requestSucceeded: true,
    confirmRunning: true,
    confirmFailed: true,
  }), false)
})
