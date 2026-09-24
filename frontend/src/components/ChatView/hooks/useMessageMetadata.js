/* Derive settled-message copy text and pinned metadata only when the transcript changes.
   Only the newest assistant row is pinned (copy without a tap); owner rows,
   including the newest, keep their timestamp behind tap-to-reveal. */
import { useMemo } from 'react'
import { isOwnerUserMessage } from '../chatRuntimeState.js'
import { messageCopyText } from '../messageCopy.js'

export default function useMessageMetadata(messages) {
  return useMemo(() => {
    let lastAssistant = -1
    for (let i = 0; i < messages.length; i += 1) {
      if (messages[i].role === 'assistant') lastAssistant = i
    }
    return messages.map((msg, i) => ({
      copyText: messageCopyText(msg),
      timestamp: isOwnerUserMessage(msg) ? msg.ts : null,
      alwaysVisible: i === lastAssistant,
    }))
  }, [messages])
}
