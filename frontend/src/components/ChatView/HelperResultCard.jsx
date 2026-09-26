/* A helper's one activity row; opening its conversation never resumes its parent. */
import { useEffect, useId, useRef, useState } from 'react'
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
import { modelDisplayName } from './chatUsageFormat.js'
import { modelQueries } from '../../hooks/queries.js'

const PROVIDER_NAMES = { claude: 'Claude', codex: 'Codex', mobius: 'Möbius' }

// Which model a helper runs on, by the name the model picker shows without its
// provider prefix ("Opus 4.8", "GPT-6-Sol"). A model the registry does not name
// keeps its provider and raw id ("Claude · claude-opus-9").
export function helperEngine(event, registry) {
  const provider = PROVIDER_NAMES[event.provider] || event.provider
  const label = modelDisplayName(event.model, registry, event.provider)
  if (!label || label === event.model.trim()) {
    return [provider, event.model].filter(Boolean).join(' · ')
  }
  return provider && label.startsWith(`${provider} `) ? label.slice(provider.length + 1) : label
}

// The helper's newest step in owner language ("Running npm test"), or null.
function helperStep(event) {
  const activity = event.activity
  if (!activity || typeof activity.tool !== 'string') return null
  return toolCallLabel({ tool: activity.tool, input: activity.summary || '', status: 'running' })
}

// One ticking clock while anything visible runs, from the helper's start time.
// The server records that time in UTC without a zone suffix; read it as UTC,
// never as the viewer's local time, or the clock is off by their UTC offset.
function useElapsed(startedAt, running) {
  const start = peerTime(startedAt)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!running || !Number.isFinite(start)) return undefined
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [running, start])
  return Number.isFinite(start) ? elapsedLabel(now - start) : null
}


// What a settled helper's row says in place of its live step.
const SETTLED = {
  completed: ['done', 'Finished'],
  failed: ['failed', 'Failed'],
  needs_review: ['failed', 'Needs review'],
}

/* A helper's one row, from launch to result: its name, provider and model,
   then what it is doing now with a running clock, or how it ended and how long
   it took. One tap opens its conversation (task, steps and result) over this
   chat, read-only and updating live while it works. */
export function HelperRow({ event, chatId, onInternalNav }) {
  const [open, setOpen] = useState(false)
  const rowRef = useRef(null)
  const name = event.task_key || 'Helper'
  const working = isWorkingHelper({ ...event, type: 'helper_result' })
  const paused = event.status === 'paused'
  const live = useElapsed(event.started_at, working && !paused)
  const [dot, outcome] = working
    ? [paused ? 'failed' : 'running', paused ? 'Paused' : helperStep(event) || 'Working']
    : SETTLED[event.status] || ['failed', 'Stopped']
  const elapsed = working
    ? live
    : Number.isFinite(event.duration_ms) ? elapsedLabel(event.duration_ms) : null
  const registry = modelQueries.registry.useQuery().data
  const sub = [helperEngine(event, registry), outcome].filter(Boolean).join(' · ')
  const canOpen = !!(chatId && event.delegation_id)
  const running = working && !paused
  const body = <>
    <span className={`chat__subagent-dot chat__subagent-dot--${dot}`} aria-hidden="true" />
    <span className="chat__subagent-body">
      <span className="chat__subagent-name">
        <span className="chat__subagent-name-text">{name}</span>
        {running && <span className="chat__subagent-name-sweep" aria-hidden="true">{name}</span>}
      </span>
      <span className="chat__subagent-sub">{sub}</span>
    </span>
    {elapsed && <span className="chat__subagent-elapsed">{elapsed}</span>}
    {canOpen && <ChevronRight className="chat__subagent-open" width={14} height={14} aria-hidden="true" />}
  </>
  const rowClass = `chat__subagent${running ? ' chat__subagent--running' : ''}`
  return <div ref={rowRef} className={`chat__subagents-list chat__helper-row${working ? ' chat__helper-working' : ''}`}>
    {canOpen
      ? <button
          type="button"
          className={`${rowClass} chat__subagent--openable`}
          aria-label={`${name}: ${sub}. Open its conversation`}
          aria-haspopup="dialog"
          onClick={() => setOpen(true)}
        >{body}</button>
      : <div className={rowClass}>{body}</div>}
    {open && <HelperConversation
      chatId={chatId}
      taskId={event.delegation_id}
      name={name}
      status={working ? (paused ? 'stopped' : 'running') : dot === 'done' ? 'done' : event.status === 'failed' ? 'failed' : 'stopped'}
      host={rowRef.current?.closest('.chat')}
      onClose={() => setOpen(false)}
      onInternalNav={onInternalNav}
    />}
  </div>
}

export default function HelperResultCard({ event, chatId, onInternalNav }) {
  return <HelperRow event={event} chatId={chatId} onInternalNav={onInternalNav} />
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
