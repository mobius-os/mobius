import { StandardMarkdown } from './markdown/BlockRenderer.jsx'
import ToolBlock from './ToolBlock.jsx'
import QuestionCard from './QuestionCard.jsx'
import Attachments from './Attachments.jsx'
import { compactionToolBlock } from './compactionToolBlock.js'


function stripAugmentation(text) {
  let cleaned = text.replace(/\s*<agent_experience>[\s\S]*?<\/agent_experience>\s*/g, '')
  cleaned = cleaned.replace(/\s*\[Files in this session:\n[\s\S]*?\]\s*/g, '')
  return cleaned.trim()
}

export default function MsgContent({
  msg,
  chatId,
  onQuestionAnswer,
  isQuestionAnswerable,
}) {
  if (msg.kind === 'compaction') {
    return (
      <div className="chat__tools">
        <ToolBlock t={compactionToolBlock(msg, chatId)} />
      </div>
    )
  }

  if (msg.blocks && msg.blocks.length > 0) {
    return (
      <>
        {msg.role === 'user' && <Attachments attachments={msg.attachments} chatId={chatId} />}
        {msg.blocks.map((block, i) => {
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
