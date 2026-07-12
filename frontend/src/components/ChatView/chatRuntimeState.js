/* Pure chat-runtime helpers for queue/stream state decisions.
 * ChatView owns side effects; this file owns small branch conditions that need
 * focused tests because mobile timing regressions repeatedly happened here.
 */

/**
 * The stable identity of a user message. A client-minted `cid` is the
 * canonical identity (React key, DOM pin target `data-cid`, queue cancel
 * key, force-steer selection). A pre-cid (legacy) row derives `legacy-<ts>`
 * at read time — the SAME value the backend derives (chat_writer.cid_of) so
 * a derived id compares equal on both sides of the wire. `ts` is demoted to
 * display/ordering metadata only. Returns null for a row with neither.
 */
export function cidOf(msg) {
  if (!msg) return null
  if (msg.cid) return msg.cid
  return msg.ts != null ? `legacy-${msg.ts}` : null
}

export function stripInternalUserMessageFields(raw) {
  if (!raw) return null
  // KEEP `cid` — it is now the durable row identity and must survive the
  // strip that prepares a server row for the transcript. Only the UI-only /
  // envelope fields are removed.
  const {
    queued: _q,
    position: _p,
    _consumed_cids: _ccids,
    _messages: _msgs,
    _agent_content: _agentContent,
    ...msg
  } = raw
  return msg
}

export function startedMessagesFromResponse(result) {
  if (!result?.message) return null
  if (Array.isArray(result.message._messages)
      && result.message._messages.length > 0) {
    return result.message._messages
      .map(stripInternalUserMessageFields)
      .filter(Boolean)
  }
  const msg = stripInternalUserMessageFields(result.message)
  return msg ? [msg] : null
}

export function continuationRowsFromPromotedMessage(promotedMessage, localPromoted) {
  return startedMessagesFromResponse({ message: promotedMessage || localPromoted }) || []
}

export function serverSnapshotBehindLocal(serverMsgs, localMsgs) {
  if (!Array.isArray(localMsgs) || localMsgs.length === 0) return false
  if (!Array.isArray(serverMsgs)) return false
  if (serverMsgs.length > localMsgs.length) return false

  const serverTs = new Set(serverMsgs.map(m => m?.ts).filter(v => v != null))
  return localMsgs.some(m => {
    if (m?.ts == null || serverTs.has(m.ts)) return false
    return m.optimistic === true || m.queued === true || m.serverTs === false
  })
}

export function canFastForwardQueue(pendingMessages, turnActive) {
  return !!turnActive
    && Array.isArray(pendingMessages)
    && pendingMessages.length > 0
    && pendingMessages.every(m => typeof m?.ts === 'number' && m.serverTs === true)
}

export function shouldShowOpenAppCta(builtApp) {
  return Boolean(builtApp?.id)
}

export function openAppCtaViewModel(builtApp, turnActive) {
  if (!shouldShowOpenAppCta(builtApp)) return null
  const name = builtApp.name || 'app'
  if (turnActive) {
    return {
      label: `Open ${name} preview`,
      ariaLabel: `Open live preview of ${name}`,
    }
  }
  return {
    label: `Open ${name}`,
    ariaLabel: `Open ${name}`,
  }
}

export function previewReadyAnnouncement(builtApp) {
  if (!shouldShowOpenAppCta(builtApp)) return ''
  return `Live preview ready for ${builtApp.name || 'app'}.`
}

export function systemEventForChat(event, chatId) {
  if (!event || chatId === null || chatId === undefined || chatId === '') {
    return event
  }
  return { ...event, chatId }
}

export function stopRequestSucceeded({ responseOk, data = null, fetchFailed = false }) {
  if (fetchFailed) return false
  if (!responseOk) return false
  if (data && data.stopped === false) return false
  return true
}

export function stopConfirmedIdle({
  stopSucceeded,
  confirmRunning,
  confirmFailed = false,
}) {
  if (!stopSucceeded) return false
  if (confirmFailed) return false
  return confirmRunning === false
}

export function shouldRetryStopAfterConfirm({
  requestSucceeded,
  confirmRunning,
  confirmFailed = false,
}) {
  return !!requestSucceeded && !confirmFailed && confirmRunning === true
}
