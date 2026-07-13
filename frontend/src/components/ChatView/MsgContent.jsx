import { memo, useEffect, useState } from 'react'
import { ProgressiveMarkdown, StandardMarkdown } from './markdown/BlockRenderer.jsx'
import ToolBlock from './ToolBlock.jsx'
import ToolActivityGroup from './ToolActivityGroup.jsx'
import { groupToolRuns, coalesceThinkingEntries } from './groupBlocks.js'
import QuestionCard from './QuestionCard.jsx'
import Attachments from './Attachments.jsx'
import CompactionCard from './CompactionCard.jsx'
import { questionKey } from './questionKey.js'
import { suppressedQuestionToolIndices, thinkingElapsedMs } from './streamReducers.js'
import { stripAugmentation } from './msgText.js'
import ErrorCard from './ErrorCard.jsx'
import { assistantBlockKey } from './streamPromotion.js'


function thoughtSeconds(durationMs) {
  if (!Number.isFinite(durationMs)) return null
  return Math.max(1, Math.round(durationMs / 1000))
}


function thoughtLine(durationMs) {
  const seconds = thoughtSeconds(durationMs)
  if (!seconds) return '> Thought'
  return `> Thought for ${seconds} ${seconds === 1 ? 'second' : 'seconds'}`
}


function formatSeconds(seconds) {
  if (!Number.isFinite(seconds)) return null
  return `${seconds} ${seconds === 1 ? 'second' : 'seconds'}`
}


function ActiveThinkingDisclosure({ block, isStreaming }) {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!isStreaming) return undefined
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [isStreaming])

  // Live items carry the runner-clock anchors used by thinkingElapsedMs.
  // Persisted partials may only have duration_ms, so the shared renderer
  // freezes at that durable value until live data becomes the selected source.
  const elapsedMs = isStreaming ? thinkingElapsedMs(block, now) : block.duration_ms
  const seconds = thoughtSeconds(elapsedMs)
  const secondsText = formatSeconds(seconds)
  const label = isStreaming
    ? `> Thinking for ${secondsText || 'a moment'}`
    : secondsText
      ? `> Thought for ${secondsText}`
      : '> Thought'

  return (
    <details className="chat__reasoning">
      <summary className="chat__reasoning-summary">
        <span className="chat__reasoning-line">
          {label}
          {isStreaming && (
            <span className="chat__reasoning-ellipsis" aria-hidden="true">
              <span />
              <span />
              <span />
            </span>
          )}
        </span>
      </summary>
      <div className="chat__reasoning-body">
        <StandardMarkdown text={block.content || ''} />
      </div>
    </details>
  )
}


// Answerability is purely a function of the block + its position + live hint.
// Computing it here (rather than passing an arrow from ChatView's render loop)
// lets React.memo skip re-renders for non-last messages on every streaming
// tick — the only message that changes during streaming is the streaming <li>
// itself, not the static history above it.
function blockAnswerable(block, { msg, isLastMsg, liveQuestionId, onQuestionAnswer }) {
  return !!(
    onQuestionAnswer
    && msg.role === 'assistant'
    && block?.type === 'question'
    && isLastMsg
    && !block.answers
    && (!liveQuestionId || block.question_id === liveQuestionId)
  )
}

