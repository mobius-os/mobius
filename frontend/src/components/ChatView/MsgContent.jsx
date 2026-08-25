import { memo } from 'react'
import { Switch } from '@openai/apps-sdk-ui/components/Switch'
import { ProgressiveMarkdown, StandardMarkdown } from './markdown/BlockRenderer.jsx'
import ActivityStretch from './ActivityStretch.jsx'
import { groupActivityRuns, coalesceThinkingEntries } from './groupBlocks.js'
import QuestionCard from './QuestionCard.jsx'
import SecureInputCard from './SecureInputCard.jsx'
import MessageSources from './MessageSources.jsx'
import Attachments from './Attachments.jsx'
import CompactionCard from './CompactionCard.jsx'
import ContinuationCard from './ContinuationCard.jsx'
import { isContinuationMessage } from './chatRuntimeState.js'
import { questionKey } from './questionKey.js'
import {
  repairInterleavedQuestionText,
  suppressedQuestionToolIndices,
} from './streamReducers.js'
import { stripAugmentation } from './msgText.js'
import ErrorCard from './ErrorCard.jsx'
import ContextCompactionMarker from './ContextCompactionMarker.jsx'
import { assistantBlockKey } from './streamPromotion.js'
import { copyAssistantSelection } from './markdownClipboard.js'
import { goalMessageObjectiveFromText } from './goalProgress.js'
import GoalHistoryCard from './GoalHistoryCard.jsx'


// Answerability is purely a function of the block + its position + live hint.
// Computing it here (rather than passing an arrow from ChatView's render loop)
// lets React.memo skip re-renders for non-last messages on every streaming
// tick — the only message that changes during streaming is the streaming <li>
// itself, not the static history above it.
function blockAnswerable(block, { msg, isLastMsg, liveQuestionId, onQuestionAnswer }) {
  // Answerable iff this block IS the chat's durable open question
  // (liveQuestionId = pending_question_id) and still unanswered — matched by id,
  // not by block position, so a card trailed by parallel output or a terminal
  // error stays answerable. liveQuestionId clears when the question is answered
  // or the turn ends, so nothing is answerable once it's null.
  return !!(
    onQuestionAnswer
    && msg.role === 'assistant'
    && block?.type === 'question'
    && isLastMsg
    && !block.answers
    && liveQuestionId
    && block.question_id === liveQuestionId
  )
}

function AssistantCopySurface({ msg, markdownByIndex, children }) {
  if (msg.role !== 'assistant') return children

  function markdownForBlock(element) {
    const index = Number(element.dataset.assistantMarkdownBlock)
    // A supplied map is authoritative, including a missing entry. Streaming
    // and cold-rendered blocks intentionally omit source until the whole block
    // is visible; falling back to msg.content here would copy the hidden tail.
    if (markdownByIndex) return markdownByIndex.get(index) ?? ''
    return msg.content ?? ''
  }

  return (
    <div
      className="chat__assistant-copy-surface"
      data-reply-source="assistant"
      onCopy={(event) => copyAssistantSelection(event, markdownForBlock)}
    >
      {children}
    </div>
  )
}

function UserMessageText({ text }) {
  const goalObjective = goalMessageObjectiveFromText(text)
  if (!goalObjective) return text

  return (
    <span className="chat__goal-message">
      <span className="chat__goal-message-tag" aria-hidden="true">Goal</span>
      <span className="chat__sr-only">Goal: </span>
      <span className="chat__goal-message-objective">{goalObjective}</span>
    </span>
  )
}

function GoalHistory({ msg }) {
  if (msg.role !== 'assistant' || !Array.isArray(msg.goal_summaries)) return null
  return msg.goal_summaries.map(summary => (
    <GoalHistoryCard key={summary.id} summary={summary} />
  ))
}

