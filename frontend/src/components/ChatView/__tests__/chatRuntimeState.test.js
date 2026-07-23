import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  answerKeepsCurrentTurn,
  answerTurnDisposition,
  builtAppPulseDecision,
  canFastForwardQueue,
  cidOf,
  continuationRowsFromPromotedMessage,
  isAutoContinuationMessage,
  isOwnerUserMessage,
  mergeRecentMessagesIntoLoadedWindow,
  openAppCtaViewModel,
  previewReadyAnnouncement,
  previewUpdatedAnnouncement,
  serverSnapshotBehindLocal,
  shouldRetryStopAfterConfirm,
  shouldShowOpenAppCta,
  startedMessagesFromResponse,
  stopConfirmedIdle,
  stopRequestSucceeded,
  stripInternalUserMessageFields,
  systemEventForChat,
} from '../chatRuntimeState.js'

test('automatic continuations are product markers, not owner messages', () => {
  const marker = {
    role: 'user',
    content: 'continue',
    kind: 'auto_continuation',
    continuation_reason: 'restart',
  }
  assert.equal(isAutoContinuationMessage(marker), true)
  assert.equal(isOwnerUserMessage(marker), false)
  assert.equal(isOwnerUserMessage({ role: 'user', content: 'hello' }), true)
  assert.equal(isOwnerUserMessage({ role: 'user', hidden: true }), false)
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

test('answer turn ownership stays compatible with older backends', () => {
  assert.equal(answerTurnDisposition({ status: 'answer_delivered' }), 'same')
  assert.equal(answerTurnDisposition({ status: 'started' }), 'new')
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

test('cidOf returns the row cid, else null (no read-time derivation)', () => {
  // Post-card-221 every user row carries an explicit cid (client-minted, or a
  // backfilled `legacy-<ts>`); cidOf returns it as-is and no longer derives one
  // from `ts`. `ts` is display/ordering metadata only.
  assert.equal(cidOf({ cid: 'abc', ts: 5 }), 'abc')
  assert.equal(cidOf({ cid: 'legacy-5', ts: 5 }), 'legacy-5')
  assert.equal(cidOf({ ts: 5 }), null)
  assert.equal(cidOf({}), null)
  assert.equal(cidOf(null), null)
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

test('shouldShowOpenAppCta is tied to the built app, not turn completion', () => {
  assert.equal(shouldShowOpenAppCta(null), false)
  assert.equal(shouldShowOpenAppCta({}), false)
  assert.equal(shouldShowOpenAppCta({ id: 42, name: 'Habits' }), true)
})

test('openAppCtaViewModel names the active preview and idle app action', () => {
  assert.equal(openAppCtaViewModel(null, true), null)
  assert.deepEqual(openAppCtaViewModel({ id: 42, name: 'Habits' }, true), {
    label: 'Open Habits preview',
    ariaLabel: 'Open live preview of Habits',
  })
  assert.deepEqual(openAppCtaViewModel({ id: 42, name: 'Habits' }, false), {
    label: 'Open Habits',
    ariaLabel: 'Open Habits',
  })
  assert.deepEqual(openAppCtaViewModel({ id: 42 }, true), {
    label: 'Open app preview',
    ariaLabel: 'Open live preview of app',
  })
})

test('previewReadyAnnouncement announces when a preview becomes available', () => {
  assert.equal(previewReadyAnnouncement(null), '')
  assert.equal(previewReadyAnnouncement({ id: 42, name: 'Habits' }), 'Live preview ready for Habits.')
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

test('previewUpdatedAnnouncement names the recompiled app', () => {
  assert.equal(previewUpdatedAnnouncement({ id: 7, name: 'Habits' }), 'Preview updated for Habits.')
  assert.equal(previewUpdatedAnnouncement({ id: 7 }), 'Preview updated for app.')
})

test('builtAppPulseDecision: a first-seen app announces but does not pulse', () => {
  const list = [{ id: 7, name: 'Habits', updated_at: 't1' }]
  const d = builtAppPulseDecision(list, new Map())
  assert.equal(d.pulseId, null)
  assert.equal(d.announce, 'Live preview ready for Habits.')
  assert.equal(d.nextSeen.get(7), 't1')
})

test('builtAppPulseDecision: an already-seen app whose updated_at advanced pulses', () => {
  const list = [{ id: 7, name: 'Habits', updated_at: 't2' }]
  const seen = new Map([[7, 't1']])
  const d = builtAppPulseDecision(list, seen)
  assert.equal(d.pulseId, 7)
  assert.equal(d.announce, 'Preview updated for Habits.')
  assert.equal(d.nextSeen.get(7), 't2')
})

test('builtAppPulseDecision: an already-seen app at the same updated_at neither pulses nor announces', () => {
  const list = [{ id: 7, name: 'Habits', updated_at: 't1' }]
  const d = builtAppPulseDecision(list, new Map([[7, 't1']]))
  assert.equal(d.pulseId, null)
  assert.equal(d.announce, '')
})

test('builtAppPulseDecision: a recompile wins the announce over a co-arriving new app', () => {
  const list = [
    { id: 7, name: 'Habits', updated_at: 't2' }, // seen at t1 → recompile
    { id: 8, name: 'Notes', updated_at: 't1' },  // brand new
  ]
  const d = builtAppPulseDecision(list, new Map([[7, 't1']]))
  assert.equal(d.pulseId, 7)
  assert.equal(d.announce, 'Preview updated for Habits.')
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
