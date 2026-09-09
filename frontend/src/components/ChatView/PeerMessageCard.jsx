/* PeerMessageCard discloses an agent exchange within the normal tool timeline. */

import { useId, useRef } from 'react'
import { StandardMarkdown } from './markdown/BlockRenderer.jsx'
import { ArrowDown, ArrowUp } from '@openai/apps-sdk-ui/components/Icon'
import { usePeerTimelineRecord } from './peerTimelineContext.js'
import { peerTime } from './peerTimeline.js'
import { peerMessageCardModel } from './peerMessageCard.js'
import { preserveTogglePosition } from './preserveTogglePosition.js'
import { useDisclosureState } from './disclosureState.js'
import { toolCallLabel } from './toolActivityLabel.js'

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

export default function PeerMessageCard({ t, chatId, disclosureKey, records: suppliedRecords, onInternalNav }) {
  const linkedRecords = usePeerTimelineRecord(t?.tool_use_id)
  const records = suppliedRecords || linkedRecords || []
  const record = records[0]
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
  const label = live ? toolCallLabel(t) : model.status === 'sent'
    ? `Sent to ${model.broadcast ? 'the group' : model.peers.join(', ') || 'another agent'}`
    : model.status === 'received' && model.notes.length === 1
      ? `Received from ${model.notes[0].sender || 'another agent'}`
      : toolCallLabel(t)
  const DirectionIcon = model.direction === 'send' ? ArrowUp : ArrowDown
  const time = peerTime(record?.created_at)
  const date = Number.isFinite(time) ? new Date(time) : null
  const hasDetail = model.hasDetail
  const headerContent = (
    <>
      <span
        className={`chat__tool-icon${live ? ' chat__tool-icon--running' : ''}`}
        data-tool-kind="agents"
        aria-hidden="true"
      >
        <DirectionIcon width={14} height={14} />
      </span>
      <span className="chat__tool-name" title={label}>
        {label}{live ? '…' : ''}
      </span>
      {date && <time className="chat__peer-time" dateTime={date.toISOString()} title={date.toLocaleString()}>{date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</time>}
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
            {record && <p className="chat__peer-delivery">{record.observedDelivery === 'during_work'
              ? 'Delivered during work'
              : record.observedDelivery === 'next_turn' ? 'Delivered at a turn boundary'
                : record.delivery === 'interrupt' ? 'Immediate delivery requested · not a read receipt'
                  : record.delivery === 'next_turn' ? 'Next-turn delivery · not a read receipt'
                    : 'Delivery timing not recorded'}</p>}
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
                  {model.body && <div className="chat__peer-body"><StandardMarkdown text={model.body} onInternalNav={onInternalNav} /></div>}
                  {model.bodyTruncated && (
                    <span className="chat__peer-excerpt">Excerpt — full note not shown</span>
                  )}
                </div>
              </div>
            )}

            {model.status === 'received' && (
              <div className="chat__peer-section chat__peer-results">
                {model.count > 1 && <span className="chat__peer-kicker">
                  Received {model.count}
                </span>}
                <ul className="chat__peer-list">
                  {model.notes.map(note => (
                    <li key={note.key} className="chat__peer-note">
                      <span className="chat__peer-note-head">
                        <KindBadge kind={note.kind} />
                        {model.count > 1 && note.sender && (
                          <span className="chat__peer-from">
                            from {note.sender}
                          </span>
                        )}
                      </span>
                      {note.body && <div className="chat__peer-body"><StandardMarkdown text={note.body} onInternalNav={onInternalNav} /></div>}
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

            {records.length > 0 && <div className="chat__peer-links">{[...new Map(records.map(note => {
              const sent = model.direction === 'send'
              return [sent ? note.recipient_chat_id : note.sender_chat_id, sent ? note.recipient_name : note.sender_name]
            })).entries()].filter(([id]) => typeof id === 'string' && id !== chatId).map(([id, name]) => <a key={id} href={`/shell?chat=${encodeURIComponent(id)}`} onClick={event => {
              if (!onInternalNav || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
              event.preventDefault()
              onInternalNav(new URL(event.currentTarget.href))
            }}>Open {name || 'source chat'}</a>)}</div>}
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
