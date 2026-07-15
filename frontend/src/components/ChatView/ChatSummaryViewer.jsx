/* ChatSummaryViewer shows the platform-published cumulative summary on demand. */

import { useEffect, useRef, useState } from 'react'
import { apiFetch } from '../../api/client.js'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { StandardMarkdown } from './markdown/BlockRenderer.jsx'

export default function ChatSummaryViewer({ chatId, onClose }) {
  const [state, setState] = useState({ status: 'loading', summary: '', error: '' })
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
        setState({ status: 'ready', summary: data.chat_summary || '', error: '' })
      } catch (error) {
        if (error?.name === 'AbortError') return
        setState({
          status: 'error',
          summary: '',
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
            <p className="chat-summary__subtitle">Updated after each settled turn.</p>
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
          {state.status === 'ready' && !state.summary && (
            <p className="chat-summary__state">The first summary will appear after this chat settles.</p>
          )}
          {state.status === 'ready' && state.summary && (
            <div className="chat-summary__content">
              <StandardMarkdown text={state.summary} />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
