import { useId, useMemo, useRef } from 'react'
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
import ActivityLineHeader, { ActivityTypeIcon } from './ActivityLineHeader.jsx'
import SubagentChips from './SubagentChips.jsx'
import { useThinkingTrace } from './useThinkingTrace.js'
import { useDisclosureState } from './disclosureState.js'

// One collapsible activity line standing in for a MULTI-STEP contiguous stretch
// of thinking and tool blocks, so a build turn's pre-prose burst reads as one
// quiet ~32px line instead of alternating rows — the answer keeps the screen.
// A lone thought/tool renders as its own disclosure (see SingleActivity below):
// wrapping one row in an identical parent adds hierarchy without information.
// Collapsed, the borderless dim header carries live
// status (a periodic shimmer over the label — bare "Thinking", or the muted
// type glyph + progressive activities while tools run) and a FAILED step's
// danger triangle + exit chip, all readable
// WITHOUT expanding. Expanded, it renders the chronological timeline: mixed
// thinking entries and tools become independently collapsed child rows, so
// opening the overview never spills a full reasoning trace or tool output into
// the transcript. A thought keeps this same child component as tools arrive,
// avoiding an automatic height/state change in the middle of a live run.
//
// COLLAPSED ON FIRST ENCOUNTER, then restored from this chat's session screen
// state — the line never auto-opens; the user's tap is the only thing that
// changes its saved open/closed value, mid-run included. An earlier version
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
function TimelineThought({ label, thought, chatId, disclosureKey, direct = false, live = false }) {
  const [open, setOpen] = useDisclosureState(chatId, disclosureKey)
  const headerRef = useRef(null)
  const bodyRef = useRef(null)
  const bodyId = useId()
  const trace = useThinkingTrace({ open, thought, chatId })
  const content = thinkingContentForDisplay(trace.content)
  const loadState = trace.loadState
  let body = <StandardMarkdown text={content} />
  if (loadState === 'loading') {
    body = (
      <span className="chat__reasoning-load" role="status" aria-live="polite">
        Loading thought…
      </span>
    )
  } else if (loadState === 'failed') {
    body = (
      <div className="chat__lazy-status">
        <span className="chat__reasoning-load" role="status" aria-live="polite">
          Thought unavailable.
        </span>
        <button type="button" className="chat__lazy-retry" onClick={trace.retry}>
          Retry
        </button>
      </div>
    )
  }

  const toggle = () => {
    preserveTogglePosition(headerRef.current, bodyRef.current)
    setOpen(o => !o)
  }

  return (
    <div className={direct
      ? `chat__activity chat__activity--direct-thought chat__activity--${live ? 'running' : 'done'}`
        + (open ? ' chat__activity--open' : '')
      : `chat__activity-think chat__activity-think--collapsible`
        + (open ? ' chat__activity-think--open' : '')
    }>
      {direct ? (
        <ActivityLineHeader
          ref={headerRef}
          text={label}
          displayState={live ? 'running' : 'done'}
          iconKind="reasoning"
          interactive
          open={open}
          ariaLabel={`${label}${live ? ', in progress' : ''}`}
          controlsId={bodyId}
          onToggle={toggle}
        />
      ) : (
        <button
          ref={headerRef}
          type="button"
          className="chat__activity-think-toggle"
          onClick={toggle}
          aria-expanded={open}
          aria-controls={bodyId}
        >
          <span className="chat__activity-think-icon" aria-hidden="true">
            <ActivityTypeIcon kind="reasoning" />
          </span>
          <span className="chat__activity-think-label">{label}</span>
        </button>
      )}
      <div ref={bodyRef} id={bodyId} className="chat__reasoning-body" hidden={!open}>
        {open && body}
        {open && loadState === 'ready' && !trace.previewComplete && (
          <div className="chat__lazy-status chat__reasoning-preview-status">
            <span>
              {trace.traceComplete
                ? 'Showing a bounded preview to keep this chat responsive.'
                : 'Showing a bounded preview while this thought is in progress.'}
            </span>
            {trace.traceComplete && (
              <button
                type="button"
                className="chat__lazy-retry"
                onClick={trace.loadFull}
              >
                Load full thought
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function SingleActivity({ entry, chatId, live, surfaceKey }) {
  const { item, idx } = entry
  const blockKey = assistantBlockKey(item, idx)
  if (item.type === 'thinking') {
    return (
      <TimelineThought
        key={assistantBlockKey(item, idx)}
        label={live ? 'Thinking' : thoughtDurationLabel(item.duration_ms)}
        thought={item}
        chatId={chatId}
        disclosureKey={`${surfaceKey}:thought:${blockKey}`}
        direct
        live={live}
      />
    )
  }
  const hasHelpers = item.subagent
    && typeof item.subagent === 'object'
    && Object.keys(item.subagent).length > 0
  return (
    <>
      <ToolBlock
        key={assistantBlockKey(item, idx)}
        t={item}
        chatId={chatId}
        compact
        disclosureKey={`${surfaceKey}:tool:${blockKey}`}
      />
      {hasHelpers && <SubagentChips subagent={item.subagent} />}
    </>
  )
}

function GroupedActivityStretch({ entries, chatId, live = false, surfaceKey }) {
  const stretchKey = assistantBlockKey(entries[0]?.item, entries[0]?.idx)
  const [userOpen, setUserOpen] = useDisclosureState(
    chatId,
    `${surfaceKey}:activity:${stretchKey}`,
  )
  const headerRef = useRef(null)
  const timelineRef = useRef(null)
  const timelineId = useId()

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
        controlsId={timelineId}
        // Togglable at any time, running or not: with default-collapse there is
        // no forced-open state for a tap to fight, so the user can peek into a
        // live run and close it again.
        onToggle={() => {
          preserveTogglePosition(headerRef.current, timelineRef.current)
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
      <div ref={timelineRef} id={timelineId} className="chat__activity-timeline" hidden={!open}>
        {open && entries.map(({ item, idx }) => {
          if (item.type === 'thinking') {
            const key = assistantBlockKey(item, idx)
            return (
              <TimelineThought
                key={key}
                label={thoughtDurationLabel(item.duration_ms)}
                thought={item}
                chatId={chatId}
                disclosureKey={`${surfaceKey}:thought:${key}`}
              />
            )
          }
          // chatId + the block's tool_use_id let ToolBlock lazily fetch a
          // truncated large output on expand (GET /tool-output/{tool_use_id}).
          return (
            <ToolBlock
              key={assistantBlockKey(item, idx)}
              t={item}
              chatId={chatId}
              disclosureKey={`${surfaceKey}:tool:${assistantBlockKey(item, idx)}`}
            />
          )
        })}
      </div>
    </div>
  )
}

export default function ActivityStretch({ entries, chatId, live = false, surfaceKey }) {
  if (entries.length === 1) {
    return (
      <SingleActivity
        entry={entries[0]}
        chatId={chatId}
        live={live}
        surfaceKey={surfaceKey}
      />
    )
  }
  return (
    <GroupedActivityStretch
      entries={entries}
      chatId={chatId}
      live={live}
      surfaceKey={surfaceKey}
    />
  )
}
