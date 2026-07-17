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
  return (
    <li
      className="chat__msg chat__msg--assistant"
      data-key={dataKey}
      data-active-assistant="true"
    >
      <MsgContent
        msg={msg}
        chatId={chatId}
        onQuestionAnswer={onAnswer}
        onResume={onResume}
        onInternalNav={onInternalNav}
        autoResumeEnabled={autoResumeEnabled}
        autoResumeAvailable={autoResumeAvailable}
        autoResumeSaving={autoResumeSaving}
        autoResumeError={autoResumeError}
        onAutoResumeChange={onAutoResumeChange}
        submissionBlocked={submissionBlocked}
        isLastMsg
        liveQuestionId={liveQuestionId}
        isActiveAnswer
        isStreaming={isStreaming}
        suppressedQuestionKeys={null}
      />
    </li>
  )
}
