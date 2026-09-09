/* Helper results expand in place; reading activity never resumes its parent. */
import { useId, useRef } from 'react'
import { ArrowDown, ChevronDown } from '@openai/apps-sdk-ui/components/Icon'
import { peerTime } from './peerTimeline.js'
import { preserveTogglePosition } from './preserveTogglePosition.js'
import { useDisclosureState } from './disclosureState.js'

export default function HelperResultCard({ event, chatId }) {
  const [open, setOpen] = useDisclosureState(chatId, event.id)
  const headerRef = useRef(null)
  const detailRef = useRef(null)
  const headerId = useId()
  const detailId = useId()
  const status = event.status === 'completed' ? 'Helper finished' : event.status === 'failed' ? 'Helper failed' : 'Helper stopped'
  const label = `${status}${event.task_key ? ` · ${event.task_key}` : ''}`
  const time = peerTime(event.created_at)
  const date = Number.isFinite(time) ? new Date(time) : null
  return <div className="chat__tool chat__tool--done chat__tool--compact chat__peer-tool">
    <button ref={headerRef} id={headerId} type="button" className="chat__tool-header"
      aria-expanded={open} aria-controls={detailId} aria-label={label}
      onClick={() => {
        preserveTogglePosition(headerRef.current, detailRef.current)
        setOpen(value => !value)
      }}>
      <span className="chat__tool-icon" data-tool-kind="agents" aria-hidden="true"><ArrowDown width={14} height={14} /></span>
      <span className="chat__tool-name" title={label}>{label}</span>
      {date && <time className="chat__peer-time" dateTime={date.toISOString()} title={date.toLocaleString()}>{date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</time>}
      <ChevronDown className={`chat__peer-chevron${open ? ' chat__peer-chevron--open' : ''}`} width={12} height={12} />
    </button>
    <div ref={detailRef} id={detailId} className="chat__tool-detail chat__peer-detail" role="region"
      aria-labelledby={headerId} tabIndex={open ? 0 : undefined} hidden={!open}>
      {open && <>
        <p className="chat__peer-delivery">{event.consumption === 'incorporated' ? 'Incorporated by the agent' : 'Available to the agent · opening this does not resume work'}</p>
        <p className="chat__peer-body">{event.body || 'No written result.'}</p>
        {event.result_truncated && <span className="chat__peer-excerpt">Excerpt — full result in the helper chat</span>}
        {event.child_chat_id && <div className="chat__peer-links"><a href={`/chat/${encodeURIComponent(event.child_chat_id)}`}>Open helper chat</a></div>}
      </>}
    </div>
  </div>
}
