/* Pure chat-runtime helpers for queue/stream state decisions.
 * ChatView owns side effects; this file owns small branch conditions that need
 * focused tests because mobile timing regressions repeatedly happened here.
 */

export function stripInternalUserMessageFields(raw) {
  if (!raw) return null
  const {
    queued: _q,
    cid: _c,
    position: _p,
    _consumed_ts: _cts,
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

export function resolveSteeredPinDecision({
  pinTargetTs,
  pinIntent,
  pinIntentStillCurrent,
  fallbackWillPin,
}) {
  const intentStillCurrent = pinIntent
    ? !!pinIntentStillCurrent?.(pinIntent)
    : true
  return {
    intentStillCurrent,
    shouldPin: pinTargetTs != null
      && intentStillCurrent
      && (pinIntent ? !!pinIntent.willPin : !!fallbackWillPin?.()),
  }
}

export function resolveFreshPinRetarget({
  startedMessages,
  fallbackTs,
  willPin,
  pinIntent,
  pinIntentStillCurrent,
}) {
  const pinTargetTs = Array.isArray(startedMessages) && startedMessages.length > 0
    ? startedMessages[0]?.ts
    : fallbackTs
  const intentStillCurrent = pinIntent
    ? !!pinIntentStillCurrent?.(pinIntent)
    : true
  return {
    pinTargetTs,
    intentStillCurrent,
    shouldPin: pinTargetTs != null && intentStillCurrent && !!willPin,
  }
}
