/* HelperConversation shows one helper agent's own conversation, read-only, over its parent chat. */

import { useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { apiFetch } from '../../api/client.js'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { StandardMarkdown } from './markdown/BlockRenderer.jsx'
import ToolBlock from './ToolBlock.jsx'
import './HelperConversation.css'

// A running helper keeps writing its conversation; re-read it on this cadence
// while the dialog is open. A settled helper is read once.
const LIVE_REFRESH_MS = 3000

const PROVIDER_NAMES = { claude: 'Claude', codex: 'Codex' }
const STATUS_LABELS = { running: 'Working', done: 'Finished', failed: 'Failed' }

async function readConversation(chatId, taskId, signal) {
  const response = await apiFetch(
    `/chats/${encodeURIComponent(chatId)}/helpers/${encodeURIComponent(taskId)}`,
    { signal },
  )
  if (response.status === 404) return { phase: 'unavailable' }
  if (!response.ok) throw new Error(`Request failed (${response.status})`)
  const data = await response.json()
  return { phase: 'ready', blocks: data.blocks || [], truncated: !!data.truncated, provider: data.provider }
}

// The dialog draws its own overlay (HelperConversation.css); `host` is the
// chat pane it covers.
export default function HelperConversation({
  chatId, taskId, name, status, host, onClose, onInternalNav,
}) {
  const [state, setState] = useState({ phase: 'loading' })
  const dialogRef = useRef(null)
  const closeRef = useRef(null)
  const bodyRef = useRef(null)
  const followRef = useRef(true)
  const titleId = useId()
  const running = status === 'running'

  useDialogFocus({ containerRef: dialogRef, initialFocusRef: closeRef, onClose })

  useEffect(() => {
    const controller = new AbortController()
    let timer = null
    async function load() {
      try {
        setState(await readConversation(chatId, taskId, controller.signal))
      } catch (error) {
        if (error?.name === 'AbortError') return
        // A failed live refresh keeps the conversation already on screen.
        setState(prev => prev.phase === 'ready'
          ? prev
          : { phase: 'error', message: error?.message || 'Could not load this conversation.' })
      }
      if (running && !controller.signal.aborted) timer = setTimeout(load, LIVE_REFRESH_MS)
    }
    load()
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [chatId, taskId, running])

  // Keep a live conversation on its newest step unless the reader scrolled up.
  useEffect(() => {
    const body = bodyRef.current
    if (body && state.phase === 'ready' && followRef.current) body.scrollTop = body.scrollHeight
  }, [state])

  const subtitle = [PROVIDER_NAMES[state.provider], STATUS_LABELS[status] || 'Stopped', 'read-only']
    .filter(Boolean).join(' · ')

  return createPortal(
    <div className="helper-convo__overlay" role="presentation" onClick={onClose}>
      <div
        ref={dialogRef}
        className="helper-convo"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onClick={event => event.stopPropagation()}
      >
        <div className="helper-convo__head">
          <div className="helper-convo__heading">
            <h2 id={titleId} className="helper-convo__title">{name}</h2>
            <p className="helper-convo__subtitle">{subtitle}</p>
          </div>
          <button
            ref={closeRef}
            type="button"
            className="helper-convo__close"
            onClick={onClose}
            aria-label="Close helper conversation"
          >×</button>
        </div>
        <div
          ref={bodyRef}
          className="helper-convo__body"
          onScroll={event => {
            const el = event.currentTarget
            followRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48
          }}
        >
          {state.phase === 'loading' && <p className="helper-convo__state">Loading conversation…</p>}
          {state.phase === 'unavailable' && (
            <p className="helper-convo__state">
              This helper&apos;s conversation isn&apos;t available. Its record may have been cleaned up.
            </p>
          )}
          {state.phase === 'error' && (
            <p className="helper-convo__state helper-convo__state--error" role="alert">{state.message}</p>
          )}
          {state.phase === 'ready' && (
            <div className="helper-convo__blocks">
              {state.truncated && <p className="helper-convo__note">Showing the most recent part of a long conversation.</p>}
              {state.blocks.length === 0 && <p className="helper-convo__note">The helper hasn&apos;t done anything yet.</p>}
              {state.blocks.map((block, i) => {
                const key = `${taskId}:${i}`
                if (block.type === 'tool') {
                  return (
                    <div key={key} className="chat__tools">
                      <ToolBlock t={block} chatId={chatId} compact disclosureKey={`helper:${key}`} onInternalNav={onInternalNav} />
                    </div>
                  )
                }
                return (
                  <div key={key} className={block.role === 'user' ? 'helper-convo__task' : 'helper-convo__text'}>
                    {block.role === 'user' && <span className="helper-convo__task-label">Task</span>}
                    <StandardMarkdown text={block.content} onInternalNav={onInternalNav} />
                  </div>
                )
              })}
              {running && <p className="helper-convo__note" aria-live="polite">Still working — updates as it goes.</p>}
            </div>
          )}
        </div>
      </div>
    </div>,
    host || document.body,
  )
}