function MsgContentInner({
  msg,
  chatId,
  messageKey,
  onQuestionAnswer,
  // Resume a turn paused by a drain-gated restart (or interrupted by a crash):
  // a stable send callback that re-sends a short "continue". Only the tail
  // interrupt note (a resumable error block on the last message) shows the
  // button. Compared in the memo below, so pass a stable reference.
  onResume,
  onInternalNav,
  autoResumeEnabled,
  autoResumeAvailable,
  autoResumeSaving,
  autoResumeError,
  onAutoResumeChange,
  submissionBlocked = false,
  // isLastMsg + liveQuestionId are primitive props so memo can do a stable
  // shallow comparison; an inline isQuestionAnswerable arrow would hand memo a
  // fresh function reference every render and defeat it.
  isLastMsg,
  liveQuestionId,
  // Callback refs (ChatView's useNudgeTargetRef) that publish the DOM node of
  // the card each footer nudge watches. This renderer serves BOTH surfaces of
  // the active answer — the durable message row and the live streaming <li> —
  // so a mid-turn surface handoff reaches the observer as a plain node swap
  // instead of leaving it bound to a node React has detached. Only the card
  // that actually blocks the turn publishes: the answerable tail question and
  // the resumable tail note.
  pendingQuestionRef,
  resumeCardRef,
  // Active answers always use this renderer for both their DB partial and
  // live SSE payload. isStreaming only enables the cursor, aria-live, and the
  // active thinking timer; it never selects a different component tree.
  isActiveAnswer = false,
  isStreaming = false,
  // Set of questionKey strings currently live in streamItems. When
  // non-null, any question block whose key appears here is already
  // rendered by the streaming <li> and should be suppressed to prevent
  // the duplicate card that arises when the bridge gate retires mid-turn
  // and the SSE catch-up re-emits the same question event into both
  // the persisted message and streamItems.
  suppressedQuestionKeys,
}) {
  const autoResumeSwitchId = chatId ? `auto-resume-${chatId}` : undefined
  // Build a stable per-render answerable predicate that closes over the
  // scalar props (no function prop needed from ChatView).
  const isQuestionAnswerable = (block) =>
    blockAnswerable(block, { msg, isLastMsg, liveQuestionId, onQuestionAnswer })
  if (msg.kind === 'compaction') {
    // Render the compaction as its own labeled card rather than the generic
    // ToolBlock — see CompactionCard. The stored `content` is untouched, so
    // chat.py's `_latest_compaction_brief` still replays the same text.
    return (
      <div className="chat__tools">
        <CompactionCard msg={msg} />
      </div>
    )
  }

  if (isContinuationMessage(msg)) {
    return <ContinuationCard msg={msg} />
  }

  if (msg.blocks && msg.blocks.length > 0) {
    // Repair the one historical malformed sequence produced when a provider's
    // authoritative completion lost identity across request_user_input. This
    // is render-time as well as reducer-time so already-saved chats self-heal
    // without rewriting partner transcripts.
    const displayBlocks = repairInterleavedQuestionText(msg.blocks)
    // The persisted transcript keeps the raw AskUserQuestion tool block
    // AND the question card (backend events.process_event appends both);
    // the live stream absorbs the tool twin into the card. Skip the twin
    // here so a reopened chat matches the live view — render-time, so it
    // also cleans up already-persisted old chats with no backend migration.
    const skipToolIdx = suppressedQuestionToolIndices(displayBlocks)

    // Entry idx is the POST-suppression position, not the raw msg.blocks
    // ordinal. The two surfaces of the active answer disagree about the twin:
    // the live reducer absorbs the AskUserQuestion tool into the question card,
    // while the DB partial keeps both and relies on the render-time skip above.
    // Raw ordinals therefore differ by one for every block after a mid-turn
    // question, and an ordinal-keyed entry (thinking, a tokenless tool, the
    // question itself) would change key across the live↔DB surface switch —
    // remounting its stretch, collapsing what the user had expanded, and
    // dropping nested lazy-fetch state. Numbering AFTER the skip makes both
    // surfaces produce identical positions for identical visible content, so
    // keys survive the switch. (Appends only ever extend the tail, so earlier
    // positions — and their keys — are stable mid-run too.)
    const entries = displayBlocks
      .map((block, i) => ({ item: block, rawIdx: i }))
      .filter(({ rawIdx }) => !skipToolIdx.has(rawIdx))
      .map(({ item }, pos) => ({ item, idx: pos }))
    // Repair already-persisted transcripts where a continuous reasoning pass was
    // fragmented into many thinking blocks: coalesce runs of adjacent thinking
    // AFTER idx assignment (each survivor keeps its first fragment's position)
    // so a tokenless tool block's `t-<idx>` React key stays stable across
    // renders. Fragmented thinking exists only in legacy saved chats, which are
    // never a live surface, so coalescing after renumbering cannot reintroduce
    // a cross-surface position mismatch. See groupBlocks.coalesceThinkingEntries.
    const finalEntries = coalesceThinkingEntries(entries)
    // The rendered tail's entry idx — the anchor for "is this block the tail"
    // checks below. msg.blocks.length would be wrong here: a skipped twin means
    // the last VISIBLE block's idx is smaller than the raw block count.
    const lastEntryIdx = finalEntries.length
      ? finalEntries[finalEntries.length - 1].idx
      : -1
    const assistantMarkdownByIndex = new Map(
      msg.role !== 'assistant' ? [] : finalEntries.flatMap(({ item, idx }) => {
        if (item.type !== 'text' || !item.content) return []
        const coldFraction = Number(item._coldRenderFraction)
        const fullyRendered = !(
          Number.isFinite(coldFraction) && coldFraction > 0 && coldFraction < 1
        )
        return fullyRendered ? [[idx, item.content]] : []
      }),
    )

    // Render a stretch-breaking block by type. Activity entries (tool/thinking)
    // are folded into an ActivityStretch below; this renders only the `single`
    // nodes — text, question, error, and provider context compaction.
    const renderBlock = (block, i) => {
      if (block.type === 'activity' && Array.isArray(block.entries)) {
        return (
          <div
            key={block.activity_id || `activity-${i}`}
            className="chat__tools"
          >
            <ActivityStretch
              entries={block.entries}
              chatId={chatId}
              live={false}
              surfaceKey={messageKey}
              detailRef={{
                message_index: block.message_index,
                start: block.start,
                end: block.end,
              }}
              summaryToolCount={block.tool_count}
            />
          </div>
        )
      }
      if (block.type === 'text') {
        const text = msg.role === 'user'
          ? stripAugmentation(block.content) : block.content
        if (!text) return null
        return (
          <div
            key={assistantBlockKey(block, i)}
            className={`chat__text chat__text--${msg.role}`}
            data-assistant-markdown-block={msg.role === 'assistant' ? i : undefined}
          >
            {msg.role === 'assistant'
              ? (isActiveAnswer
                  ? <ProgressiveMarkdown
                      text={text}
                      isStreaming={isStreaming && i === lastEntryIdx}
                      onInternalNav={onInternalNav}
                      mediaDimensions={msg.media_dimensions}
                    />
                  : <StandardMarkdown
                      text={text}
                      renderFraction={block._coldRenderFraction}
                      onInternalNav={onInternalNav}
                      mediaDimensions={msg.media_dimensions}
                    />)
              : msg.role === 'user' ? <UserMessageText text={text} /> : text}
          </div>
        )
      }
      if (block.type === 'context_compaction') {
        return (
          <ContextCompactionMarker
            key={assistantBlockKey(block, i)}
          />
        )
      }
      // tool + thinking blocks never reach renderBlock: groupActivityRuns folds
      // every contiguous run of them (including a lone one) into a group node,
      // rendered by ActivityStretch below. renderBlock only sees the block types
      // that BREAK a stretch — text/compaction (above), question, and error.
      if (block.type === 'question') {
        // Suppress if this exact question is currently live in
        // streamItems — the streaming <li> is already rendering it.
        // This prevents the duplicate card that appears when the
        // bridge gate retires and the SSE catch-up burst fires a
        // `question` event into both the persisted message and
        // streamItems simultaneously.
        if (suppressedQuestionKeys?.has(questionKey(block))) return null
        const answers = block.answers
        // Answerable when it is the open question on the latest turn —
        // position-independent. A still-open card can be trailed by parallel
        // tool/subagent output or a terminal error; openness is identity, not
        // tail position. blockAnswerable scopes to isLastMsg + the live id, and
        // the backend agrees via app.chat.open_question_id.
        const answerable = !!(
          onQuestionAnswer && isQuestionAnswerable?.(block)
        )
        return (
          <div key={assistantBlockKey(block, i)}>
            <QuestionCard
              chatId={chatId}
              questions={block.questions || []}
              questionId={block.question_id}
              answeredMap={answers}
              onAnswer={answerable ? onQuestionAnswer : undefined}
              disabled={!answerable && !answers}
              pendingCardRef={answerable ? pendingQuestionRef : undefined}
            />
          </div>
        )
      }
      if (block.type === 'secure_input') {
        return (
          <SecureInputCard
            key={block.request_id || `secure-input-${i}`}
            block={block}
            chatId={chatId}
            interactive={isActiveAnswer && isStreaming}
          />
        )
      }
      if (block.type === 'error') {
        // Errors persist as their own block (backend's
        // process_event for 'error' events). Read as a system
        // notice rather than an agent reply — distinct
        // styling, no agent bubble — so the user can tell at
        // a glance the agent didn't author it. Without this
        // branch the block rendered to null and the error
        // vanished on chat return; that was the bug.
        //
        // The card body (label, park/pause classification, reset line) is
        // ErrorCard — this SAME block renderer consumes the live payload too,
        // so the two sources cannot diverge. Only the Resume button
        // lives here: a turn paused by a drain-gated restart (or interrupted
        // by a crash) persists a `resumable` note (backend reconcile marks
        // it), and only the TAIL note on the last message is resumable —
        // mirrors how a question card is only answerable at the tail — so
        // scrolled-back history and live provider errors never show a Resume
        // button. One tap opens a provider continuation turn and persists a
        // product marker rather than attributing the internal prompt to the
        // owner; on a park the button reads "Resume now" (design §2.4).
        const resumable = !!(block.resumable && isLastMsg && onResume)
        const parked = !!block.pause?.resets_at
        return (
          <ErrorCard
            key={assistantBlockKey(block, i)}
            block={block}
            autoResume={resumable && parked && !!autoResumeEnabled}
          >
            {resumable && parked && autoResumeAvailable && onAutoResumeChange && (
              <div className="chat__limit-option">
                <div className="chat__limit-option-copy">
                  <label
                    className="chat__limit-option-label"
                    htmlFor={autoResumeSwitchId}
                  >
                    Automatically continue after usage limits
                  </label>
                </div>
                <Switch
                  className="chat-policy-switch"
                  id={autoResumeSwitchId}
                  checked={!!autoResumeEnabled}
                  onCheckedChange={onAutoResumeChange}
                  disabled={!!autoResumeSaving || submissionBlocked}
                />
                {autoResumeError && (
                  <span className="chat__limit-option-error" role="alert">
                    {autoResumeError}
                  </span>
                )}
              </div>
            )}
            {resumable && (
              <button
                type="button"
                className="chat__resume"
                // Publish ONLY the tail note's button. `resumable` gates on
                // isLastMsg, not on tail position, so a last message holding
                // two resumable error blocks renders two buttons — and one
                // shared callback ref has ONE slot: the later mount wins, then
                // an unmount of the OTHER button calls the ref with null and
                // the cue goes dark while the real target is still on screen
                // and offscreen. The tail is also exactly what arms the cue
                // (ChatView's tailResumableBlock), so gating here keeps the
                // observed node and the `active` flag talking about the same
                // block. The old querySelectorAll lookup took `.pop()`, which
                // is what this reproduces.
                ref={i === lastEntryIdx ? resumeCardRef : undefined}
                onClick={() => onResume('continue', {
                  continuation: 'manual',
                  pin: false,
                })}
                disabled={submissionBlocked}
                title={submissionBlocked
                  ? 'Wait for the provider switch to finish.'
                  : undefined}
              >
                {parked ? 'Resume now' : 'Resume'}
              </button>
            )}
          </ErrorCard>
        )
      }
      return null
    }

    // Fold contiguous runs of thinking AND tool blocks into one activity
    // stretch. Live items arrive here after conversion to the same block shape,
    // so the transcript doesn't reshuffle on promote.
    const nodes = groupActivityRuns(finalEntries)

    return (
      <AssistantCopySurface msg={msg} markdownByIndex={assistantMarkdownByIndex}>
        {msg.role === 'user' && <Attachments attachments={msg.attachments} chatId={chatId} />}
        {nodes.map((node, nodeIdx) => {
          if (node.group) {
            // A stretch is LIVE only when it's the trailing node of the
            // active answer while the TURN is running — the agent is working
            // in it right now (label shimmer). isStreaming here carries turn
            // liveness, not payload-source choice: a DB partial shown through
            // the reconnect catch-up window is still live (see ChatView's
            // isStreaming prop). A settled stretch above the tail never
            // re-renders on its own.
            const live = isActiveAnswer && isStreaming && nodeIdx === nodes.length - 1
            // Key the stretch by its FIRST entry (assistantBlockKey): a
            // thinking-first stretch keeps its thinking idx, a tool-first stretch
            // its `tool_use_id`/`t-<idx>`, so a single→group / live↔DB /
            // mid-run-growth transition updates this slot in place rather than
            // swapping keys and forcing a delete+insert. Each entry inside the
            // stretch keeps its own key so a catch-up commit reconciles by
            // identity.
            return (
              <div
                key={assistantBlockKey(node.group[0].item, node.group[0].idx)}
                className="chat__tools"
              >
                <ActivityStretch
                  entries={node.group}
                  chatId={chatId}
                  live={live}
                  surfaceKey={messageKey}
                  onInternalNav={onInternalNav}
                />
              </div>
            )
          }
          return renderBlock(node.single.item, node.single.idx)
        })}
        {/* Web sources collected from the turn's tool blocks and shown once
            after the answer. Memory keeps its own richer lookup card inline. */}
        {msg.role === 'assistant' && !isStreaming && (
          <MessageSources
            blocks={msg.blocks}
            chatId={chatId}
            sourceRef={msg.source_ref}
            disclosureKey={`${messageKey}:references`}
          />
        )}
        {!isStreaming && <GoalHistory msg={msg} />}
      </AssistantCopySurface>
    )
  }

  const text = msg.role === 'user' && msg.content
    ? stripAugmentation(msg.content) : msg.content

  return (
    <AssistantCopySurface msg={msg}>
      {msg.role === 'user' && <Attachments attachments={msg.attachments} chatId={chatId} />}
      {text ? (
        <div
          key={isActiveAnswer ? 0 : undefined}
          className={`chat__text chat__text--${msg.role}`}
          data-assistant-markdown-block={msg.role === 'assistant' ? 0 : undefined}
        >
          {msg.role === 'assistant'
            ? (isActiveAnswer
                ? <ProgressiveMarkdown
                    text={text}
                    isStreaming={isStreaming}
                    onInternalNav={onInternalNav}
                    mediaDimensions={msg.media_dimensions}
                  />
                : <StandardMarkdown
                    text={text}
                    onInternalNav={onInternalNav}
                    mediaDimensions={msg.media_dimensions}
                  />)
            : msg.role === 'user' ? <UserMessageText text={text} /> : text}
        </div>
      ) : null}
      {!isStreaming && <GoalHistory msg={msg} />}
    </AssistantCopySurface>
  )
}

