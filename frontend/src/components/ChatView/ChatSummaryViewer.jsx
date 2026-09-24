/* ChatSummaryViewer distinguishes the current summary from its digest history. */

import { useRef } from 'react'
import { apiFetch } from '../../api/client.js'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { StandardMarkdown } from './markdown/BlockRenderer.jsx'
import { readChatContinuityPage } from './chatSummaryHistory.js'
import { useChatSummaryContinuity } from './hooks/useChatSummaryContinuity.js'

function readPage(chatId, afterRevision, signal) {
  return readChatContinuityPage(chatId, afterRevision, apiFetch, signal)
}

export default function ChatSummaryViewer({ chatId, onClose }) {
  const { state, loadMore, loadingOlder } = useChatSummaryContinuity(chatId, readPage)
  const dialogRef = useRef(null)
  const closeRef = useRef(null)

  useDialogFocus({ containerRef: dialogRef, initialFocusRef: closeRef, onClose })

  return (
    <div className="chat-summary__overlay" role="presentation" onClick={onClose}>
      <div
        ref={dialogRef}
        className="chat-summary"
        role="dialog"
        aria-modal="true"
        aria-labelledby="chat-summary-title"
        onClick={event => event.stopPropagation()}
      >
        <div className="chat-summary__head">
          <div>
            <h2 id="chat-summary-title" className="chat-summary__title">Chat summary</h2>
            <p className="chat-summary__subtitle">
              The current handoff and saved checkpoint history for this conversation.
            </p>
          </div>
          <button
            ref={closeRef}
            type="button"
            className="chat-summary__close"
            onClick={onClose}
            aria-label="Close chat summary"
          >×</button>
        </div>
        <div className="chat-summary__body">
          {state.status === 'loading' && (
            <p className="chat-summary__state">Loading summary…</p>
          )}
          {state.status === 'error' && (
            <p className="chat-summary__state chat-summary__state--error" role="alert">
              {state.error}
            </p>
          )}
          {state.status === 'ready' && (
            <div className="chat-summary__layers">
              {state.error && (
                <p className="chat-summary__state chat-summary__state--error" role="alert">
                  {state.error}
                </p>
              )}
              <section className="chat-summary__layer">
                <div className="chat-summary__layer-head">
                  <h3>Chat name</h3>
                  <p>One-line name used to identify this conversation.</p>
                </div>
                <div className="chat-summary__layer-body chat-summary__layer-body--plain">
                  {state.layers.description || 'The chat name appears here when available.'}
                </div>
              </section>
              <section className="chat-summary__layer">
                <div className="chat-summary__layer-head">
                  <h3>Current summary</h3>
                  <p>Current handoff retained for continuing this conversation.</p>
                </div>
                <div className="chat-summary__layer-body">
                  {state.layers.summary
                    ? <StandardMarkdown text={state.layers.summary} />
                    : <p className="chat-summary__empty">No current summary has been published yet.</p>}
                </div>
              </section>
              <section className="chat-summary__layer">
                <div className="chat-summary__layer-head">
                  <h3>Digest history</h3>
                  <p>Saved updates, in the order they were recorded.</p>
                </div>
                <div className="chat-summary__layer-body">
                  {state.layers.history.length === 0 && (
                    <p className="chat-summary__empty">No digest entries have been saved yet.</p>
                  )}
                  {state.layers.history.map(entry => (
                    <article key={`${entry.revision}-${entry.checkpoint_id}`}>
                      <h4>{entry.legacy_baseline ? 'Legacy baseline' : `Revision ${entry.revision}`}</h4>
                      {entry.digest && <StandardMarkdown text={entry.digest} />}
                      {entry.legacy_markdown && <StandardMarkdown text={entry.legacy_markdown} />}
                    </article>
                  ))}
                </div>
                {state.hasMore && (
                  <button type="button" disabled={loadingOlder} onClick={loadMore}>
                    {loadingOlder ? 'Loading…' : 'Load more checkpoints'}
                  </button>
                )}
              </section>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
