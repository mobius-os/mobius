import MsgContent from './MsgContent.jsx'


/**
 * Stable row shell for the one active assistant answer.
 *
 * The DB partial and the live SSE payload both flow through MsgContent. This
 * wrapper never selects a renderer; it only owns the invariant DOM anchor the
 * scroll state machine resolves through `[data-key]`.
 */
export default function StreamingMessage({
  msg,
  dataKey,
  chatId,
  activityMessageId,
  activitySourceBlocks,
  onAnswer,
  onPrepareAnswer,
  onCancelAnswer,
  onResume,
  resumeState,
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
}) {
  return (
    <li
      className="chat__msg chat__msg--assistant"
      data-key={dataKey}
      data-active-assistant="true"
    >
      <MsgContent
        msg={msg}
        chatId={chatId}
      activityMessageId={activityMessageId}
      activitySourceBlocks={activitySourceBlocks}
        messageKey={dataKey}
        onQuestionAnswer={onAnswer}
        onQuestionSubmitIntent={onPrepareAnswer}
        onQuestionSubmitCancel={onCancelAnswer}
        onResume={onResume}
        resumeState={resumeState}
        onInternalNav={onInternalNav}
        autoResumeEnabled={autoResumeEnabled}
        autoResumeAvailable={autoResumeAvailable}
        autoResumeSaving={autoResumeSaving}
        autoResumeError={autoResumeError}
        onAutoResumeChange={onAutoResumeChange}
        limitResetElapsed={limitResetElapsed}
        submissionBlocked={submissionBlocked}
        isLastMsg
        liveQuestionId={liveQuestionId}
        pendingQuestionRef={pendingQuestionRef}
        resumeCardRef={resumeCardRef}
        isActiveAnswer
        isStreaming={isStreaming}
        suppressedQuestionKeys={null}
      />
    </li>
  )
}
