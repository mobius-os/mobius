/* Full chat-scoped diff viewer opened from the composer's Brain menu. */

import { useEffect, useMemo, useRef, useState } from 'react'
import { X } from '@openai/apps-sdk-ui/components/Icon'
import { apiFetch } from '../../api/client.js'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { formatRelativeTime } from '../../lib/relativeTime.js'
import FileDiffList from '../DiffView/FileDiffList.jsx'
import {
  mergeChatDiffEntries,
  normalizeChatDiffEntries,
  summarizeChatDiffs,
} from './chatDiffs.js'
import './ChatWork.css'

function updateLabel(entry) {
  const count = entry?.preview?.files?.length || 0
  return count === 1 ? '1 file' : `${count} files`
}

function updateTime(value) {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return formatRelativeTime(new Date(value).toISOString())
  }
  return formatRelativeTime(value)
}

export default function ChatDiffViewer({ chatId, initialEntries, onClose }) {
  const [state, setState] = useState({
    status: 'loading',
    entries: initialEntries || [],
    error: '',
  })
  const dialogRef = useRef(null)
  const closeRef = useRef(null)
  const expansionSequenceRef = useRef(0)
  const initialEntriesRef = useRef(initialEntries || [])
  const [expansionCommand, setExpansionCommand] = useState(null)
  initialEntriesRef.current = initialEntries || []

  useDialogFocus({
    containerRef: dialogRef,
    initialFocusRef: closeRef,
    onClose,
  })

  useEffect(() => {
    setState(current => ({
      ...current,
      entries: mergeChatDiffEntries(current.entries, initialEntries),
    }))
  }, [initialEntries])

  useEffect(() => {
    const controller = new AbortController()
    async function load() {
      try {
        const response = await apiFetch(
          `/chats/${encodeURIComponent(chatId)}/edit-diffs`,
          { signal: controller.signal },
        )
        if (!response.ok) throw new Error(`Request failed (${response.status})`)
        const data = await response.json()
        const authoritative = normalizeChatDiffEntries(data?.entries)
        setState({
          status: 'ready',
          entries: mergeChatDiffEntries(authoritative, initialEntriesRef.current),
          error: '',
        })
      } catch (error) {
        if (error?.name === 'AbortError') return
        setState(current => ({
          ...current,
          status: 'error',
          error: 'Could not refresh the complete change history.',
        }))
      }
    }
    load()
    return () => controller.abort()
  }, [chatId])

  const summary = useMemo(() => summarizeChatDiffs(state.entries), [state.entries])
  const shortenedCount = state.entries.filter(entry => entry.preview?.truncated).length

  function setEveryDiffExpanded(expanded) {
    expansionSequenceRef.current += 1
    setExpansionCommand({
      id: expansionSequenceRef.current,
      expanded,
    })
  }

  return (
    <div className="chat-work__overlay" role="presentation" onClick={onClose}>
      <div
        ref={dialogRef}
        className="chat-work chat-work--diffs"
        role="dialog"
        aria-modal="true"
        aria-labelledby="chat-work-diff-title"
        onClick={event => event.stopPropagation()}
      >
        <header className="chat-work__head">
          <div>
            <h2 id="chat-work-diff-title">Changes from this chat</h2>
            <p>
              {summary.updateCount > 0
                ? `${summary.updateCount} ${summary.updateCount === 1 ? 'update' : 'updates'} · ${summary.fileCount} ${summary.fileCount === 1 ? 'file' : 'files'}`
                : 'Every recorded file edit will appear here.'}
            </p>
          </div>
          <button
            ref={closeRef}
            type="button"
            className="chat-work__close"
            onClick={onClose}
            aria-label="Close changes"
          >
            <X width={19} height={19} />
          </button>
        </header>
        {state.entries.length > 0 && (
          <div className="chat-work__toolbar">
            <div role="group" aria-label="Diff display controls">
              <button type="button" onClick={() => setEveryDiffExpanded(true)}>
                Expand all
              </button>
              <button type="button" onClick={() => setEveryDiffExpanded(false)}>
                Collapse all
              </button>
            </div>
          </div>
        )}
        <div className="chat-work__body">
          {state.status === 'loading' && state.entries.length === 0 && (
            <p className="chat-work__state" role="status">Loading changes…</p>
          )}
          {state.status === 'error' && state.entries.length === 0 && (
            <p className="chat-work__state chat-work__state--error" role="alert">
              {state.error}
            </p>
          )}
          {state.entries.length === 0 && state.status === 'ready' && (
            <div className="chat-work__empty">
              <strong>No file changes recorded yet</strong>
              <span>Edits made through this chat will collect here automatically.</span>
            </div>
          )}
          {state.entries.length > 0 && (
            <div className="chat-work__updates">
              {state.status === 'error' && (
                <p className="chat-work__notice">{state.error} Showing the changes already loaded in this chat.</p>
              )}
              {shortenedCount > 0 && (
                <p className="chat-work__notice">
                  {shortenedCount === 1
                    ? '1 older update is excerpt-only because its full diff was never saved.'
                    : `${shortenedCount} older updates are excerpt-only because their full diffs were never saved.`}
                </p>
              )}
              {state.entries.map((entry, index) => (
                <section className="chat-work__update" key={entry.id}>
                  <div className="chat-work__update-head">
                    <div>
                      <span className="chat-work__update-number">Update {index + 1}</span>
                      <strong>{updateLabel(entry)}</strong>
                    </div>
                    {entry.ts ? <span>{updateTime(entry.ts)}</span> : null}
                  </div>
                  <FileDiffList
                    files={entry.preview.files}
                    diffTruncated={entry.preview.truncated}
                    expansionCommand={expansionCommand}
                  />
                  {entry.preview.relative && (
                    <p className="chat-work__update-note">Line numbers are relative to the edited selection.</p>
                  )}
                </section>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
