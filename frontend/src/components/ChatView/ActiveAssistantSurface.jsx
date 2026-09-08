/* ActiveAssistantSurface isolates the live answer from composer-only renders. */

import { memo, useMemo } from 'react'
import StreamingMessage from './StreamingMessage.jsx'
import {
  carryQuestionAnswers,
  streamItemsToAssistantPayload,
} from './streamPromotion.js'
import { projectSteerContinuationMessage } from './steerContinuity.js'


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
  onAnswerPrepare,
  onResume,
  onInternalNav,
  autoResumeEnabled,
  autoResumeAvailable,
  autoResumeSaving,
  autoResumeError,
  onAutoResumeChange,
  limitResetElapsed,
  submissionBlocked,
  liveQuestionId,
  pendingQuestionRef,
  resumeCardRef,
  isStreaming,
  sealedSteerAssistant,
}) {
  const msg = useMemo(() => {
    let source = null
    if (useDbActivePayload) {
      source = activeMirrorMsg
    } else if (hasLivePayload) {
      const livePayload = streamItemsToAssistantPayload(streamItems, { finalize: false })
      source = {
        ...(activeMirrorMsg || {}),
        role: 'assistant',
        // Live rendering keeps running tool state and thinking clock anchors;
        // final promotion converts the same items with finalize=true. The
        // mirrored DB blocks supply only durable interaction state: a catch-up
        // replay can be richer overall while still carrying the original blank
        // form of a question whose answer has already committed.
        ...livePayload,
        blocks: carryQuestionAnswers(
          livePayload.blocks,
          activeMirrorMsg?.blocks || [],
        ),
      }
    }
    return projectSteerContinuationMessage(
      sealedSteerAssistant,
      source,
      { active: isStreaming },
    )
  }, [
    activeMirrorMsg,
    hasLivePayload,
    isStreaming,
    sealedSteerAssistant,
    streamItems,
    useDbActivePayload,
  ])

  if (!msg) return null

  return (
    <StreamingMessage
      msg={msg}
      dataKey={dataKey}
      chatId={chatId}
      onAnswer={onAnswer}
      onAnswerPrepare={onAnswerPrepare}
      onResume={onResume}
      onInternalNav={onInternalNav}
      autoResumeEnabled={autoResumeEnabled}
      autoResumeAvailable={autoResumeAvailable}
      autoResumeSaving={autoResumeSaving}
      autoResumeError={autoResumeError}
      onAutoResumeChange={onAutoResumeChange}
      limitResetElapsed={limitResetElapsed}
      submissionBlocked={submissionBlocked}
      liveQuestionId={liveQuestionId}
      pendingQuestionRef={pendingQuestionRef}
      resumeCardRef={resumeCardRef}
      isStreaming={isStreaming}
    />
  )
}

export default memo(ActiveAssistantSurface)
