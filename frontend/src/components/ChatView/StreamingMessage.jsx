import { useEffect, useState } from 'react'
import { ProgressiveMarkdown, StandardMarkdown } from './markdown/BlockRenderer.jsx'
import ToolBlock from './ToolBlock.jsx'
import ToolActivityGroup from './ToolActivityGroup.jsx'
import { groupToolRuns } from './groupBlocks.js'
import QuestionCard from './QuestionCard.jsx'


function durationSeconds(durationMs) {
  if (!Number.isFinite(durationMs)) return null
  return Math.max(1, Math.round(durationMs / 1000))
}


function ThinkingDisclosure({ item, isActive }) {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!isActive) return undefined
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [isActive])

  const startedAt = Number.isFinite(item.startedAt) ? item.startedAt : now
  const activeSeconds = durationSeconds(Math.max(0, now - startedAt))
  const frozenSeconds = durationSeconds(item.duration_ms)
  const label = isActive ? 'Thinking' : 'Thought'
  const seconds = isActive ? (activeSeconds || 1) : frozenSeconds

  return (
    <details className="chat__reasoning">
      <summary className="chat__reasoning-summary">
        <span className="chat__reasoning-label">{label}</span>
        {seconds && (
          <span className="chat__reasoning-counter">{seconds}s</span>
        )}
        {isActive && (
          <span className="chat__reasoning-dots" aria-hidden="true">
            <span /><span /><span />
          </span>
        )}
      </summary>
      <div className="chat__reasoning-body">
        <StandardMarkdown text={item.content} />
      </div>
    </details>
  )
}


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
  // Render a single stream item by type. `i` is the ORIGINAL index in
  // streamItems, so the isLast/isActive checks below (which key off
  // `streamItems.length - 1`) stay correct even after adjacent tool items are
  // folded into a group — grouping never touches a trailing text/thinking item.
  const renderItem = (item, i) => {
    if (item.type === 'tool') {
      // Key a lone tool by `s-t-<firstIdx>` — the SAME scheme the group wrapper
      // below uses. When a second adjacent tool arrives and this slot becomes a
      // group, the wrapper key is unchanged, so React updates the slot in place
      // instead of delete+insert (which would churn height and displace the
      // reader). `i` here is the item's original index = the group's first
      // index, so the two keys coincide.
      return (
        <div key={`s-t-${i}`} className="chat__tools">
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
    if (item.type === 'thinking') {
      // The agent's live reasoning, shown as a COLLAPSED, secondary
      // disclosure so a long "thinking" stretch reads as a quiet
      // timer (filling the silence) rather than a
      // wall of raw chain-of-thought. The partner can expand it to
      // peek; by default the answer stays the focus. While this is
      // the last item the agent is still mid-thought, so the summary
      // animates with dots; an earlier thinking block the agent has
      // already moved past reads as a frozen "Thought for Ns" toggle.
      const isActive = i === streamItems.length - 1
      return (
        <ThinkingDisclosure key={`s-${i}`} item={item} isActive={isActive} />
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
  }

  // Fold adjacent tool items into one Activity card — the SAME grouping
  // MsgContent applies to the persisted blocks, so the live view and the
  // promoted message look identical.
  const nodes = groupToolRuns(streamItems.map((item, i) => ({ item, idx: i })))

  return (
    <li
      className="chat__msg chat__msg--assistant"
      data-key={dataKey}
    >
      {nodes.map(node => {
        if (node.group) {
          const tools = node.group.map(e => e.item)
          // `s-t-<firstIdx>` — the SAME key a lone tool at that index uses
          // (see renderItem), so the 1→2-tool transition updates this slot in
          // place rather than swapping keys and forcing a delete+insert.
          return (
            <div key={`s-t-${node.group[0].idx}`} className="chat__tools">
              <ToolActivityGroup tools={tools}>
                {node.group.map(e => (
                  <ToolBlock key={`s-${e.idx}`} t={e.item} />
                ))}
              </ToolActivityGroup>
            </div>
          )
        }
        return renderItem(node.single.item, node.single.idx)
      })}
    </li>
  )
}
