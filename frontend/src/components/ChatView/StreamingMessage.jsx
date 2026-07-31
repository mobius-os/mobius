import MsgContent from './MsgContent.jsx'
import MessageMetaRow from './MessageMetaRow.jsx'
import { messageCopyText } from './messageCopy.js'


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
  pendingQuestionRef,
  resumeCardRef,
  isStreaming,
  messageMetaVisible,
  onMessageMetaClick,
}) {
  // A live answer is still changing under the reader, so copy appears only
  // after the turn settles.
  const copyText = isStreaming ? '' : messageCopyText(msg)

  return (
    <li
      className="chat__msg chat__msg--assistant"
      data-key={dataKey}
      data-active-assistant="true"
      onClick={copyText && onMessageMetaClick
        ? (event) => onMessageMetaClick(event, dataKey)
        : undefined}
    >
      <MsgContent
        msg={msg}
        chatId={chatId}
        messageKey={dataKey}
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
        pendingQuestionRef={pendingQuestionRef}
        resumeCardRef={resumeCardRef}
        isActiveAnswer
        isStreaming={isStreaming}
        suppressedQuestionKeys={null}
      />
      <MessageMetaRow
        copyText={copyText}
        visible={messageMetaVisible}
      />
    </li>
  )
}
