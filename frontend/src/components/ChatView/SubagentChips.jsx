import { useEffect, useState } from 'react'
import './SubagentChips.css'
import { toolActivityLabel } from './toolActivityLabel.js'

// Helper ROWS for a delegating turn's background subagents, rendered inside an
// ActivityStretch when its Task/Agent tool block carries a `.subagent` map
// (streamReducers.applyTaskEvent stamps it live; backend card 247 persists the
// same shape, so live/promoted/reloaded render identically). This component
// owns ONLY the rows — the enclosing ActivityStretch header owns the "Working
// in the background" label and the running/done count, so there is no header
// here. Renders nothing when `.subagent` is absent or empty (Codex delegations
// surface as an ordinary background-work activity, with no per-helper chips).

// Owner-language: the chip name is ALWAYS the helper's `description` — never
// task_type, never "subagent"/"Task". If a collab-op prefix ever leaks onto the
// description, strip it so the chip stays human.
const OP_PREFIX_RE = /^(spawnAgent|wait|Task|Agent)\s*:\s*/i
function helperName(description) {
  const raw = String(description || '').trim().replace(OP_PREFIX_RE, '').trim()
  return raw || 'Working in the background'
}

// Whole-second elapsed, compact ("8s", "1m 04s").
function elapsedLabel(ms) {
  const total = Math.max(0, Math.round(ms / 1000))
  if (total < 60) return `${total}s`
  const mins = Math.floor(total / 60)
  const secs = total % 60
  return `${mins}m ${String(secs).padStart(2, '0')}s`
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
    return helper.last_tool_name ? toolActivityLabel(helper.last_tool_name) : null
  }
  return helper.summary ? String(helper.summary) : null
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

export default function SubagentChips({ subagent }) {
  const helpers = subagent && typeof subagent === 'object'
    ? Object.entries(subagent)
    : []
  const anyRunning = helpers.some(([, h]) => h?.status === 'running')

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

  if (helpers.length === 0) return null

  return (
    <div className="chat__subagents-list">
      {helpers.map(([taskId, helper]) => {
        const name = helperName(helper.description)
        const sub = subLine(helper)
        const isRunning = helper.status === 'running'
        const ms = elapsedMs(helper, now)
        const elapsed = ms != null ? elapsedLabel(ms) : null
        return (
          <div
            key={taskId}
            className={
              'chat__subagent'
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
          </div>
        )
      })}
    </div>
  )
}
