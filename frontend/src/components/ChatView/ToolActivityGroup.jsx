import { useMemo, useRef, useState } from 'react'
import { toolGroupState, toolGroupSummary } from './groupBlocks.js'
import { preserveTogglePosition } from './preserveTogglePosition.js'

// One collapsible "Activity" card standing in for a run of adjacent tool calls,
// so a build turn's 20-tool wall doesn't bury the agent's prose. The header
// summarizes the run ("Reading files · Editing code +2") and carries a status
// chip so a FAILED step is visible without expanding. Children are the
// individual ToolBlocks, rendered by the caller (which owns their lazy-fetch
// props) and shown only when expanded.
//
// COLLAPSED BY DEFAULT, ALWAYS — the card never auto-opens; the user's tap is
// the only thing that opens or closes it, mid-run included. An earlier version
// force-opened while any child was running (`open = running || userOpen`), on
// the theory that the live tool should stay visible mid-stream. That was wrong
// on two counts:
//   1. There is no running child in the gap between one tool ending and the
//      next starting, so the card flapped open→closed→open at EVERY tool
//      boundary and snapped shut the instant the run finished.
//   2. Each flap changed the card's height, and `.chat__scroll` runs with
//      overflow-anchor:none plus manual scroll anchoring (see the "Chat UX —
//      non-negotiable constraints" reference), so the height churn displaced
//      whatever the reader was looking at.
// The premise was also false: standalone stream tool blocks never carry
// defaultOpen, so a lone running ToolBlock already renders collapsed with just
// a header spinner. Liveness does NOT need the body open — the header spinner
// plus the running-tool-first summary (toolGroupSummary) already say what is
// executing. So the sole open/close signal is `userOpen`.
export default function ToolActivityGroup({ tools, children }) {
  // Deriving the state parses each child's output (for the exit-code failure
  // check), so memoize on a cheap status+output-length signature: it only
  // recomputes when a tool actually changes (status flip, or output grows
  // mid-stream), NOT on every typewriter frame of a co-rendering prose answer.
  const sig = tools.map(t => `${t?.status || ''}:${t?.output?.length || 0}`).join('|')
  const state = useMemo(() => toolGroupState(tools), [sig]) // eslint-disable-line react-hooks/exhaustive-deps
  const running = state === 'running'
  const [userOpen, setUserOpen] = useState(false)
  const headerRef = useRef(null)
  // The user's toggle is the ONLY open/close signal — no force-open (see the
  // header comment for why removing it fixed the boundary flap + scroll
  // displacement). While collapsed, the header spinner carries live status.
  const open = userOpen

  const summary = toolGroupSummary(tools)
  const countLabel = `${tools.length} tool call${tools.length === 1 ? '' : 's'}`
  const title = running
    ? `Running ${countLabel}`
    : state === 'error'
      ? `${countLabel} failed`
      : countLabel

  return (
    <div className={`chat__toolgroup chat__toolgroup--${state}`}>
      <button
        ref={headerRef}
        type="button"
        className="chat__toolgroup-header"
        // Togglable at any time, running or not: with default-collapse there is
        // no forced-open state for a tap to fight, so the user can peek into a
        // live run and close it again.
        onClick={() => {
          preserveTogglePosition(headerRef.current)
          setUserOpen(o => !o)
        }}
        aria-expanded={open}
      >
        {running
          ? <span className="chat__tool-spin" />
          : (
            <span className="chat__toolgroup-icon" aria-hidden="true">
              {state === 'error'
                ? (
                  /* triangle — a step failed */
                  <svg viewBox="0 0 16 16" width="13" height="13" fill="none"
                    stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"
                    strokeLinejoin="round">
                    <path d="M8 2 15 14H1z" /><path d="M8 6v4" /><path d="M8 12h.01" />
                  </svg>
                )
                : (
                  /* check — the run completed */
                  <svg viewBox="0 0 16 16" width="13" height="13" fill="none"
                    stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"
                    strokeLinejoin="round">
                    <path d="M3 8.5 6.5 12 13 4.5" />
                  </svg>
                )}
            </span>
          )}
        <span className="chat__toolgroup-summary">
          <span className="chat__toolgroup-title">{title}</span>
          {summary && (
            <span className="chat__toolgroup-activity">{summary}</span>
          )}
        </span>
        {state === 'error' && (
          <span className="chat__toolgroup-chip">Failed</span>
        )}
        <span className="chat__toolgroup-toggle" aria-hidden="true">
          <svg
            className={`chat__chevron${open ? '' : ' chat__chevron--collapsed'}`}
            width="10" height="10" viewBox="0 0 10 10" fill="none"
          >
            <path d="M2 4l3 3 3-3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </span>
      </button>
      {open && <div className="chat__toolgroup-body">{children}</div>}
    </div>
  )
}
