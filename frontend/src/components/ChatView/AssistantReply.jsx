/* One reply owns live/saved source rows, continuous prose, and its final References. */
import { Fragment, memo, useMemo, useContext } from 'react'
import MsgContent from './MsgContent.jsx'
import { blockAnswerable } from './questionAnswerable.js'
import MessageSources from './MessageSources.jsx'
import { carryDurableBlockState, streamItemsToAssistantPayload } from './streamPromotion.js'
import { projectSteerContinuationMessage } from './steerContinuity.js'
import { mergeProjectedPeerActivity } from './peerTimeline.js'
import { presentAssistantReply, replyQuestionSuppression } from './assistantReplies.js'
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
  pendingQuestionRef,
  resumeCardRef,
  ...messageProps
}) {
  const positions = useContext(PeerTimelineContext)?.positions
  const msg = useMemo(() => {
    let source = null
    if (useDbActivePayload) {
      source = activeMirrorMsg
    } else if (hasLivePayload) {
      const livePayload = streamItemsToAssistantPayload(streamItems, { finalize: false })
      const blocks = mergeProjectedPeerActivity(
        livePayload.blocks, activeMirrorMsg?.blocks || [], activitySourceBlocks || [],
      )
      source = {
        ...(activeMirrorMsg || replyGroup.rows[activeRowIndex]?.message || {}),
        ...livePayload,
        role: 'assistant',
        blocks: carryDurableBlockState(blocks, activeMirrorMsg?.blocks || []),
      }
    }
    return replyGroup.rows.length > 1 ? source : projectSteerContinuationMessage(
      sealedSteerAssistant, source, { active: isStreaming },
    )
  }, [activeMirrorMsg, activitySourceBlocks, hasLivePayload, isStreaming,
    sealedSteerAssistant, streamItems, useDbActivePayload, replyGroup, activeRowIndex])

  const sourceRows = useMemo(() => replyGroup.rows.map((row, index) => (
    index === (activeRowIndex >= 0 ? activeRowIndex : replyGroup.rows.length - 1)
      ? { ...row, message: msg || row.message } : row
  )), [replyGroup, activeRowIndex, msg])
  const rows = useMemo(() => presentAssistantReply(sourceRows, {
    activeIndex: isStreaming ? activeRowIndex : -1, positions,
  }), [sourceRows, activeRowIndex, isStreaming, positions])
  if (!msg) return null

  const lastVisibleRow = rows.findLastIndex(row => !row.message.hidden)
  const tailMessage = rows[lastVisibleRow]?.message
  const openTailQuestion = !isStreaming && tailMessage?.blocks?.some(block => (
    blockAnswerable(block, {
      msg: tailMessage,
      isLastMsg,
      liveQuestionId: messageProps.liveQuestionId,
      onQuestionAnswer: messageProps.onQuestionAnswer,
    })
  ))
  const sources = !isStreaming && <MessageSources
    chatId={chatId}
    groups={sourceRows.map(row => row.message.blocks)}
    refs={sourceRows.flatMap(row => row.message.source_ref ? [row.message.source_ref] : [])}
    disclosureKey={`${rows[lastVisibleRow].key}:references`}
  />
  return <li className="chat__reply">
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
              activitySourceBlocks={active ? activitySourceBlocks : replyGroup.rows[index].message.blocks}
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
              recoveryCredit={tail ? messageProps.recoveryCredit : null}
              pendingQuestionRef={pendingQuestionRef}
              resumeCardRef={resumeCardRef}
              replySourcesBeforeQuestion={tail && openTailQuestion ? sources : null}
              // The selected row already owns the live/DB question source.
              // Suppressing its key would remove its only card on acceptance.
              suppressedQuestionKeys={replyQuestionSuppression(messageProps.suppressedQuestionKeys, activeRowIndex, index)}
            />
          </li>}
        </Fragment>
      })}
    </ul>
    {!openTailQuestion && sources}
  </li>
}

export default memo(AssistantReply)
