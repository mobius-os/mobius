import { useEffect, useMemo, useRef, useState } from 'react'
import { StandardMarkdown } from './markdown/BlockRenderer.jsx'
import ToolBlock from './ToolBlock.jsx'
import {
  activityStreamState,
  activityCollapsedLabel,
  thoughtDurationLabel,
} from './groupBlocks.js'
import { toolBlockExitCode } from './toolResultFormat.js'
import { thinkingContentForDisplay } from './streamReducers.js'
import { assistantBlockKey } from './streamPromotion.js'
import { preserveTogglePosition } from './preserveTogglePosition.js'

// One collapsible activity line standing in for a contiguous stretch of thinking
// AND tool blocks, so a build turn's pre-prose burst reads as one quiet ~32px
// line instead of alternating "> Thought" lines and bordered tool cards — the
// answer keeps the screen. Collapsed, the borderless dim header carries live
// status (a spinner while a tool runs, a pulse dot + "Thinking for Ns" while the
// agent reasons) and a FAILED step's danger triangle + exit chip, all readable
// WITHOUT expanding. Expanded, it renders the chronological timeline: thinking
// bodies inline (dim StandardMarkdown) and tools as their own ToolBlock rows,
// which still own the lazy-fetch of large output.
//
// COLLAPSED BY DEFAULT, ALWAYS — the line never auto-opens; the user's tap is
// the only thing that opens or closes it, mid-run included. An earlier version
// of the tool-group card this replaces force-opened while any child was running
// (`open = running || userOpen`), on the theory that the live tool should stay
// visible mid-stream. That was wrong on two counts:
//   1. There is no running child in the gap between one tool ending and the
//      next starting, so it flapped open→closed→open at EVERY tool boundary and
//      snapped shut the instant the run finished.
//   2. Each flap changed the line's height, and `.chat__scroll` runs with
//      overflow-anchor:none plus manual scroll anchoring (see the "Chat UX —
//      non-negotiable constraints" reference), so the height churn displaced
//      whatever the reader was looking at.
// The premise was also false: liveness does NOT need the body open — the header
// spinner / pulse dot plus the running-first activity summary already say what
// is executing. So the sole open/close signal is `userOpen`, and no effect or
// prop derives it.
export default function ActivityStretch({ entries, chatId, live = false }) {
  const [userOpen, setUserOpen] = useState(false)
  const headerRef = useRef(null)

  const lastItem = entries[entries.length - 1]?.item
  const liveThinkingTail = live && lastItem?.type === 'thinking'
  const toolRunning = entries.some(
    e => e?.item?.type === 'tool' && e.item.status === 'running',
  )

  // The 1Hz ticker runs ONLY while the live tail is thinking — the sole moving
  // part of a collapsed stretch is then the "Thinking for Ns" clock. A live tool
  // run advertises itself with the spinner (no tick needed) and a settled stretch
  // never re-renders on its own. This is the exact scope of the old
  // ActiveThinkingDisclosure timer, moved here; the elapsed reads through
  // thinkingElapsedMs (inside activityCollapsedLabel) so it stays replay-invariant
  // across a reconnect burst.
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!liveThinkingTail) return undefined
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [liveThinkingTail])

  // Deriving the state parses each tool's output for its exit code, so memoize on
  // a cheap signature: tool name + status + output length + the output's TAIL
  // slice + explicit exit code + entry count + the live-thinking flag — NOT
  // thinking content length, so a typewriter thinking delta never busts the memo
  // while a co-rendering prose answer re-renders this component every frame. The
  // tail slice covers replace-semantics output (streamReducers sets it whole):
  // an equal-length replacement that flips the terminal envelope's exit code
  // changes the tail (`"exit_code":N}` lives there), so length alone can't leave
  // a stale success line. The name is in the sig because the label rollup below
  // reads it.
  const sig = entries
    .map(e => {
      const it = e?.item
      if (it?.type === 'tool') {
        return `t:${it.tool || ''}:${it.status || ''}:${it.output?.length || 0}`
          + `:${it.output?.slice(-14) || ''}:${it.output_exit_code ?? ''}`
      }
      return 'k'
    })
    .join('|') + `|${entries.length}|${liveThinkingTail ? 'T' : ''}`

  const meta = useMemo(() => {
    const tools = entries
      .filter(e => e?.item?.type === 'tool')
      .map(e => e.item)
    const state = activityStreamState(tools, { liveThinkingTail })
    // The collapsed exit chip shows the most-recent failed tool's code (the same
    // "exit N" the ToolBlock header carries), so a failed step is legible without
    // opening. Only computed once the stretch has settled to 'error'.
    let exitCode = null
    if (state === 'error') {
      for (const t of tools) {
        const code = toolBlockExitCode(t)
        if (code != null && code !== 0) exitCode = code
      }
    }
    return { state, exitCode, toolCount: tools.length, thinkingOnly: tools.length === 0 }
  }, [sig]) // eslint-disable-line react-hooks/exhaustive-deps

  const { state, exitCode, toolCount, thinkingOnly } = meta
  // The label is memoized on the same signature so a prose typewriter frame
  // never rebuilds the dedup'd activity rollup for every stretch above it. The
  // 1Hz `now` tick re-derives it only while the live thinking clock is the
  // moving part; `live` flips it once at settle.
  const { text, showEllipsis } = useMemo(
    () => activityCollapsedLabel(entries, { live, now }),
    [sig, live, now], // eslint-disable-line react-hooks/exhaustive-deps
  )

  // The user's toggle is the ONLY open/close signal — no force-open (see the
  // header comment). While collapsed, the header status carries liveness.
  const open = userOpen

  // The step count and failure detail ride in the accessible name only (the
  // visible line stays a calm activity summary); the one-second clock is not in
  // an aria-live region, so a screen reader is not re-announced every tick.
  const stepNote = toolCount > 0
    ? ` (${toolCount} ${toolCount === 1 ? 'step' : 'steps'})`
    : ''
  const stateNote = state === 'error'
    ? `, a step failed${exitCode != null ? ` with exit ${exitCode}` : ''}`
    : ''

  return (
    <div className={
      `chat__activity chat__activity--${state}`
      + (live ? ' chat__activity--live' : '')
      + (open ? ' chat__activity--open' : '')
    }>
      <button
        ref={headerRef}
        type="button"
        className="chat__activity-header"
        // Togglable at any time, running or not: with default-collapse there is
        // no forced-open state for a tap to fight, so the user can peek into a
        // live run and close it again.
        onClick={() => {
          preserveTogglePosition(headerRef.current)
          setUserOpen(o => !o)
        }}
        aria-expanded={open}
        aria-label={`${text}${stepNote}${stateNote}`}
      >
        {liveThinkingTail && !toolRunning ? (
          // The agent is reasoning now — a quiet pulse, distinct from the spinner.
          <span className="chat__activity-icon chat__activity-icon--think" aria-hidden="true" />
        ) : state === 'running' ? (
          <span className="chat__tool-spin" />
        ) : state === 'error' ? (
          <span className="chat__activity-icon" aria-hidden="true">
            {/* triangle — a step failed */}
            <svg viewBox="0 0 16 16" width="13" height="13" fill="none"
              stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"
              strokeLinejoin="round">
              <path d="M8 2 15 14H1z" /><path d="M8 6v4" /><path d="M8 12h.01" />
            </svg>
          </span>
        ) : (
          // Settled success has NO leading icon — success is the default state,
          // not a signal (ChatGPT-style: a checkmark on every done run is noise).
          null
        )}
        <span className="chat__activity-label">{text}</span>
        {showEllipsis && (
          <span className="chat__reasoning-ellipsis" aria-hidden="true">
            <span />
            <span />
            <span />
          </span>
        )}
        {state === 'error' && exitCode != null && (
          <span className="chat__activity-chip">exit {exitCode}</span>
        )}
        {/* No chevron: the line IS the affordance (owner call, 2026-07-16).
            aria-expanded still announces the disclosure state, and the open
            timeline below makes the expanded state visually obvious. */}
      </button>
      {open && (
        <div className="chat__activity-timeline">
          {entries.map(({ item, idx }) => {
            if (item.type === 'thinking') {
              return (
                <div className="chat__activity-think" key={assistantBlockKey(item, idx)}>
                  {/* For a thinking-ONLY stretch the collapsed header already said
                      "Thought for Ns", so this redundant row label is suppressed and
                      the body shows directly — byte-for-byte the old reasoning UX. */}
                  {!thinkingOnly && (
                    <span className="chat__activity-think-label">
                      {thoughtDurationLabel(item.duration_ms)}
                    </span>
                  )}
                  <div className="chat__reasoning-body">
                    <StandardMarkdown text={thinkingContentForDisplay(item.content)} />
                  </div>
                </div>
              )
            }
            // chatId + the block's tool_use_id let ToolBlock lazily fetch a
            // truncated large output on expand (GET /tool-output/{tool_use_id}).
            return (
              <ToolBlock key={assistantBlockKey(item, idx)} t={item} chatId={chatId} />
            )
          })}
        </div>
      )}
    </div>
  )
}
