import { memo } from 'react'
import { StandardMarkdown } from './markdown/BlockRenderer.jsx'
import ToolBlock from './ToolBlock.jsx'
import QuestionCard from './QuestionCard.jsx'
import Attachments from './Attachments.jsx'
import CompactionCard from './CompactionCard.jsx'
import { questionKey } from './questionKey.js'
import { suppressedQuestionToolIndices } from './streamReducers.js'


function stripAugmentation(text) {
  let cleaned = text.replace(/\s*<agent_experience>[\s\S]*?<\/agent_experience>\s*/g, '')
  cleaned = cleaned.replace(/\s*\[Files in this session:\n[\s\S]*?\]\s*/g, '')
  return cleaned.trim()
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
  // isLastMsg + liveQuestionId replace the old inline isQuestionAnswerable
  // arrow so memo can do a stable shallow comparison instead of seeing a
  // fresh function reference every render.
  isLastMsg,
  liveQuestionId,
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
    return (
      <>
        {msg.role === 'user' && <Attachments attachments={msg.attachments} chatId={chatId} />}
        {msg.blocks.map((block, i) => {
          if (skipToolIdx.has(i)) return null
          if (block.type === 'text') {
            const text = msg.role === 'user'
              ? stripAugmentation(block.content) : block.content
            if (!text) return null
            return (
              <div key={i} className={`chat__text chat__text--${msg.role}`}>
                {msg.role === 'assistant'
                  ? <StandardMarkdown text={text} />
                  : text}
              </div>
            )
          }
          if (block.type === 'tool') {
            return (
              <div key={i} className="chat__tools">
                <ToolBlock t={block} />
              </div>
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
            // Only the LAST block's question is the one the runner is parked
            // on (see isQuestionAnswerable in ChatView). A question with any
            // block after it — the turn continued, or `reconcile` appended an
            // interrupted-turn note — is history, not the live prompt.
            const isTailBlock = i === msg.blocks.length - 1
            const answerable = !!(
              onQuestionAnswer && isTailBlock && isQuestionAnswerable?.(block)
            )
            return (
              <div key={i}>
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
            // Run the message through StandardMarkdown so URLs in
            // provider error responses (quota links, billing
            // pages) become clickable — agents' error payloads
            // typically include "Upgrade to Pro (https://...)" and
            // "purchase more credits at https://..." that the user
            // wants to tap straight from the chat.
            return (
              <div key={i} className="chat__text--error" role="alert">
                <span className="chat__error-label">Error</span>
                <StandardMarkdown
                  text={block.message || 'The agent ran into an issue.'}
                />
              </div>
            )
          }
          return null
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
        <div className={`chat__text chat__text--${msg.role}`}>
          {msg.role === 'assistant'
            ? <StandardMarkdown text={text} />
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
    && prev.isLastMsg === next.isLastMsg
    && prev.liveQuestionId === next.liveQuestionId
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
