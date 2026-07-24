/* ActiveAssistantSurface isolates the live answer from composer-only renders. */

import { memo, useMemo } from 'react'
import StreamingMessage from './StreamingMessage.jsx'
import { streamItemsToAssistantPayload } from './streamPromotion.js'


/**
 * The active answer is often the most expensive subtree in the shell: a long
 * turn can contain dozens of tool, thinking, text, and question blocks.
 * Composer text belongs to a separate interaction, so a keystroke must not
 * rebuild that tree while the stream inputs themselves are unchanged.
 *
 * React.memo supplies the component boundary. The memoized payload also keeps
 * MsgContent's existing identity comparison effective when some other
 * ChatView-only state changes without advancing the stream.
 */
function ActiveAssistantSurface({
  activeMirrorMsg,
  useDbActivePayload,
  hasLivePayload,
  streamItems,
  dataKey,
  chatId,
  onAnswer,
  onResume,
  onInternalNav,
  autoResumeEnabled,
  autoResumeAvailable,
  autoResumeSaving,
  autoResumeError,
  onAutoResumeChange,
  submissionBlocked,
  liveQuestionId,
  isStreaming,
}) {
  const msg = useMemo(() => {
    if (useDbActivePayload) return activeMirrorMsg
    if (!hasLivePayload) return null
    return {
      ...(activeMirrorMsg || {}),
      role: 'assistant',
      // Live rendering keeps running tool state and thinking clock anchors;
      // final promotion converts the same items with finalize=true.
      ...streamItemsToAssistantPayload(streamItems, { finalize: false }),
    }
  }, [activeMirrorMsg, hasLivePayload, streamItems, useDbActivePayload])

  if (!msg) return null

  return (
    <StreamingMessage
      msg={msg}
      dataKey={dataKey}
      chatId={chatId}
      onAnswer={onAnswer}
      onResume={onResume}
      onInternalNav={onInternalNav}
      autoResumeEnabled={autoResumeEnabled}
      autoResumeAvailable={autoResumeAvailable}
      autoResumeSaving={autoResumeSaving}
      autoResumeError={autoResumeError}
      onAutoResumeChange={onAutoResumeChange}
      submissionBlocked={submissionBlocked}
      liveQuestionId={liveQuestionId}
      isStreaming={isStreaming}
    />
  )
}

export default memo(ActiveAssistantSurface)