function MsgContentInner({
  msg,
  chatId,
  onQuestionAnswer,
  // Resume a turn paused by a drain-gated restart (or interrupted by a crash):
  // a stable send callback that re-sends a short "continue". Only the tail
  // interrupt note (a resumable error block on the last message) shows the
  // button. Compared in the memo below, so pass a stable reference.
  onResume,
  // isLastMsg + liveQuestionId are primitive props so memo can do a stable
  // shallow comparison; an inline isQuestionAnswerable arrow would hand memo a
  // fresh function reference every render and defeat it.
  isLastMsg,
  liveQuestionId,
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

  if (msg.blocks && msg.blocks.length > 0) {
    // The persisted transcript keeps the raw AskUserQuestion tool block
    // AND the question card (backend events.process_event appends both);
    // the live stream absorbs the tool twin into the card. Skip the twin
    // here so a reopened chat matches the live view — render-time, so it
    // also cleans up already-persisted old chats with no backend migration.
    const skipToolIdx = suppressedQuestionToolIndices(msg.blocks)

    // Render a single block by type. Pulled out of the map so both the plain
    // path and grouped tool runs (below) share one renderer.
    const renderBlock = (block, i) => {
      if (block.type === 'text') {
        const text = msg.role === 'user'
          ? stripAugmentation(block.content) : block.content
        if (!text) return null
        return (
          <div key={assistantBlockKey(block, i)} className={`chat__text chat__text--${msg.role}`}>
            {msg.role === 'assistant'
              ? (isActiveAnswer
                  ? <ProgressiveMarkdown
                      text={text}
                      isStreaming={isStreaming && i === msg.blocks.length - 1}
                    />
                  : <StandardMarkdown text={text} />)
              : text}
          </div>
        )
      }
      if (block.type === 'tool') {
        // Key a lone tool by its persisted `tool_use_id` (lever 2a) so the block
        // survives the live↔DB surface switch and a catch-up commit by identity.
        // Fall back to `t-<firstIdx>` for a legacy/tokenless block — the SAME
        // scheme the group wrapper below uses — so a message that renders one
        // tool and then (on a later render carrying a second adjacent tool)
        // folds it into a group updates the slot in place instead of
        // delete+insert. `i` is the block's index = the group's first index, and
        // the group wrapper prefers the same tool's id, so the keys coincide.
        return (
          <div key={assistantBlockKey(block, i)} className="chat__tools">
            {/* chatId + the block's tool_use_id let ToolBlock lazily fetch a
                truncated large output on expand (GET
                /tool-output/{tool_use_id}). */}
            <ToolBlock t={block} chatId={chatId} />
          </div>
        )
      }
      if (block.type === 'thinking') {
        if (isActiveAnswer) {
          return (
            <ActiveThinkingDisclosure
              key={assistantBlockKey(block, i)}
              block={block}
              isStreaming={isStreaming && i === msg.blocks.length - 1}
            />
          )
        }
        return (
          <details key={assistantBlockKey(block, i)} className="chat__reasoning">
            <summary className="chat__reasoning-summary">
              <span className="chat__reasoning-line">
                {thoughtLine(block.duration_ms)}
              </span>
            </summary>
            <div className="chat__reasoning-body">
              <StandardMarkdown text={block.content || ''} />
            </div>
          </details>
        )
      }
      if (block.type === 'question') {
        // Suppress if this exact question is currently live in
        // streamItems — the streaming <li> is already rendering it.
        // This prevents the duplicate card that appears when the
        // bridge gate retires and the SSE catch-up burst fires a
        // `question` event into both the persisted message and
        // streamItems simultaneously.
        if (suppressedQuestionKeys?.has(questionKey(block))) return null
        const answers = block.answers
        // Only the LAST block's question is answerable (see
        // isQuestionAnswerable in ChatView). Recovery keeps a still-open
        // question at the tail; if anything later follows it, the turn has
        // moved on and the card is transcript history.
        const isTailBlock = i === msg.blocks.length - 1
        const answerable = !!(
          onQuestionAnswer && isTailBlock && isQuestionAnswerable?.(block)
        )
        return (
          <div key={assistantBlockKey(block, i)}>
            <QuestionCard
              questions={block.questions || []}
              questionId={block.question_id}
              answeredMap={answers}
              onAnswer={answerable ? onQuestionAnswer : undefined}
              disabled={!answerable && !answers}
            />
          </div>
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
        // button. One tap re-sends a short "continue" as a normal visible
        // send; on a park the button reads "Resume now" (design §2.4).
        const resumable = !!(block.resumable && isLastMsg && onResume)
        const parked = !!block.pause?.resets_at
        return (
          <ErrorCard key={assistantBlockKey(block, i)} block={block}>
            {resumable && (
              <button
                type="button"
                className="chat__resume"
                onClick={() => onResume('continue')}
              >
                {parked ? 'Resume now' : 'Resume'}
              </button>
            )}
          </ErrorCard>
        )
      }
      return null
    }

    // Skip the AskUserQuestion tool twin, then fold runs of adjacent tool
    // blocks into one Activity card. Live items arrive here after conversion
    // to the same block shape, so the transcript doesn't reshuffle on promote.
    const entries = msg.blocks
      .map((block, i) => ({ item: block, idx: i }))
      .filter(({ idx }) => !skipToolIdx.has(idx))
    // Repair already-persisted transcripts where a continuous reasoning pass was
    // fragmented into many thinking blocks: coalesce runs of adjacent thinking
    // AFTER idx assignment (each survivor keeps its original persisted idx) so a
    // tokenless tool block's `t-<idx>` React key stays stable across renders.
    // See groupBlocks.coalesceThinkingEntries.
    const nodes = groupToolRuns(coalesceThinkingEntries(entries))

    return (
      <>
        {msg.role === 'user' && <Attachments attachments={msg.attachments} chatId={chatId} />}
        {nodes.map(node => {
          if (node.group) {
            const tools = node.group.map(e => e.item)
            // Prefer the first tool's `tool_use_id` (lever 2a); fall back to
            // `t-<firstIdx>` — the SAME key a lone tool at that index uses (see
            // renderBlock), so a single→group transition updates this slot in
            // place rather than swapping keys and forcing a delete+insert. Each
            // grouped ToolBlock is keyed by its own id so a catch-up commit
            // reconciles each block by identity within the group.
            return (
              <div
                key={assistantBlockKey(node.group[0].item, node.group[0].idx)}
                className="chat__tools"
              >
                <ToolActivityGroup tools={tools}>
                  {node.group.map(e => (
                    <ToolBlock
                      key={assistantBlockKey(e.item, e.idx)}
                      t={e.item}
                      chatId={chatId}
                    />
                  ))}
                </ToolActivityGroup>
              </div>
            )
          }
          return renderBlock(node.single.item, node.single.idx)
        })}
      </>
    )
  }

  const text = msg.role === 'user' && msg.content
    ? stripAugmentation(msg.content) : msg.content

  return (
    <>
      {msg.role === 'user' && <Attachments attachments={msg.attachments} chatId={chatId} />}
      {text ? (
        <div
          key={isActiveAnswer ? 0 : undefined}
          className={`chat__text chat__text--${msg.role}`}
        >
          {msg.role === 'assistant'
            ? (isActiveAnswer
                ? <ProgressiveMarkdown text={text} isStreaming={isStreaming} />
                : <StandardMarkdown text={text} />)
            : text}
        </div>
      ) : null}
    </>
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
    && prev.onQuestionAnswer === next.onQuestionAnswer
    && prev.onResume === next.onResume
    && prev.isLastMsg === next.isLastMsg
    && prev.liveQuestionId === next.liveQuestionId
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
