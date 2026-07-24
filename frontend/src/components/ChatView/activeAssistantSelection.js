/* Active-assistant selection derives the stable DB/live rendering surface. */

import { isOwnerUserMessage } from './chatRuntimeState.js'
import {
  chooseActiveAssistantMirrorIndex,
  chooseActiveAssistantSurface,
  findTrailingAssistantPartialIndex,
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
  findBridgeIndex,
}) {
  const bridgeMsgIdx = turnActive ? findBridgeIndex(messages) : -1
  const trailingAssistantPartialIdx = turnActive
    ? findTrailingAssistantPartialIndex(messages)
    : -1
  const hasLiveAssistantPayload = turnActive && streamItems.length > 0
  const bridgeMsg = bridgeMsgIdx >= 0 ? messages[bridgeMsgIdx] : null
  const bridgeFollowedByVisibleUser = bridgeMsgIdx >= 0 && messages
    .slice(bridgeMsgIdx + 1)
    .some(isOwnerUserMessage)
  const trailingAssistantPartialMsg = trailingAssistantPartialIdx >= 0
    ? messages[trailingAssistantPartialIdx]
    : null
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
    bridgeFollowedByVisibleUser,
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
  const showActiveAssistantSurface = !!(
    useDbActivePayload ? activeMirrorMsg : hasLiveAssistantPayload
  )

  return {
    activeMirrorMsg,
    activeMirrorMsgIdx,
    bridgeMsgIdx,
    hasLiveAssistantPayload,
    showActiveAssistantSurface,
    trailingAssistantPartialIdx,
    useDbActivePayload,
    activeAssistantIsStreaming: !!(
      showActiveAssistantSurface && !useDbActivePayload
    ),
  }
}
