/* PeerMessageCard renders a Möbius peer-network exchange as its own collapsed
   activity row — so the owner can see when the agent talked to another agent,
   instead of it hiding inside a generic "Ran commands" block. The disclosure
   holds the note itself: who, what kind, and the body. */

import { useId, useRef } from 'react'
import { peerMessageCardModel } from './peerMessageCard.js'
import { preserveTogglePosition } from './preserveTogglePosition.js'
import { ActivityTypeIcon } from './ActivityLineHeader.jsx'
import { useDisclosureState } from './disclosureState.js'
import { toolActivityIcon, toolCallLabel } from './toolActivityLabel.js'

const KIND_LABEL = {
  note: 'Note',
  finding: 'Finding',
  request: 'Request',
  blocker: 'Blocker',
  handoff: 'Handoff',
}

function KindBadge({ kind }) {
  return (
    <span className={`chat__peer-kind chat__peer-kind--${kind}`}>
      {KIND_LABEL[kind] || 'Note'}
    </span>
  )
}

export default function PeerMessageCard({ t, chatId, disclosureKey }) {
  const model = peerMessageCardModel(t?.peer_message)
  const [open, setOpen] = useDisclosureState(chatId, disclosureKey)
  const headerRef = useRef(null)
  const detailRef = useRef(null)
  const headerId = useId()
  const detailId = useId()
  if (!model) return null

  // Liveness follows the tool, never the provisional marker: an interrupted
  // send/read leaves the marker at 'sending'/'reading' but the tool is done,
  // so a historical card must not spin forever.
  const live = t?.status === 'running'
  const label = toolCallLabel(t)
  const iconKind = toolActivityIcon('PeerMessage')
  const hasDetail = model.hasDetail
  const headerContent = (
    <>
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
    </>
  )

  return (
    <div className={
      `chat__tool chat__tool--${live ? 'running' : 'done'} chat__tool--compact`
      + ` chat__peer-tool chat__peer-tool--${model.status}`
    }>
      {hasDetail ? (
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
          aria-label={label}
        >
          {headerContent}
        </button>
      ) : (
        <div
          className="chat__tool-header chat__tool-header--static"
          role={live ? 'status' : undefined}
          aria-label={`${label}${live ? ', in progress' : ''}`}
        >
          {headerContent}
        </div>
      )}

      {hasDetail && (
        <div
          ref={detailRef}
          id={detailId}
          className="chat__tool-detail chat__peer-detail"
          role="region"
          aria-labelledby={headerId}
          tabIndex={open ? 0 : undefined}
          hidden={!open}
        >
          {open && (
          <>
            {model.status === 'sent' && (
              <div className="chat__peer-section">
                <span className="chat__peer-kicker">
                  {model.broadcast
                    ? 'Sent to everyone in this scope'
                    : 'Sent to'}
                </span>
                {!model.broadcast && model.peers.length > 0 && (
                  <p className="chat__peer-names">{model.peers.join(', ')}</p>
                )}
                {!model.broadcast && model.truncated && (
                  <p className="chat__peer-meta">
                    Showing {model.peers.length} of {model.count} recipients
                  </p>
                )}
                <div className="chat__peer-note">
                  <KindBadge kind={model.kind} />
                  {model.body && <p className="chat__peer-body">{model.body}</p>}
                  {model.bodyTruncated && (
                    <span className="chat__peer-excerpt">Excerpt — full note not shown</span>
                  )}
                </div>
              </div>
            )}

            {model.status === 'received' && (
              <div className="chat__peer-section chat__peer-results">
                <span className="chat__peer-kicker">
                  {model.count === 1 ? 'Received' : `Received ${model.count}`}
                </span>
                <ul className="chat__peer-list">
                  {model.notes.map(note => (
                    <li key={note.key} className="chat__peer-note">
                      <span className="chat__peer-note-head">
                        <KindBadge kind={note.kind} />
                        {note.sender && (
                          <span className="chat__peer-from">
                            from {note.sender}
                          </span>
                        )}
                      </span>
                      {note.body && <p className="chat__peer-body">{note.body}</p>}
                      {note.bodyTruncated && (
                        <span className="chat__peer-excerpt">Excerpt — full note not shown</span>
                      )}
                    </li>
                  ))}
                </ul>
                {model.truncated && (
                  <p className="chat__peer-meta">
                    Showing {model.notes.length} of {model.count} messages
                  </p>
                )}
              </div>
            )}

            {model.status === 'failed' && model.reason && (
              <div className="chat__peer-section">
                <span className="chat__peer-kicker">Failed</span>
                <p className="chat__peer-body chat__peer-reason">{model.reason}</p>
              </div>
            )}
          </>
          )}
        </div>
      )}
    </div>
  )
}
