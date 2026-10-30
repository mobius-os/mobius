/* One reply owns live/saved source rows, continuous prose, its final References,
   and the one action row at its end: its time, Copy of its answer, and Try
   again where offered. */
import { Fragment, memo, useMemo, useContext } from 'react'
import MsgContent from './MsgContent.jsx'
import MessageSources from './MessageSources.jsx'
import MessageMetaRow from './MessageMetaRow.jsx'
import { renderedReplyClipboard, replyOutcome } from './messageCopy.js'
import { ownsRecoveryAction } from './recoveryCard.js'
import { carryDurableBlockState, streamItemsToAssistantPayload } from './streamPromotion.js'
import { projectSteerContinuationMessage } from './steerContinuity.js'
import { mergeProjectedActivity } from './peerTimeline.js'
import { presentAssistantReply, presentAssistantActivity, replyQuestionSuppression } from './assistantReplies.js'
import { PeerTimelineRows } from './PeerTimeline.jsx'
import { PeerTimelineContext } from './peerTimelineContext.js'

// Both the component and payload stay memoized: a composer edit must not
// recreate the expensive answer tree or its live-to-saved disclosure identities.
function AssistantReply({
  replyGroup,
  activeRowIndex = 0,
  activeMirrorMsg,
  useDbActivePayload,
  hasLivePayload,
  streamItems,
  activitySourceBlocks,
  sealedSteerAssistant,
  chatId,
  isStreaming = false,
  isLastMsg = true,
  metaVisible = false,
  onRevealMeta,
  onTryAgain,
  pendingQuestionRef,
  resumeCardRef,
  ...messageProps
}) {
  const timeline = useContext(PeerTimelineContext)
  const positions = timeline?.positions
  const presentationRows = replyGroup.presentationRows
  const msg = useMemo(() => {
    let source = null
    if (useDbActivePayload) {
      source = activeMirrorMsg
    } else if (hasLivePayload) {
      const livePayload = streamItemsToAssistantPayload(streamItems, { finalize: false })
      const blocks = mergeProjectedActivity(
        livePayload.blocks, activeMirrorMsg?.blocks || [], activitySourceBlocks || [],
      )
      source = {
        ...(activeMirrorMsg || presentationRows[activeRowIndex]?.message || {}),
        ...livePayload,
        role: 'assistant',
        blocks: carryDurableBlockState(blocks, activeMirrorMsg?.blocks || []),
      }
    }
    return presentationRows.length > 1 ? source : projectSteerContinuationMessage(
      sealedSteerAssistant, source, { active: isStreaming },
    )
  }, [activeMirrorMsg, activitySourceBlocks, hasLivePayload, isStreaming,
    sealedSteerAssistant, streamItems, useDbActivePayload, presentationRows, activeRowIndex])

  const sourceRows = useMemo(() => presentationRows.map((row, index) => (
    index === (activeRowIndex >= 0 ? activeRowIndex : presentationRows.length - 1)
      ? { ...row, message: msg || row.message } : row
  )), [presentationRows, activeRowIndex, msg])
  const proseRows = useMemo(() => presentAssistantReply(sourceRows, {
    activeIndex: isStreaming ? activeRowIndex : -1, positions,
  }), [sourceRows, activeRowIndex, isStreaming, positions])
  const activity = useMemo(() => presentAssistantActivity(proseRows, {
    activeIndex: isStreaming ? activeRowIndex : -1, positions,
  }), [proseRows, activeRowIndex, isStreaming, positions])
  const rows = activity.rows
  const displayTimeline = useMemo(() => ({ tools: timeline?.tools, positions: activity.positions }), [timeline?.tools, activity.positions])
  // A reply gets its actions once, at its end, only after it has settled.
  const outcome = useMemo(() => (
    isStreaming ? null : replyOutcome(rows.map(row => row.message))
  ), [rows, isStreaming])
  const answerText = outcome?.answer || ''
  if (!msg) return null

  const lastVisibleRow = rows.findLastIndex(row => !row.message.hidden)
  const metaKey = presentationRows[0].key
  // An error card that owns recovery already offers Try now or Resume. Any
  // other failure (a plain provider error) leaves Try again as the way on.
  const cardOwnsRecovery = ownsRecoveryAction({
    block: outcome?.endingError,
    // endingError is by definition the last row's last rendered entry.
    entryIndex: 0,
    lastEntryIndex: 0,
    isLastMessage: isLastMsg,
    canResume: !!messageProps.onResume,
  })
  const tryAgain = outcome && onTryAgain && !cardOwnsRecovery
    ? () => onTryAgain(metaKey) : undefined
  return <PeerTimelineContext.Provider value={displayTimeline}><li
    className="chat__reply"
    onClick={outcome && onRevealMeta ? event => onRevealMeta(event, metaKey) : undefined}
    // The whole active reply is the current response: activity presentation may
    // move its live tools into an earlier row than the streaming one.
    data-current-response={activeRowIndex >= 0 ? 'true' : undefined}
  >
    <ul className="chat__reply-rows" role="presentation">
      {rows.map((row, index) => {
        const active = index === activeRowIndex
        const tail = index === lastVisibleRow
        return <Fragment key={row.key}>
          <PeerTimelineRows notes={row.notes} chatId={chatId} onInternalNav={messageProps.onInternalNav} />
          {!row.message.hidden && <li
            className="chat__msg chat__msg--assistant"
            data-key={row.key}
            data-source-key={row.message.id !== row.key ? row.message.id : undefined}
            data-text-owner-key={row.message.reply_text_owner_key}
            data-anchor-key={row.anchorKey !== row.key ? row.anchorKey : undefined}
            data-active-assistant={active ? 'true' : undefined}
            tabIndex={-1}
          >
            <MsgContent
              {...messageProps}
              msg={row.message}
              chatId={chatId}
              messageKey={row.key}
              activityMessageId={row.message.id}
              activitySourceBlocks={active ? activitySourceBlocks : presentationRows[index].message.blocks}
              isStreaming={active && isStreaming}
              isActiveAnswer={active}
              isLastMsg={tail && isLastMsg}
              onResume={tail ? messageProps.onResume : undefined}
              autoResumeEnabled={tail && messageProps.autoResumeEnabled}
              autoResumeAvailable={tail && messageProps.autoResumeAvailable}
              autoResumeSaving={tail && messageProps.autoResumeSaving}
              autoResumeError={tail ? messageProps.autoResumeError : ''}
              onAutoResumeChange={tail ? messageProps.onAutoResumeChange : undefined}
              limitResetElapsed={tail && messageProps.limitResetElapsed}
              continuationWait={tail ? messageProps.continuationWait : null}
              handoff={tail ? messageProps.handoff : null}
              recoveryCredit={tail ? messageProps.recoveryCredit : null}
              pendingQuestionRef={pendingQuestionRef}
              resumeCardRef={resumeCardRef}
              // The selected row already owns the live/DB question source.
              // Suppressing its key would remove its only card on acceptance.
              suppressedQuestionKeys={replyQuestionSuppression(messageProps.suppressedQuestionKeys, activeRowIndex, index)}
            />
          </li>}
        </Fragment>
      })}
    </ul>
    {!isStreaming && <MessageSources
      chatId={chatId}
      groups={sourceRows.map(row => row.message.blocks)}
      refs={sourceRows.flatMap(row => row.message.source_ref ? [row.message.source_ref] : [])}
      disclosureKey={`${rows[lastVisibleRow].key}:references`}
    />}
    {/* Mounted while streaming too, empty: settling fills the reserved line
        instead of adding one under a followed transcript. */}
    <MessageMetaRow
      timestamp={outcome ? presentationRows[0].message.ts : null}
      copy={answerText ? button => renderedReplyClipboard(
        button.closest('.chat__reply'), rows.map(row => row.key), outcome.parts,
      ) : undefined}
      onTryAgain={tryAgain}
      side="start"
      visible={metaVisible}
    />
  </li></PeerTimelineContext.Provider>
}

export default memo(AssistantReply)
