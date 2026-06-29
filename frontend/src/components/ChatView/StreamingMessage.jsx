import { ProgressiveMarkdown, StandardMarkdown } from './markdown/BlockRenderer.jsx'
import ToolBlock from './ToolBlock.jsx'
import QuestionCard from './QuestionCard.jsx'

/**
 * The single live `<li>` that renders the in-flight turn's
 * `streamItems` (text deltas, tool blocks, question cards, provider
 * errors) while the agent is streaming — i.e. the
 * `sending && streamItems.length > 0` branch of the message list.
 *
 * Relocated verbatim from ChatView's render. This is a pure leaf:
 * no refs, no effects, no scroll/spacer/queue logic. It exists only
 * to keep that vocabulary (the four block renderers) out of ChatView,
 * which no longer imports any of them.
 *
 * The `<li>` MUST keep `className="chat__msg chat__msg--assistant"`
   * and `data-key={dataKey}`: the scroll state machine
   * (useScrollMode's applyMode) resolves ANCHOR_AT via
   * `querySelector('.chat__msg[data-key]')`. `dataKey` is
   * `streamingDataKey` from ChatView — the kept DB partial's key in the
   * BRIDGE case, otherwise a synthetic `streaming-<chatId>` key. The
   * synthetic key lets foreground reconnect preserve the reader's position
   * inside a live streaming answer while catch-up fills content below.
 *
 * @param {object} props
 * @param {Array<object>} props.streamItems  the live stream items
 *   (latestItemsRef's render mirror): text/tool/question/error.
   * @param {string|undefined} props.dataKey  ChatView's stable
   *   `streamingDataKey`.
 * @param {(text: string, resolvedAnswers: object, questionId?: string) => void} props.onAnswer
 *   doSendSilent — submits an AskUserQuestion answer as a hidden
 *   user message. The card is clickable even mid-stream because the
 *   runner is paused on the AskUserQuestion future.
 */
export default function StreamingMessage({ streamItems, dataKey, onAnswer }) {
  return (
    <li
      className="chat__msg chat__msg--assistant"
      data-key={dataKey}
    >
      {streamItems.map((item, i) => {
        if (item.type === 'tool') {
          return (
            <div key={`s-${i}`} className="chat__tools">
              <ToolBlock t={item} />
            </div>
          )
        }
        if (item.type === 'question') {
          // QuestionCard tracks its own `submitted` state and
          // disables itself after the user answers. The agent
          // is paused on the AskUserQuestion future, so the
          // user MUST be able to click these chips even while
          // the turn is otherwise "streaming". No external
          // disabled gate.
          //
          // Pass item.answers as answeredMap so patchQuestionAnswers's
          // optimistic update is visible immediately when the question
          // is still in streamItems — without this, the card stays
          // in the interactive state even after the user submitted.
          return (
            <div key={`s-${i}`}>
              <QuestionCard
                questions={item.questions}
                questionId={item.question_id}
                answeredMap={item.answers}
                onAnswer={item.answers ? undefined : onAnswer}
              />
            </div>
          )
        }
        if (item.type === 'text') {
          const isLast = i === streamItems.length - 1
          return (
            <div key={`s-${i}`} className="chat__text chat__text--assistant">
              <ProgressiveMarkdown text={item.content} />
              {isLast && <span className="chat__cursor" />}
            </div>
          )
        }
        if (item.type === 'error') {
          // StandardMarkdown so URLs in provider errors
          // (quota links, billing pages) become clickable.
          // Same shape as the post-promote render in
          // MsgContent so the user gets the same affordance
          // before and after the streaming `<li>` is
          // replaced by the persisted message.
          return (
            <div key={`s-${i}`} className="chat__text--error" role="alert">
              <span className="chat__error-label">Error</span>
              <StandardMarkdown
                text={item.message || 'The agent ran into an issue.'}
              />
            </div>
          )
        }
        return null
      })}
    </li>
  )
}
