/* ChatSummaryViewer shows the chat name, replaceable chat summary, and append-only full digest. */

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
            <p className="chat-summary__subtitle">Three levels of continuity, saved by the agent as it works.</p>
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
                  <p>The name used to identify this conversation.</p>
                </div>
                <div className="chat-summary__layer-body chat-summary__layer-body--plain">
                  {state.layers.description || 'The chat name appears once the agent saves this chat.'}
                </div>
              </section>
              <section className="chat-summary__layer">
                <div className="chat-summary__layer-head">
                  <h3>Chat summary</h3>
                  <p>The whole conversation in brief, with connected recent progress.</p>
                </div>
                <div className="chat-summary__layer-body">
                  {state.layers.digest
                    ? <StandardMarkdown text={state.layers.digest} math={false} />
                    : <p className="chat-summary__empty">No chat summary has been saved yet.</p>}
                </div>
              </section>
              <section className="chat-summary__layer">
                <div className="chat-summary__layer-head">
                  <h3>Full digest</h3>
                  <p>Accumulated decisions and evidence retained for safe continuation.</p>
                </div>
                <div className="chat-summary__layer-body">
                  {state.layers.summary
                    ? <StandardMarkdown text={state.layers.summary} math={false} />
                    : <p className="chat-summary__empty">No full digest entries have been saved yet.</p>}
                </div>
              </section>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
