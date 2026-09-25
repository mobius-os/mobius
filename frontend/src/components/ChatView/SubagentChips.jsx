import { useEffect, useState } from 'react'
import { ChevronRight } from '@openai/apps-sdk-ui/components/Icon'
import './SubagentChips.css'
import { toolActivityLabel } from './toolActivityLabel.js'
import HelperConversation from './HelperConversation.jsx'
import { agentHelperEntries, elapsedLabel } from './toolTasks.js'

// Helper ROWS for a delegating turn's background subagents, rendered inside an
// ActivityStretch when its Task/Agent tool block carries a `.subagent` map
// (streamReducers.applyTaskEvent stamps it live; backend card 247 persists the
// same shape, so live/promoted/reloaded render identically). This component
// owns ONLY the rows — the enclosing ActivityStretch header owns the "Working
// in the background" label and the running/done count, so there is no header
// here. Renders nothing when `.subagent` holds no agent helper.

// Owner-language: the chip name is ALWAYS the helper's `description` — never
// task_type, never "subagent"/"Task". If a collab-op prefix ever leaks onto the
// description, strip it so the chip stays human.
const OP_PREFIX_RE = /^(spawnAgent|wait|Task|Agent)\s*:\s*/i
function helperName(description) {
  const raw = String(description || '').trim().replace(OP_PREFIX_RE, '').trim()
  return raw || 'Working in the background'
}

// Best-effort elapsed. task_* events currently carry NO ts, so this is anchored
// on the client-stamped startedAt (first task_start) — a helper that reconnects
// with only a start resets its clock, which is acceptable; this is deliberately
// NOT replay-invariant. A settled helper prefers a runner-measured usage span
// when one exists, else the client start→last span.
function elapsedMs(helper, now) {
  const started = Number.isFinite(helper.startedAt) ? helper.startedAt : null
  if (helper.status === 'running') {
    return started != null ? Math.max(0, now - started) : null
  }
  const durMs = helper.usage && Number.isFinite(helper.usage.duration_ms)
    ? helper.usage.duration_ms
    : null
  if (durMs != null) return durMs
  if (started != null && Number.isFinite(helper.lastAt)) {
    return Math.max(0, helper.lastAt - started)
  }
  return null
}

// The muted sub-line under a row: while running, the helper's current activity
// (owner-language, e.g. "Reading files") from its last tool; once settled, its
// one-line summary if the backend provided one.
function subLine(helper) {
  if (helper.status === 'running') {
    // String-coerce before toolActivityLabel: it returns its input verbatim for
    // an unknown tool name, so a non-string (SDK shape drift / a malformed
    // persisted block) would otherwise reach a React child and throw "Objects
    // are not valid as a React child". The runner also clips this at emission;
    // this is the render-side backstop for legacy/other-provider data.
    return helper.last_tool_name != null
      ? toolActivityLabel(String(helper.last_tool_name)) : null
  }
  return helper.summary != null ? String(helper.summary) : null
}

function StatusDot({ status }) {
  // muted = running, green = done, red = failed/killed/stopped. A small dot is a
  // status indicator, not a success checkmark — it does not read as celebratory.
  const cls = status === 'running'
    ? 'chat__subagent-dot--running'
    : status === 'done'
      ? 'chat__subagent-dot--done'
      : 'chat__subagent-dot--failed'
  return <span className={`chat__subagent-dot ${cls}`} aria-hidden="true" />
}

export default function SubagentChips({ subagent, chatId, onInternalNav }) {
  // Only agent helpers are rows; a shell task is its command's own row.
  const helpers = agentHelperEntries({ subagent })
  const anyRunning = helpers.some(([, h]) => h.status === 'running')

  // One 1s ticker advances the elapsed labels while anything runs, stopping the
  // moment every helper settles. Rows are fixed-height and their text single-
  // lines, so a tick never changes height and can't displace the reader (the
  // scroll model runs overflow-anchor:none — see the Chat UX contract).
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!anyRunning) return undefined
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [anyRunning])

  // { taskId, host }: the open helper and the chat pane its dialog covers.
  const [opened, setOpened] = useState(null)

  if (helpers.length === 0) return null
  const openHelper = opened && helpers.find(([taskId]) => taskId === opened.taskId)?.[1]

  return (
    <div className="chat__subagents-list">
      {helpers.map(([taskId, helper]) => {
        const name = helperName(helper.description)
        const sub = subLine(helper)
        const isRunning = helper.status === 'running'
        const ms = elapsedMs(helper, now)
        const elapsed = ms != null ? elapsedLabel(ms) : null
        const openable = !!chatId
        const Row = openable ? 'button' : 'div'
        return (
          <Row
            key={taskId}
            {...(openable && {
              type: 'button',
              'aria-label': `Open ${name} conversation`,
              'aria-haspopup': 'dialog',
              onClick: event => setOpened({ taskId, host: event.currentTarget.closest('.chat') }),
            })}
            className={
              'chat__subagent'
              + (openable ? ' chat__subagent--openable' : '')
              + (isRunning ? ' chat__subagent--running' : '')
              + (helper.status === 'failed' || helper.status === 'killed'
                  || helper.status === 'stopped'
                  ? ' chat__subagent--failed' : '')
            }
          >
            <StatusDot status={helper.status} />
            <span className="chat__subagent-body">
              {/* Liveness is the label shimmer (masked bright sweep over the
                  muted base), never a spinner — the activity line's idiom. The
                  sweep paints only while running. */}
              <span className="chat__subagent-name">
                <span className="chat__subagent-name-text">{name}</span>
                {isRunning && (
                  <span className="chat__subagent-name-sweep" aria-hidden="true">
                    {name}
                  </span>
                )}
              </span>
              {sub && <span className="chat__subagent-sub">{sub}</span>}
            </span>
            {elapsed && <span className="chat__subagent-elapsed">{elapsed}</span>}
            {openable && <ChevronRight className="chat__subagent-open" width={14} height={14} aria-hidden="true" />}
          </Row>
        )
      })}
      {openHelper && (
        <HelperConversation
          chatId={chatId}
          taskId={opened.taskId}
          name={helperName(openHelper.description)}
          status={openHelper.status}
          host={opened.host}
          onClose={() => setOpened(null)}
          onInternalNav={onInternalNav}
        />
      )}
    </div>
  )
}
