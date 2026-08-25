/* Active-assistant selection derives the stable DB/live rendering surface. */

import { startsFollowingTurn } from './chatRuntimeState.js'
import {
  assistantStreamBelongsToActiveMessage,
  chooseActiveAssistantMirrorIndex,
  chooseActiveAssistantSurface,
  findTrailingAssistantPartialIndex,
  promoteAssistantStreamWithFollowingMessages,
} from './streamPromotion.js'


/**
 * Source selection can compare the complete live block list with persisted
 * partials several times. Keep that work behind one memoizable pure boundary;
 * draft text has no bearing on which assistant source owns the active row.
 */
export function deriveActiveAssistantSelection({
  turnActive,
  messages,
  streamItems,
  streamAssistantMessageId = null,
  activeAssistantMessageId = null,
  liveItemsRetired = false,
  findBridgeIndex,
}) {
  const streamIdentityMsgIdx = turnActive && streamAssistantMessageId
    ? messages.findIndex(message => (
        message?.role === 'assistant'
        && message.id != null
        && String(message.id) === String(streamAssistantMessageId)
      ))
    : -1
  const activeIdentityMsgIdx = turnActive && activeAssistantMessageId
    ? messages.findIndex(message => (
        message?.role === 'assistant'
        && message.id != null
        && String(message.id) === String(activeAssistantMessageId)
      ))
    : -1
  const identifiedStreamIsCurrent = assistantStreamBelongsToActiveMessage(
    streamAssistantMessageId,
    activeAssistantMessageId,
  )
  const identityMsgIdx = activeAssistantMessageId
    ? activeIdentityMsgIdx
    : streamIdentityMsgIdx
  const legacyBridgeMsgIdx = turnActive ? findBridgeIndex(messages) : -1
  const legacyBridgeFollowedByNewTurn = legacyBridgeMsgIdx >= 0 && messages
    .slice(legacyBridgeMsgIdx + 1)
    .some(startsFollowingTurn)
  const legacyBridgeAllowed = !activeAssistantMessageId && (
    !streamAssistantMessageId || (
      legacyBridgeMsgIdx >= 0
      && messages[legacyBridgeMsgIdx]?.id == null
      && !legacyBridgeFollowedByNewTurn
    )
  )
  const bridgeMsgIdx = identityMsgIdx >= 0
    ? identityMsgIdx
    : (legacyBridgeAllowed ? legacyBridgeMsgIdx : -1)
  let trailingAssistantPartialIdx = turnActive
    ? findTrailingAssistantPartialIndex(messages)
    : -1
  if (
    (activeAssistantMessageId || streamAssistantMessageId)
    && identityMsgIdx < 0
    && trailingAssistantPartialIdx >= 0
    && messages[trailingAssistantPartialIdx]?.id != null
    && (
      !activeAssistantMessageId
      || String(messages[trailingAssistantPartialIdx].id)
        !== String(activeAssistantMessageId)
    )
  ) {
    trailingAssistantPartialIdx = -1
  }
  const bridgeMsg = bridgeMsgIdx >= 0 ? messages[bridgeMsgIdx] : null
  const bridgeFollowedByNewTurn = !activeAssistantMessageId
    && bridgeMsgIdx >= 0 && messages
    .slice(bridgeMsgIdx + 1)
    .some(startsFollowingTurn)
  // A sessionStorage snapshot can survive the restart that begins a successor
  // turn. The server-owned assistant id is authoritative when available;
  // transcript boundaries remain only a rolling-data fallback.
  const staleIdentifiedPayload = !!streamAssistantMessageId && (
    !identifiedStreamIsCurrent
    || (
      !activeAssistantMessageId
      && streamIdentityMsgIdx >= 0
      && bridgeFollowedByNewTurn
    )
  )
  const hasLiveAssistantPayload = !!(
    turnActive && streamItems.length > 0 && !staleIdentifiedPayload
  )
  const trailingAssistantPartialMsg = trailingAssistantPartialIdx >= 0
    ? messages[trailingAssistantPartialIdx]
    : null
  // Promotion records the exact streamItems array that was painted when its
  // content moved into the durable transcript. A query-cache publish can expose
  // that durable row before React applies clearStreamItems; suppress only that
  // explicitly retired array. A ref/state mismatch alone is insufficient: a
  // legitimate continuation can advance latestItemsRef before its next paint.
  const staleLiveAssistantAfterPromotion = !!(
    hasLiveAssistantPayload && liveItemsRetired
  )
  const bridgeAssistantSurface = chooseActiveAssistantSurface(
    bridgeMsg,
    streamItems,
  )
  const trailingAssistantSurface = chooseActiveAssistantSurface(
    trailingAssistantPartialMsg,
    streamItems,
  )
  const activeMirrorMsgIdx = chooseActiveAssistantMirrorIndex({
    bridgeMsgIdx,
    trailingAssistantPartialIdx,
    bridgeFollowedByNewTurn,
    hasLivePayload: hasLiveAssistantPayload,
    bridgeSurface: bridgeAssistantSurface,
    surface: trailingAssistantSurface,
  })
  const activeMirrorMsg = activeMirrorMsgIdx >= 0
    ? messages[activeMirrorMsgIdx]
    : null
  const selectedSurface = activeMirrorMsgIdx === bridgeMsgIdx
    ? bridgeAssistantSurface
    : (activeMirrorMsgIdx === trailingAssistantPartialIdx
        ? trailingAssistantSurface
        : { hideMessage: false, suppressStream: false })
  const useDbActivePayload = !!(
    activeMirrorMsg
    && (!hasLiveAssistantPayload || selectedSurface.suppressStream)
  )
  const showActiveAssistantSurface = !staleLiveAssistantAfterPromotion && !!(
    useDbActivePayload ? activeMirrorMsg : hasLiveAssistantPayload
  )

  return {
    activeMirrorMsg,
    activeMirrorMsgIdx,
    bridgeMsgIdx,
    hasLiveAssistantPayload,
    showActiveAssistantSurface,
    staleLiveAssistantAfterPromotion,
    trailingAssistantPartialIdx,
    useDbActivePayload,
    activeAssistantIsStreaming: !!(
      showActiveAssistantSurface && !useDbActivePayload
    ),
  }
}


/**
 * Publish one live assistant array into the durable transcript atomically.
 * The retirement marker must become observable before the synchronous query
 * cache publish, otherwise one render can paint both copies of the answer.
 */
export function commitAssistantPromotion({
  retiredItemsRef,
  paintedItems,
  promotedItems,
  assistantMessageId,
  bridgeTs,
  followingMessages = [],
  commitMessages,
}) {
  retiredItemsRef.current = paintedItems
  commitMessages(
    prev => promoteAssistantStreamWithFollowingMessages(prev, {
      items: promotedItems,
      assistantMessageId,
      bridgeTs,
      followingMessages,
    }),
    undefined,
    { force: true },
  )
}
