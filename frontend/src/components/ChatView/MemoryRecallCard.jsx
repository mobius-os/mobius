/* MemoryRecallCard is deliberately the same collapsed activity row as any
   other search tool. Its disclosure holds the owner-facing receipt: question,
   result summaries (or failure), and durable links into Memory. */

import { useId, useRef } from 'react'
import { ChevronRight } from '@openai/apps-sdk-ui/components/Icon'
import { memoryRecallCardModel } from './memoryRecallCard.js'
import { preserveTogglePosition } from './preserveTogglePosition.js'
import { ActivityTypeIcon } from './ActivityLineHeader.jsx'
import { useDisclosureState } from './disclosureState.js'
import { toolActivityIcon, toolCallLabel } from './toolActivityLabel.js'

function openInternal(event, href, onInternalNav) {
  if (!onInternalNav || !href) return
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey
      || event.button !== 0) return
  let url
  try {
    url = new URL(href, window.location.href)
  } catch {
    return
  }
  event.preventDefault()
  onInternalNav(url)
}

export default function MemoryRecallCard({
  t,
  chatId,
  disclosureKey,
  onInternalNav,
}) {
  const model = memoryRecallCardModel(t?.recall)
  const [open, setOpen] = useDisclosureState(chatId, disclosureKey)
  const headerRef = useRef(null)
  const detailRef = useRef(null)
  const headerId = useId()
  const detailId = useId()
  if (!model) return null

  const live = model.status === 'searching' || t?.status === 'running'
  const label = toolCallLabel(t)
  const iconKind = toolActivityIcon('MemoryRecall')

  return (
    <div className={
      `chat__tool chat__tool--${live ? 'running' : 'done'} chat__tool--compact`
      + ` chat__memory-tool chat__memory-tool--${model.status}`
    }>
      <button
        ref={headerRef}
        id={headerId}
        type="button"
        className="chat__tool-header"
        onClick={() => {
          preserveTogglePosition(headerRef.current, detailRef.current)
          setOpen(value => !value)
        }}
        aria-expanded={open}
        aria-controls={detailId}
        aria-label={`${label}${live ? ', in progress' : ''}`}
      >
        <span
          className={`chat__tool-icon${live ? ' chat__tool-icon--running' : ''}`}
          data-tool-kind={iconKind}
          aria-hidden="true"
        >
          <ActivityTypeIcon kind={iconKind} />
        </span>
        <span className="chat__tool-name" title={label}>
          {label}{live ? '…' : ''}
        </span>
      </button>

      <div
        ref={detailRef}
        id={detailId}
        className="chat__tool-detail chat__memory-detail"
        role="region"
        aria-labelledby={headerId}
        tabIndex={open ? 0 : undefined}
        hidden={!open}
      >
        {open && (
          <>
            {model.query && (
              <div className="chat__memory-section">
                <span className="chat__memory-kicker">Query</span>
                <p className="chat__memory-query">{model.query}</p>
              </div>
            )}

            {model.status === 'searching' && (
              <div className="chat__memory-section" role="status">
                <span className="chat__memory-kicker">Status</span>
                <p className="chat__memory-state">Searching Memory…</p>
              </div>
            )}

            {model.status === 'hit' && model.notes.length > 0 && (
              <div className="chat__memory-section chat__memory-results">
                <span className="chat__memory-kicker">Results</span>
                <ul className="chat__memory-list">
                  {model.notes.map(note => (
                    <li key={note.key}>
                      {note.href ? (
                        <a
                          className="chat__memory-note"
                          href={note.href}
                          onClick={event => openInternal(
                            event, note.href, onInternalNav,
                          )}
                          aria-label={`${note.label} — open in Memory`}
                        >
                          <span className="chat__memory-note-copy">
                            <strong>{note.label}</strong>
                            {note.summary && <span>{note.summary}</span>}
                          </span>
                          <ChevronRight width={14} height={14} aria-hidden="true" />
                        </a>
                      ) : (
                        <span className="chat__memory-note chat__memory-note--static">
                          <span className="chat__memory-note-copy">
                            <strong>{note.label}</strong>
                            {note.summary && <span>{note.summary}</span>}
                          </span>
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {model.status === 'empty' && (
              <div className="chat__memory-section">
                <span className="chat__memory-kicker">Results</span>
                <p className="chat__memory-state">
                  Nothing relevant is recorded yet.
                </p>
              </div>
            )}

            {model.status === 'failed' && (
              <div className="chat__memory-section">
                <span className="chat__memory-kicker">Error</span>
                <p className="chat__memory-state chat__memory-state--failed">
                  Memory couldn’t complete this lookup.
                </p>
              </div>
            )}

          </>
        )}
      </div>
    </div>
  )
}
