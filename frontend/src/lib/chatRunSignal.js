/**
 * System run events are activity notifications, not durable boolean state.
 * A start and finish can arrive in one ReadableStream chunk and one React
 * batch, so retaining a monotonic value guarantees the mounted chat observes
 * that something happened even when the final running boolean is unchanged.
 */
export const EMPTY_CHAT_RUN_SIGNAL = Object.freeze({
  seq: 0,
  starts: 0,
  finishes: 0,
})


export function advanceChatRunSignal(signal, eventType) {
  if (eventType === 'chat_run_reconcile') {
    return { ...signal, seq: signal.seq + 1 }
  }
  if (eventType !== 'chat_run_started' && eventType !== 'chat_run_finished') {
    return signal
  }
  return {
    seq: signal.seq + 1,
    starts: signal.starts + (eventType === 'chat_run_started' ? 1 : 0),
    finishes: signal.finishes + (eventType === 'chat_run_finished' ? 1 : 0),
  }
}


export function bumpChatRunSignal(signals, chatId, eventType) {
  if (!chatId) return signals
  const key = String(chatId)
  const next = new Map(signals)
  next.set(
    key,
    advanceChatRunSignal(next.get(key) || EMPTY_CHAT_RUN_SIGNAL, eventType),
  )
  return next
}


export function chatRunSignal(signals, chatId) {
  if (!chatId) return EMPTY_CHAT_RUN_SIGNAL
  return signals.get(String(chatId)) || EMPTY_CHAT_RUN_SIGNAL
}


export function chatRunSignalDelta(previous, current) {
  return {
    started: current.starts > previous.starts,
    finished: current.finishes > previous.finishes,
  }
}


export const EMPTY_TURN_DONE_GATE = Object.freeze({ sent: false })


export function advanceTurnDoneGate(gate, eventType) {
  if (
    eventType === 'chat_run_started'
    || eventType === 'message_started'
    || eventType === 'auto_resume_waiting'
  ) {
    return { gate: { sent: false }, emit: false }
  }
  if (
    eventType !== 'chat_run_finished'
    && eventType !== 'stream_finished'
    && eventType !== 'stream_continues'
  ) {
    return { gate, emit: false }
  }
  const emit = !gate.sent
  return {
    gate: { sent: eventType !== 'stream_continues' },
    emit,
  }
}
