import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  builtAppPulseDecision,
  canFastForwardQueue,
  continuationRowsFromPromotedMessage,
  openAppCtaViewModel,
  previewReadyAnnouncement,
  previewUpdatedAnnouncement,
  resolveFreshPinRetarget,
  resolveSteeredPinDecision,
  serverSnapshotBehindLocal,
  shouldRetryStopAfterConfirm,
  shouldShowOpenAppCta,
  startedMessagesFromResponse,
  stopConfirmedIdle,
  stopRequestSucceeded,
  systemEventForChat,
} from '../chatRuntimeState.js'

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
  assert.equal(rows[0].cid, undefined)
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

test('resolveSteeredPinDecision falls back to live scroll when local intent is missing', () => {
  assert.deepEqual(resolveSteeredPinDecision({
    pinTargetTs: 123,
    pinIntent: null,
    fallbackWillPin: () => true,
  }), {
    intentStillCurrent: true,
    shouldPin: true,
  })

  assert.deepEqual(resolveSteeredPinDecision({
    pinTargetTs: 123,
    pinIntent: null,
    fallbackWillPin: () => false,
  }), {
    intentStillCurrent: true,
    shouldPin: false,
  })
})

test('resolveSteeredPinDecision honors stale local intent over fallback', () => {
  assert.deepEqual(resolveSteeredPinDecision({
    pinTargetTs: 123,
    pinIntent: { willPin: true, userScrollIntentVersion: 1 },
    pinIntentStillCurrent: () => false,
    fallbackWillPin: () => true,
  }), {
    intentStillCurrent: false,
    shouldPin: false,
  })
})

test('resolveFreshPinRetarget moves pin from optimistic ts to canonical server ts', () => {
  assert.deepEqual(resolveFreshPinRetarget({
    startedMessages: [{ role: 'user', content: 'hello', ts: 222 }],
    fallbackTs: 111,
    willPin: true,
    pinIntent: { willPin: true, userScrollIntentVersion: 1 },
    pinIntentStillCurrent: () => true,
  }), {
    pinTargetTs: 222,
    intentStillCurrent: true,
    shouldPin: true,
  })
})

test('resolveFreshPinRetarget yields to a user scroll after submit', () => {
  assert.deepEqual(resolveFreshPinRetarget({
    startedMessages: [{ role: 'user', content: 'hello', ts: 222 }],
    fallbackTs: 111,
    willPin: true,
    pinIntent: { willPin: true, userScrollIntentVersion: 1 },
    pinIntentStillCurrent: () => false,
  }), {
    pinTargetTs: 222,
    intentStillCurrent: false,
    shouldPin: false,
  })
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
