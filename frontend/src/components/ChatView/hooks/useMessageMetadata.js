/* Derive settled-message copy text and pinned metadata only when the transcript changes. */
import { useMemo } from 'react'
import { isOwnerUserMessage } from '../chatRuntimeState.js'
import { messageCopyText } from '../messageCopy.js'

export default function useMessageMetadata(messages) {
  return useMemo(() => {
    let lastUser = -1
    let lastAssistant = -1
    for (let i = 0; i < messages.length; i += 1) {
      if (isOwnerUserMessage(messages[i])) lastUser = i
      if (messages[i].role === 'assistant') lastAssistant = i
    }
    return messages.map((msg, i) => ({
      copyText: messageCopyText(msg),
      timestamp: isOwnerUserMessage(msg) ? msg.ts : null,
      alwaysVisible: i === lastUser || i === lastAssistant,
    }))
  }, [messages])
}