// Memoize by shallow prop comparison. The comparison below skips re-renders
// when neither the message content nor the answerability scalars changed.
// During streaming, only the last <li>'s props change; the history above is
// stable and skips entirely.
//
// isLastMsg is intentionally compared as a scalar so that the last-message
// becoming non-last (when a new message arrives) triggers a re-render to
// mark the previous question card as no-longer-answerable.
export default memo(MsgContentInner, (prev, next) => {
  return (
    prev.msg === next.msg
    && prev.chatId === next.chatId
    && prev.messageKey === next.messageKey
    && prev.onQuestionAnswer === next.onQuestionAnswer
    && prev.onResume === next.onResume
    && prev.onInternalNav === next.onInternalNav
    && prev.autoResumeEnabled === next.autoResumeEnabled
    && prev.autoResumeAvailable === next.autoResumeAvailable
    && prev.autoResumeSaving === next.autoResumeSaving
    && prev.autoResumeError === next.autoResumeError
    && prev.onAutoResumeChange === next.onAutoResumeChange
    && prev.submissionBlocked === next.submissionBlocked
    && prev.isLastMsg === next.isLastMsg
    && prev.liveQuestionId === next.liveQuestionId
    // The nudge refs are props, so an exhaustive comparator has to include
    // them: a ref whose identity changed must reach the DOM node, or React
    // never re-invokes it and the observer keeps the old channel. In practice
    // they never change (useNudgeTargetRef memoizes both with []), which is
    // why these two lines cost nothing. Skipping a re-render does NOT
    // unpublish anything — React only re-runs a callback ref when the ref or
    // the host node changes, and a bailed-out render changes neither.
    && prev.pendingQuestionRef === next.pendingQuestionRef
    && prev.resumeCardRef === next.resumeCardRef
    && prev.isActiveAnswer === next.isActiveAnswer
    && prev.isStreaming === next.isStreaming
    // suppressedQuestionKeys is a Set (new reference each render) or null.
    // Compare by size + content when both are Sets; treat null vs Set as unequal.
    // This is intentionally conservative — a false inequality triggers a
    // re-render for the affected message, which is rare and cheap.
    && (prev.suppressedQuestionKeys === next.suppressedQuestionKeys
        || (prev.suppressedQuestionKeys != null
            && next.suppressedQuestionKeys != null
            && prev.suppressedQuestionKeys.size === next.suppressedQuestionKeys.size
            && [...prev.suppressedQuestionKeys].every(k => next.suppressedQuestionKeys.has(k))))
  )
})
