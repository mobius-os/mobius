import { useMemo, useState } from 'react'
import { toolGroupState, toolGroupSummary } from './groupBlocks.js'

// One collapsible "Activity" card standing in for a run of adjacent tool calls,
// so a build turn's 20-tool wall doesn't bury the agent's prose. The header
// summarizes the run ("Read, Edit, Bash +2") and carries a status chip so a
// FAILED step is visible without expanding. Children are the individual
// ToolBlocks, rendered by the caller (which owns their lazy-fetch props) and
// shown only when expanded.
//
// Collapsed by default (owner's call), but auto-expands while any child is
// running so the live tool stays visible mid-stream — matching the existing
// defaultOpen-on-running behavior for standalone tool blocks. Once the run
// finishes it does NOT auto-collapse (the user may be reading it).
export default function ToolActivityGroup({ tools, children }) {
  // Deriving the state parses each child's output (for the exit-code failure
  // check), so memoize on a cheap status+output-length signature: it only
  // recomputes when a tool actually changes (status flip, or output grows
  // mid-stream), NOT on every typewriter frame of a co-rendering prose answer.
  const sig = tools.map(t => `${t?.status || ''}:${t?.output?.length || 0}`).join('|')
  const state = useMemo(() => toolGroupState(tools), [sig]) // eslint-disable-line react-hooks/exhaustive-deps
  const running = state === 'running'
  const [userOpen, setUserOpen] = useState(false)
  // While running, force open so the live tool stays visible; otherwise honor
  // the user's toggle (default collapsed). A run that finishes untouched
  // collapses on its own, since `running` drops to false and `userOpen` is
  // still false — matching "collapsed by default" without persisting the
  // forced-open state.
  const open = running || userOpen

  const summary = toolGroupSummary(tools)

  return (
    <div className={`chat__toolgroup chat__toolgroup--${state}`}>
      <button
        type="button"
        className="chat__toolgroup-header"
        // While running the group is force-open to keep the live tool visible,
        // so a tap must not silently flip the hidden userOpen state (which would
        // otherwise decide the collapsed state the instant the run finishes).
        onClick={() => { if (!running) setUserOpen(o => !o) }}
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
        <span className="chat__toolgroup-summary">{summary}</span>
        <span className="chat__toolgroup-count">{tools.length} tools</span>
        {state === 'error' && (
          <span className="chat__toolgroup-chip">Failed</span>
        )}
        <span className="chat__toolgroup-toggle" aria-hidden="true">
          {open ? '▾' : '▸'}
        </span>
      </button>
      {open && <div className="chat__toolgroup-body">{children}</div>}
    </div>
  )
}
