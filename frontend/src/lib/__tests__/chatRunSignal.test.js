import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  EMPTY_CHAT_RUN_SIGNAL,
  EMPTY_TURN_DONE_GATE,
  advanceChatRunSignal,
  advanceTurnDoneGate,
  bumpChatRunSignal,
  chatRunSignal,
  chatRunSignalDelta,
} from '../chatRunSignal.js'


test('coalesced start and finish retain monotonic chat activity', () => {
  let signals = new Map()
  signals = bumpChatRunSignal(signals, 'chat-1', 'chat_run_started')
  signals = bumpChatRunSignal(signals, 'chat-1', 'chat_run_finished')

  const signal = chatRunSignal(signals, 'chat-1')
  assert.deepEqual(signal, { seq: 2, starts: 1, finishes: 1 })
  assert.deepEqual(chatRunSignalDelta(EMPTY_CHAT_RUN_SIGNAL, signal), {
    started: true,
    finished: true,
  })
})


test('run activity remains isolated by chat id', () => {
  let signals = new Map()
  signals = bumpChatRunSignal(signals, 'chat-1', 'chat_run_started')

  assert.equal(chatRunSignal(signals, 'chat-1').starts, 1)
  assert.equal(chatRunSignal(signals, 'chat-2'), EMPTY_CHAT_RUN_SIGNAL)
})


test('stream-open reconciliation advances work without inventing an event', () => {
  const reconciled = advanceChatRunSignal(
    EMPTY_CHAT_RUN_SIGNAL,
    'chat_run_reconcile',
  )
  assert.deepEqual(chatRunSignalDelta(EMPTY_CHAT_RUN_SIGNAL, reconciled), {
    started: false,
    finished: false,
  })
})


for (const events of [
  ['chat_run_started', 'stream_finished', 'chat_run_finished'],
  ['chat_run_started', 'chat_run_finished', 'stream_finished'],
]) {
  test(`TURN_DONE is emitted once for ${events.join(' -> ')}`, () => {
    let gate = EMPTY_TURN_DONE_GATE
    let emitted = 0
    for (const event of events) {
      const result = advanceTurnDoneGate(gate, event)
      gate = result.gate
      if (result.emit) emitted += 1
    }
    assert.equal(emitted, 1)
  })
}


test('a coalesced external start and finish arms then completes once', () => {
  let gate = EMPTY_TURN_DONE_GATE
  for (const event of ['chat_run_started', 'chat_run_finished']) {
    const result = advanceTurnDoneGate(gate, event)
    gate = result.gate
  }
  assert.equal(gate.sent, true)
})


test('a queued continuation emits once for each completed turn', () => {
  let gate = EMPTY_TURN_DONE_GATE
  let emitted = 0
  for (const event of [
    'message_started',
    'stream_continues',
    'stream_finished',
  ]) {
    const result = advanceTurnDoneGate(gate, event)
    gate = result.gate
    if (result.emit) emitted += 1
  }
  assert.equal(emitted, 2)
  assert.equal(gate.sent, true)
})


test('a durable park arms completion before a wholly missed automatic run', () => {
  let gate = { sent: true } // The parked turn already completed.
  gate = advanceTurnDoneGate(gate, 'auto_resume_waiting').gate
  const finished = advanceTurnDoneGate(gate, 'chat_run_finished')
  assert.equal(finished.emit, true)
  assert.equal(finished.gate.sent, true)
})
