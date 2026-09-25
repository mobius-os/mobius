/* Helper results expand in place; reading activity never resumes its parent. */
import { useEffect, useId, useRef, useState } from 'react'
import { StandardMarkdown } from './markdown/BlockRenderer.jsx'
import { ArrowDown, ChevronRight } from '@openai/apps-sdk-ui/components/Icon'
import './SubagentChips.css'
import { peerTime } from './peerTimeline.js'
import { formatDateTime, formatTime } from '../../lib/dateTimeFormat.js'
import { preserveTogglePosition } from './preserveTogglePosition.js'
import { useDisclosureState } from './disclosureState.js'
import { isWorkingHelper } from './workingHelper.js'
import { elapsedLabel } from './toolTasks.js'
import { toolCallLabel } from './toolActivityLabel.js'
import HelperConversation from './HelperConversation.jsx'

const PROVIDER_NAMES = { claude: 'Claude', codex: 'Codex', mobius: 'Möbius' }

// "Claude · claude-opus-4-8": which provider and model a helper runs on.
export function helperEngine(event) {
  return [PROVIDER_NAMES[event.provider] || event.provider, event.model]
    .filter(Boolean).join(' · ')
}

// The helper's newest step in owner language ("Running npm test"), or null.
function helperStep(event) {
  const activity = event.activity
  if (!activity || typeof activity.tool !== 'string') return null
  return toolCallLabel({ tool: activity.tool, input: activity.summary || '', status: 'running' })
}

// One ticking clock while anything visible runs, from the helper's start time.
function useElapsed(startedAt, running) {
  const start = Date.parse(startedAt || '')
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!running || !Number.isFinite(start)) return undefined
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [running, start])
  return Number.isFinite(start) ? elapsedLabel(now - start) : null
}


function openHelperChat(click, childChatId, onInternalNav) {
  if (!onInternalNav || click.button !== 0 || click.metaKey || click.ctrlKey || click.shiftKey || click.altKey) return
  click.preventDefault()
  onInternalNav(new URL(`/shell?chat=${encodeURIComponent(childChatId)}`, window.location.origin))
}

/* A still-working helper is a live row like Claude's own helper rows: its
   name, provider and model, what it is doing now, and a running clock. One tap
   opens its conversation over this chat, read-only and updating live. */
export function WorkingHelperRow({ event, chatId, onInternalNav }) {
  const [open, setOpen] = useState(false)
  const rowRef = useRef(null)
  const name = event.task_key || 'Helper'
  const paused = event.status === 'paused'
  const elapsed = useElapsed(event.started_at, !paused)
  const step = paused ? 'Paused' : helperStep(event) || 'Working'
  const sub = [helperEngine(event), step].filter(Boolean).join(' · ')
  const canOpen = !!(chatId && event.delegation_id)
  const body = <>
    <span className={`chat__subagent-dot ${paused ? 'chat__subagent-dot--failed' : 'chat__subagent-dot--running'}`} aria-hidden="true" />
    <span className="chat__subagent-body">
      <span className="chat__subagent-name">
        <span className="chat__subagent-name-text">{name}</span>
        <span className="chat__subagent-name-sweep" aria-hidden="true">{name}</span>
      </span>
      <span className="chat__subagent-sub">{sub}</span>
    </span>
    {elapsed && <span className="chat__subagent-elapsed">{elapsed}</span>}
    {canOpen && <ChevronRight className="chat__subagent-open" width={14} height={14} aria-hidden="true" />}
  </>
  return <div ref={rowRef} className="chat__subagents-list chat__helper-working">
    {canOpen
      ? <button
          type="button"
          className="chat__subagent chat__subagent--openable chat__subagent--running"
          aria-label={`${name}: ${sub}. Open its conversation`}
          aria-haspopup="dialog"
          onClick={() => setOpen(true)}
        >{body}</button>
      : <div className="chat__subagent chat__subagent--running">{body}</div>}
    {open && <HelperConversation
      chatId={chatId}
      taskId={event.delegation_id}
      name={name}
      status={paused ? 'stopped' : 'running'}
      host={rowRef.current?.closest('.chat')}
      onClose={() => setOpen(false)}
      onInternalNav={onInternalNav}
    />}
  </div>
}

export default function HelperResultCard({ event, chatId, onInternalNav }) {
  if (isWorkingHelper({ ...event, type: 'helper_result' })) {
    return <WorkingHelperRow event={event} chatId={chatId} onInternalNav={onInternalNav} />
  }
  return <FinishedHelperCard event={event} chatId={chatId} onInternalNav={onInternalNav} />
}

