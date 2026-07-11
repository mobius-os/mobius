import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  canFastForwardQueue,
  continuationRowsFromPromotedMessage,
  openAppCtaViewModel,
  previewReadyAnnouncement,
  resolveFreshPinRetarget,
  resolveSteeredPinDecision,
  serverSnapshotBehindLocal,
  shouldShowOpenAppCta,
  startedMessagesFromResponse,
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
  assert.deepEqual(systemEventForChat({ type: 'app_built', appId: '7' }, 'chat-a'), {
    type: 'app_built',
    appId: '7',
    chatId: 'chat-a',
  })
  assert.deepEqual(systemEventForChat({ type: 'app_built', chatId: 'old' }, 'chat-a'), {
    type: 'app_built',
    chatId: 'chat-a',
  })
  assert.equal(systemEventForChat(null, 'chat-a'), null)
  assert.deepEqual(systemEventForChat({ type: 'app_built' }, null), { type: 'app_built' })
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
