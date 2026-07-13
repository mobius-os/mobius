/* Pure chat-runtime helpers for queue/stream state decisions.
 * ChatView owns side effects; this file owns small branch conditions that need
 * focused tests because mobile timing regressions repeatedly happened here.
 */

/**
 * The stable identity of a user message. `cid` is the canonical identity
 * (React key, DOM pin target `data-cid`, queue cancel key, force-steer
 * selection). Current clients mint it; card-221 backfilled `cid=legacy-<ts>`
 * onto every legacy row, so post-migration every user row carries an explicit
 * cid and no read-time derivation is needed (chat_writer.cid_of matches). `ts`
 * is display/ordering metadata only. Returns null for a row with no cid.
 */
export function cidOf(msg) {
  if (!msg) return null
  return msg.cid || null
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

export function previewUpdatedAnnouncement(builtApp) {
  return `Preview updated for ${builtApp?.name || 'app'}.`
}

// Pure decision for the built-app CTA pulse + announce, given the current CTA
// list (derived from server truth, newest last) and a Map of the last-seen
// updated_at per app id. Both cases — first build vs source recompile — are
// derived here from updated_at deltas alone:
//
//   - a NEW id (absent from `lastSeen`) is a FIRST BUILD: record its updated_at
//     WITHOUT pulsing, and the newest such app drives the first-build announce
//     ("Live preview ready …").
//   - an ALREADY-SEEN id whose updated_at ADVANCED is a source RECOMPILE: flash
//     "Preview updated ✓" and announce it. A recompile announce wins over a
//     first-build one in the same batch.
//
// Because the derived list is `app.chat_id === activeChatId`, an app appears in
// exactly one chat's list (its single chat_id), so this per-ChatView decision
// can only ever pulse in the chat that owns the app — no cross-chat flash.
// SANCTIONED trade: updated_at also bumps on a rename/metadata write, so such
// an update flashes "Preview updated ✓" and can reorder the CTA list (it sorts
// by updated_at) — accepted as-is, since the app row DID update and tracking a
// parallel source-only timestamp would recreate the duplicated state this
// derivation removed.
export function builtAppPulseDecision(builtApps, lastSeen) {
  const list = Array.isArray(builtApps) ? builtApps : []
  const seen = lastSeen instanceof Map ? lastSeen : new Map()
  const nextSeen = new Map()
  let pulseApp = null
  let newApp = null
  for (const app of list) {
    if (!app || app.id == null) continue
    const id = Number(app.id)
    const updatedAt = app.updated_at ?? null
    nextSeen.set(id, updatedAt)
    if (!seen.has(id)) {
      newApp = app
    } else if (updatedAt != null && seen.get(id) != null
        && updatedAt !== seen.get(id)) {
      pulseApp = app
    }
  }
  const announce = pulseApp
    ? previewUpdatedAnnouncement(pulseApp)
    : (newApp ? previewReadyAnnouncement(newApp) : '')
  return {
    pulseId: pulseApp ? Number(pulseApp.id) : null,
    announce,
    nextSeen,
  }
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