function FinishedHelperCard({ event, chatId, onInternalNav }) {
  const [open, setOpen] = useDisclosureState(chatId, event.id)
  const [viewing, setViewing] = useState(false)
  const headerRef = useRef(null)
  const detailRef = useRef(null)
  const headerId = useId()
  const detailId = useId()
  const status = event.status === 'completed' ? 'Helper finished' : event.status === 'failed' ? 'Helper failed' : 'Helper stopped'
  const duration = Number.isFinite(event.duration_ms) ? elapsedLabel(event.duration_ms) : null
  const label = [
    `${status}${event.task_key ? ` · ${event.task_key}` : ''}`,
    helperEngine(event),
    duration,
  ].filter(Boolean).join(' · ')
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
      {date && <time className="chat__peer-time" dateTime={date.toISOString()} title={formatDateTime(date)}>{formatTime(date)}</time>}
    </button>
    <div ref={detailRef} id={detailId} className="chat__tool-detail chat__peer-detail" role="region"
      aria-labelledby={headerId} tabIndex={open ? 0 : undefined} hidden={!open}>
      {open && <>
        <p className="chat__peer-delivery">{delivery}</p>
        <div className="chat__peer-body"><StandardMarkdown text={event.body || 'No written result.'} onInternalNav={onInternalNav} /></div>
        {event.result_truncated && <span className="chat__peer-excerpt">Excerpt — full result in the helper chat</span>}
        {event.child_chat_id && <div className="chat__peer-links">
          {event.delegation_id && <button type="button" className="chat__peer-link-button" onClick={() => setViewing(true)}>View conversation</button>}
          <a href={`/shell?chat=${encodeURIComponent(event.child_chat_id)}`} onClick={click => openHelperChat(click, event.child_chat_id, onInternalNav)}>Open helper chat</a>
        </div>}
      </>}
    </div>
    {viewing && <HelperConversation
      chatId={chatId}
      taskId={event.delegation_id}
      name={event.task_key || 'Helper'}
      status={event.status === 'completed' ? 'done' : event.status === 'failed' ? 'failed' : 'stopped'}
      host={headerRef.current?.closest('.chat')}
      onClose={() => setViewing(false)}
      onInternalNav={onInternalNav}
    />}
  </div>
}

export function HelperResultGroupCard({ events, chatId, onInternalNav }) {
  const [open, setOpen] = useDisclosureState(
    chatId,
    `helper-results:${events.map(event => event.activityId || event.id).join(',')}`,
  )
  const headerRef = useRef(null)
  const detailRef = useRef(null)
  const headerId = useId()
  const detailId = useId()
  const working = events.filter(event => isWorkingHelper({ ...event, type: 'helper_result' })).length
  const finished = events.filter(event => event.status === 'completed').length
  const failed = events.length - finished - working
  const label = working
    ? [`${events.length} helpers`, `${working} working`,
        finished ? `${finished} finished` : null,
        failed ? `${failed} need attention` : null].filter(Boolean).join(' · ')
    : failed
    ? `${events.length} helper results · ${finished} finished, ${failed} need attention`
    : `${events.length} helpers finished`
  const date = peerTime(events.at(-1)?.created_at)
  const last = Number.isFinite(date) ? new Date(date) : null
  return <div className="chat__tool chat__tool--done chat__tool--compact chat__peer-tool chat__helper-result-group">
    <button ref={headerRef} id={headerId} type="button" className="chat__tool-header"
      aria-expanded={open} aria-controls={detailId} aria-label={label}
      onClick={() => {
        preserveTogglePosition(headerRef.current, detailRef.current)
        setOpen(value => !value)
      }}>
      <span className="chat__tool-icon" data-tool-kind="agents" aria-hidden="true"><ArrowDown width={14} height={14} /></span>
      <span className="chat__tool-name" title={label}>{label}</span>
      {last && <time className="chat__peer-time" dateTime={last.toISOString()} title={formatDateTime(last)}>{formatTime(last)}</time>}
    </button>
    <div ref={detailRef} id={detailId} className="chat__tool-detail chat__peer-detail chat__helper-result-group-detail" role="region"
      aria-labelledby={headerId} tabIndex={open ? 0 : undefined} hidden={!open}>
      {open && events.map(event => <HelperResultCard key={event.activityId || event.id} event={event} chatId={chatId} onInternalNav={onInternalNav} />)}
    </div>
  </div>
}
