/* ChatSummaryViewer shows all three platform-published chat summary layers. */

import { useEffect, useRef, useState } from 'react'
import { apiFetch } from '../../api/client.js'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { StandardMarkdown } from './markdown/BlockRenderer.jsx'

export default function ChatSummaryViewer({ chatId, onClose }) {
  const [state, setState] = useState({
    status: 'loading',
    layers: { description: '', digest: '', summary: '' },
    error: '',
  })
  const dialogRef = useRef(null)
  const closeRef = useRef(null)

  useDialogFocus({
    containerRef: dialogRef,
    initialFocusRef: closeRef,
    onClose,
  })

  useEffect(() => {
    const controller = new AbortController()
    async function load() {
      try {
        const response = await apiFetch(`/chats/${chatId}/agent-context`, {
          signal: controller.signal,
        })
        if (!response.ok) throw new Error(`Request failed (${response.status})`)
        const data = await response.json()
        setState({
          status: 'ready',
          layers: {
            description: data.chat_description || '',
            digest: data.chat_digest || '',
            summary: data.chat_summary || '',
          },
          error: '',
        })
      } catch (error) {
        if (error?.name === 'AbortError') return
        setState({
          status: 'error',
          layers: { description: '', digest: '', summary: '' },
          error: error?.message || 'Could not load the chat summary.',
        })
      }
    }
    load()
    return () => controller.abort()
  }, [chatId])

  return (
    <div className="chat-summary__overlay" role="presentation" onClick={onClose}>
      <div
        ref={dialogRef}
        className="chat-summary"
        role="dialog"
        aria-modal="true"
        aria-labelledby="chat-summary-title"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="chat-summary__head">
          <div>
            <h2 id="chat-summary-title" className="chat-summary__title">Chat summary</h2>
            <p className="chat-summary__subtitle">Three levels of continuity, updated after each settled turn.</p>
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
              <section className="chat-summary__layer">
                <div className="chat-summary__layer-head">
                  <h3>Chat name</h3>
                  <p>One-line summary used to identify this conversation.</p>
                </div>
                <div className="chat-summary__layer-body chat-summary__layer-body--plain">
                  {state.layers.description || 'The chat name will appear after this conversation settles.'}
                </div>
              </section>
              <section className="chat-summary__layer">
                <div className="chat-summary__layer-head">
                  <h3>Digest</h3>
                  <p>Bounded context available to recent conversations.</p>
                </div>
                <div className="chat-summary__layer-body">
                  {state.layers.digest
                    ? <StandardMarkdown text={state.layers.digest} />
                    : <p className="chat-summary__empty">No separate digest has been published for this chat yet.</p>}
                </div>
              </section>
              <section className="chat-summary__layer">
                <div className="chat-summary__layer-head">
                  <h3>Full summary</h3>
                  <p>Cumulative handoff retained for continuing this conversation.</p>
                </div>
                <div className="chat-summary__layer-body">
                  {state.layers.summary
                    ? <StandardMarkdown text={state.layers.summary} />
                    : <p className="chat-summary__empty">The full summary will appear after this conversation settles.</p>}
                </div>
              </section>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
