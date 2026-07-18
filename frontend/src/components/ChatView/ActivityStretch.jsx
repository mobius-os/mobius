import { useMemo, useRef, useState } from 'react'
import { StandardMarkdown } from './markdown/BlockRenderer.jsx'
import ToolBlock from './ToolBlock.jsx'
import {
  activityStreamState,
  activityDisplayState,
  activityMemoSig,
  activityCollapsedLabel,
  thoughtDurationLabel,
} from './groupBlocks.js'
import { toolBlockExitCode } from './toolResultFormat.js'
import { toolActivityIcon, effectiveToolName } from './toolActivityLabel.js'
import { thinkingContentForDisplay } from './streamReducers.js'
import { assistantBlockKey } from './streamPromotion.js'
import { preserveTogglePosition } from './preserveTogglePosition.js'
import ActivityLineHeader from './ActivityLineHeader.jsx'
import SubagentChips from './SubagentChips.jsx'

// One collapsible activity line standing in for a contiguous stretch of thinking
// AND tool blocks, so a build turn's pre-prose burst reads as one quiet ~32px
// line instead of alternating "> Thought" lines and bordered tool cards — the
// answer keeps the screen. Collapsed, the borderless dim header carries live
// status (a periodic shimmer over the label — bare "Thinking", or the muted
// type glyph + progressive activities while tools run) and a FAILED step's
// danger triangle + exit chip, all readable
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
// The premise was also false: liveness does NOT need the body open — the label
// shimmer plus the running-first activity summary already say what is
// executing. So the sole open/close signal is `userOpen`, and no effect or
// prop derives it.
export default function ActivityStretch({ entries, chatId, live = false }) {
  const [userOpen, setUserOpen] = useState(false)
  const headerRef = useRef(null)

  const lastItem = entries[entries.length - 1]?.item
  const liveThinkingTail = live && lastItem?.type === 'thinking'
  // The line's glyph matches its LEADING label word: the currently-running
  // tool's activity while one runs (toolGroupSummary leads with it), else the
  // first-seen activity (the past-tense sentence leads with that).
  const leadTool = [...entries].reverse()
    .find(e => e?.item?.type === 'tool' && e.item.status === 'running')?.item
    || entries.find(e => e?.item?.type === 'tool')?.item
  const leadToolIcon = toolActivityIcon(effectiveToolName(leadTool))

  // A delegating turn's Task/Agent tool blocks carry a `.subagent` map of live
  // (streamReducers.applyTaskEvent) or persisted (backend 247) helper metadata.
  // Render those helpers as rows in this stretch and surface a running/done
  // count on the header — the header's activity word already reads "Working in
  // the background", so the count is the only addition. The Task ToolBlock is
  // NOT hidden: its output/expand stays reachable in the expanded timeline.
  const subagentTools = entries
    .map(e => e?.item)
    .filter(it => it?.type === 'tool'
      && it.subagent
      && typeof it.subagent === 'object'
      && Object.keys(it.subagent).length > 0)
  const subagentHelpers = subagentTools
    .flatMap(it => Object.values(it.subagent))
    .filter(h => h && typeof h === 'object')
  const runningHelpers = subagentHelpers.filter(h => h.status === 'running').length
  const failedHelpers = subagentHelpers.filter(
    h => h.status === 'failed' || h.status === 'killed' || h.status === 'stopped'
  ).length
  const doneHelpers = subagentHelpers.length - runningHelpers - failedHelpers
  // Count successes and failures separately: a failed/killed helper is not
  // "done", and the header must not label a red-dotted row as done.
  const subagentCount = subagentHelpers.length > 0
    ? [
        runningHelpers > 0 ? `${runningHelpers} running` : null,
        doneHelpers > 0 ? `${doneHelpers} done` : null,
        failedHelpers > 0 ? `${failedHelpers} failed` : null,
      ].filter(Boolean).join(' · ')
    : null
  // Deriving the state parses each tool's output for its exit code, so memoize
  // on a cheap signature (see activityMemoSig for the exact staleness contract:
  // head+tail output slices catch an equal-length exit-code flip; thinking
  // content never busts the memo on typewriter frames).
  const sig = activityMemoSig(entries, { liveThinkingTail })

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
  // The one presentation authority for icon, chip, and state class: a live
  // stretch reads in-progress for its whole life — the tool→tool gap included —
  // so icon and tense can never contradict (see activityDisplayState). Applied
  // OUTSIDE the memo because `live` is not part of the signature.
  const displayState = activityDisplayState(state, { live })
  // The label is memoized on the same signature so a prose typewriter frame
  // never rebuilds the dedup'd activity rollup for every stretch above it;
  // `live` flips it once at settle.
  const text = useMemo(
    () => activityCollapsedLabel(entries, { live }),
    [sig, live], // eslint-disable-line react-hooks/exhaustive-deps
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
  const stateNote = displayState === 'error'
    ? `, a step failed${exitCode != null ? ` with exit ${exitCode}` : ''}`
    : displayState === 'running'
      ? ', in progress'
      : ''
  const iconKind = thinkingOnly ? 'reasoning' : leadToolIcon

  return (
    <div className={
      `chat__activity chat__activity--${displayState}`
      + (open ? ' chat__activity--open' : '')
    }>
      <ActivityLineHeader
        ref={headerRef}
        text={text}
        displayState={displayState}
        iconKind={iconKind}
        exitCode={exitCode}
        interactive
        open={open}
        ariaLabel={`${text}${stepNote}${stateNote}`}
        // Togglable at any time, running or not: with default-collapse there is
        // no forced-open state for a tap to fight, so the user can peek into a
        // live run and close it again.
        onToggle={() => {
          preserveTogglePosition(headerRef.current)
          setUserOpen(o => !o)
        }}
        // A delegating turn's helper rollup ("2 running · 1 done"); the header
        // owns it so it reads without expanding the line.
        count={subagentCount}
      />
      {/* Helper rows for a delegating turn, ALWAYS visible (not gated on the
          disclosure) so live subagent progress reads without expanding. The
          raw Task ToolBlock — its output + inspection — stays in the timeline
          below. One SubagentChips per Task/Agent tool block that has helpers. */}
      {subagentTools.map((tool, i) => (
        <SubagentChips
          key={tool.tool_use_id ?? `subagent-${i}`}
          subagent={tool.subagent}
        />
      ))}
      {open && (
        <div className="chat__activity-timeline">
          {entries.map(({ item, idx }) => {
            if (item.type === 'thinking') {
              // The stretch itself is the ONLY disclosure: opening it preserves the
              // reasoning exactly as generated, with no second toggle to click
              // (owner call). A mixed run keeps a plain duration label so the
              // thought reads apart from the tool rows beside it; a thinking-only
              // stretch already named it in the collapsed header.
              return (
                <div className="chat__activity-think" key={assistantBlockKey(item, idx)}>
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
