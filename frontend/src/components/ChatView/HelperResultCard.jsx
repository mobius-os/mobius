/* Helper results expand in place; reading activity never resumes its parent. */
import { useId, useRef } from 'react'
import { StandardMarkdown } from './markdown/BlockRenderer.jsx'
import { ArrowDown } from '@openai/apps-sdk-ui/components/Icon'
import { peerTime } from './peerTimeline.js'
import { preserveTogglePosition } from './preserveTogglePosition.js'
import { useDisclosureState } from './disclosureState.js'

export default function HelperResultCard({ event, chatId, onInternalNav }) {
  const [open, setOpen] = useDisclosureState(chatId, event.id)
  const headerRef = useRef(null)
  const detailRef = useRef(null)
  const headerId = useId()
  const detailId = useId()
  const status = event.status === 'completed' ? 'Helper finished' : event.status === 'failed' ? 'Helper failed' : 'Helper stopped'
  const label = `${status}${event.task_key ? ` · ${event.task_key}` : ''}`
  const time = peerTime(event.created_at)
  const date = Number.isFinite(time) ? new Date(time) : null
  const delivery = event.consumption === 'incorporated'
    ? 'Incorporated by the agent'
    : event.consumption === 'available'
      ? 'Available to the agent · opening this does not resume work'
      : event.consumption === 'notified'
        ? 'Delivery recorded · agent incorporation is not known · opening this does not resume work'
        : 'Agent incorporation is not known · opening this does not resume work'
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
    </button>
    <div ref={detailRef} id={detailId} className="chat__tool-detail chat__peer-detail" role="region"
      aria-labelledby={headerId} tabIndex={open ? 0 : undefined} hidden={!open}>
      {open && <>
        <p className="chat__peer-delivery">{delivery}</p>
        <div className="chat__peer-body"><StandardMarkdown text={event.body || 'No written result.'} onInternalNav={onInternalNav} /></div>
        {event.result_truncated && <span className="chat__peer-excerpt">Excerpt — full result in the helper chat</span>}
        {event.child_chat_id && <div className="chat__peer-links"><a href={`/shell?chat=${encodeURIComponent(event.child_chat_id)}`} onClick={click => {
          if (!onInternalNav || click.button !== 0 || click.metaKey || click.ctrlKey || click.shiftKey || click.altKey) return
          click.preventDefault()
          onInternalNav(new URL(click.currentTarget.href))
        }}>Open helper chat</a></div>}
      </>}
    </div>
  </div>
}
