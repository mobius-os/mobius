/* One reply owns live/saved source rows, continuous prose and its final
   References. The reply at the very end of the chat also holds the one
   footer: its time, Copy of everything the agent said since the owner last
   spoke, and Branch. */
import { Fragment, memo, useContext, useLayoutEffect, useMemo, useRef } from 'react'
import MsgContent from './MsgContent.jsx'
import MessageSources from './MessageSources.jsx'
import MessageMetaRow from './MessageMetaRow.jsx'
import { registerReplyProse, renderedStretchClipboard, replyProse } from './messageCopy.js'
import { carryDurableBlockState, streamItemsToAssistantPayload } from './streamPromotion.js'
import { projectSteerContinuationMessage } from './steerContinuity.js'
import { mergeProjectedActivity } from './peerTimeline.js'
import { presentAssistantReply, presentAssistantActivity, replyQuestionSuppression } from './assistantReplies.js'
import { PeerTimelineRows } from './PeerTimeline.jsx'
import { PeerTimelineContext } from './peerTimelineContext.js'

/** The first reply of the agent's stretch that ends with `group`: replies
 * back to the last visible row that is not a reply (an owner message or a
 * visible marker). Answered cards, waits and helper results are hidden
 * carriers, so the stretch runs on across them. */
export function agentStretchStart(messages, replyGroups, group) {
  let first = group
  for (;;) {
    let previous = first.start - 1
    while (previous >= 0 && messages[previous]?.hidden) previous -= 1
    const earlier = replyGroups.get(previous)
    if (!earlier || earlier === first) return first
    first = earlier
  }
}

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
  footer = null,
  stretchStartKey,
  stretchCopyable = false,
  onBranch,
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
  // A settled reply records its prose for the stretch's footer to copy.
  const prose = useMemo(() => (isStreaming ? null : {
    rowKeys: rows.map(row => row.key), parts: replyProse(rows.map(row => row.message)),
  }), [rows, isStreaming])
  const replyRef = useRef(null)
  useLayoutEffect(() => {
    const element = replyRef.current
    registerReplyProse(element, prose)
    return () => registerReplyProse(element, null)
  }, [prose])
  if (!msg) return null

  const lastVisibleRow = rows.findLastIndex(row => !row.message.hidden)
  const branchThroughId = rows[rows.length - 1].message.id
  const metaKey = presentationRows[0].key
  const settled = footer === 'settled' && !isStreaming
  return <PeerTimelineContext.Provider value={displayTimeline}><li
    ref={replyRef}
    className="chat__reply"
    data-reply-key={metaKey}
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
    {/* Only the chat's last reply has a footer; replies above it leave no
        gap. It is mounted while the agent works too, empty: settling fills
        the reserved line instead of adding one under a followed transcript. */}
    {footer && <MessageMetaRow
      timestamp={settled ? presentationRows[0].message.ts : null}
      copy={settled && stretchCopyable ? button => renderedStretchClipboard(
        button.closest('.chat__reply'), stretchStartKey,
      ) : undefined}
      // Branching continues from the whole settled reply: its last row, named
      // by the id the server stored it under.
      branch={settled && onBranch && branchThroughId != null
        ? () => onBranch(String(branchThroughId)) : undefined}
      side="start"
      visible
    />}
  </li></PeerTimelineContext.Provider>
}

export default memo(AssistantReply)
