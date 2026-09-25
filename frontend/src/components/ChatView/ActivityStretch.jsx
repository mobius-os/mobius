import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { StandardMarkdown } from './markdown/BlockRenderer.jsx'
import ToolBlock from './ToolBlock.jsx'
import { apiFetch, jsonOrThrow } from '../../api/client.js'
import {
  activityStreamState,
  activityDisplayState,
  activityMemoSig,
  activityCollapsedLabel,
  activitySummaryTools,
  thoughtDurationLabel,
} from './groupBlocks.js'
import { toolActivityIcon, effectiveToolName } from './toolActivityLabel.js'
import { thinkingContentForDisplay } from './streamReducers.js'
import { assistantBlockKey } from './streamPromotion.js'
import { preserveTogglePosition } from './preserveTogglePosition.js'
import ActivityLineHeader, { ActivityTypeIcon } from './ActivityLineHeader.jsx'
import SubagentChips from './SubagentChips.jsx'
import { agentHelperEntries } from './toolTasks.js'
import { useThinkingTrace } from './useThinkingTrace.js'
import { useDisclosureState } from './disclosureState.js'
import { mergePositionedActivityEntries } from './activityPosition.js'
import { restartCardActivityEntries } from './streamReducers.js'
import HelperResultCard, { HelperRow } from './HelperResultCard.jsx'
import { isWorkingHelper } from './workingHelper.js'

// The helper a Möbius spawn_agent call started (its label input is the name).
function spawnedHelperName(item) {
  if (item?.type !== 'tool' || effectiveToolName(item) !== 'HelperSpawn') return null
  return typeof item.input === 'string' ? item.input.trim() || null : null
}

// One collapsible activity line standing in for a MULTI-STEP contiguous stretch
// of thinking and tool blocks, so a build turn's pre-prose burst reads as one
// quiet ~32px line instead of alternating rows — the answer keeps the screen.
// A lone thought/tool renders as its own disclosure (see SingleActivity below):
// wrapping one row in an identical parent adds hierarchy without information.
// Collapsed, the borderless dim header carries only live status (a periodic
// shimmer over the label — bare "Thinking", or the muted type glyph +
// progressive activities while tools run). A command exit is diagnostic detail,
// not a verdict on the turn, so it stays inside expansion. Expanded, the line
// renders the chronological timeline: mixed
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
      </div>
    </div>
  )
}

function SingleActivity({ entry, chatId, live, surfaceKey, onInternalNav }) {
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
  if (item.type === 'helper_result') {
    return <HelperResultCard event={item} chatId={chatId} onInternalNav={onInternalNav} />
  }
  return (
    <ToolBlock
      key={assistantBlockKey(item, idx)}
      t={item}
      chatId={chatId}
      compact
      disclosureKey={`${surfaceKey}:tool:${blockKey}`}
      onInternalNav={onInternalNav}
    />
  )
}

export function activityDetailUrl(chatId, detailRef) {
  const messageIndex = encodeURIComponent(detailRef.message_index)
  const start = encodeURIComponent(detailRef.start)
  const end = encodeURIComponent(detailRef.end)
  return `/chats/${encodeURIComponent(chatId)}/activity-detail`
    + `?message_index=${messageIndex}&start=${start}&end=${end}`
}

function GroupedActivityStretch({
  entries,
  chatId,
  live = false,
  surfaceKey,
  detailRef = null,
  detailSegments = null,
  positionedEntries = [],
  summaryToolCount = null,
  suppressLatestRestart = false,
  onInternalNav,
}) {
  const stretchKey = assistantBlockKey(entries[0]?.item, entries[0]?.idx)
  const [userOpen, setUserOpen] = useDisclosureState(
    chatId,
    `${surfaceKey}:activity:${stretchKey}`,
  )
  const headerRef = useRef(null)
  const timelineRef = useRef(null)
  const userOpenRef = useRef(userOpen)
  const visibleOpenRef = useRef(false)
  const timelineId = useId()
  const [detailEntries, setDetailEntries] = useState(null)
  const [detailError, setDetailError] = useState(false)
  const [detailAttempt, setDetailAttempt] = useState(0)
  const [detailRequested, setDetailRequested] = useState(userOpen)
  const compositeSegments = Array.isArray(detailSegments) ? detailSegments : null
  const detailMessageIndex = detailRef?.message_index
  const detailStart = detailRef?.start
  const detailEnd = detailRef?.end
  const detailKey = compositeSegments
    ? compositeSegments.map(segment => {
        const ref = segment.detail_ref
        return ref
          ? `${segment.key}:${ref.message_index}:${ref.start}:${ref.end}`
          : `${segment.key}:inline`
      }).join('|')
    : detailRef
      ? `${detailMessageIndex}:${detailStart}:${detailEnd}`
      : ''
  const needsDetail = compositeSegments
    ? compositeSegments.some(segment => segment.detail_ref)
    : Boolean(detailRef)

  useEffect(() => {
    setDetailEntries(null)
    setDetailError(false)
    setDetailRequested(userOpenRef.current)
  }, [detailKey])

  useEffect(() => {
    if (
      !detailRequested
      || !needsDetail
      || detailEntries
      || detailError
    ) return undefined
    const controller = new AbortController()
    let current = true
    const loadSegment = segment => {
      if (!segment.detail_ref) return Promise.resolve(segment.entries || [])
      return apiFetch(activityDetailUrl(chatId, segment.detail_ref), {
        signal: controller.signal,
      }).then(res => jsonOrThrow(res, 'Activity detail failed'))
        .then(data => {
          const merged = mergePositionedActivityEntries(
            Array.isArray(data.entries) ? data.entries : [],
            segment.positioned_entries || [],
          )
          return merged.map(entry => ({
            ...entry,
            idx: `${segment.key}:${entry.idx}`,
          }))
        })
    }
    const request = compositeSegments
      ? Promise.all(compositeSegments.map(loadSegment)).then(results => results.flat())
      : apiFetch(activityDetailUrl(chatId, {
          message_index: detailMessageIndex,
          start: detailStart,
          end: detailEnd,
        }), {
          signal: controller.signal,
        }).then(res => jsonOrThrow(res, 'Activity detail failed'))
          .then(data => mergePositionedActivityEntries(
            Array.isArray(data.entries) ? data.entries : [],
            positionedEntries,
          ))
    request.then(entries => restartCardActivityEntries(
      entries,
      suppressLatestRestart,
    )).then(entries => {
        if (!current) return
        revealBeforeReady()
        setDetailEntries(entries)
      })
      .catch(error => {
        if (!current || error?.name === 'AbortError') return
        revealBeforeReady()
        setDetailError(true)
      })
    return () => {
      current = false
      controller.abort()
    }
  }, [
    chatId,
    detailAttempt,
    detailEntries,
    detailError,
    detailEnd,
    detailKey,
    detailMessageIndex,
    detailStart,
    detailRequested,
    needsDetail,
    compositeSegments,
    positionedEntries,
    suppressLatestRestart,
  ])

  const lastItem = entries[entries.length - 1]?.item
  const liveThinkingTail = live && lastItem?.type === 'thinking'
  // The line's glyph matches its LEADING label word: the currently-running
  // tool's activity while one runs (toolGroupSummary leads with it), else the
  // first-seen activity (the past-tense sentence leads with that).
  const summaryTools = activitySummaryTools(entries)
  const leadTool = [...summaryTools].reverse().find(tool => tool.status === 'running')
    || summaryTools[0]
  const leadToolIcon = toolActivityIcon(effectiveToolName(leadTool))

  // A delegating turn's Task/Agent tool blocks carry a `.subagent` map of live
  // (streamReducers.applyTaskEvent) or persisted (backend 247) helper metadata.
  // Each helper appears once, in order with the other steps (the timeline
  // below draws it where it was launched), plus a running/done count on the
  // header. Shell tasks are commands, not helpers (toolTasks.js): they neither
  // add a row nor count toward "N running".
  const subagentTools = entries
    .map(e => e?.item)
    .filter(it => it?.type === 'tool' && agentHelperEntries(it).length > 0)
  const subagentHelpers = subagentTools
    .flatMap(it => agentHelperEntries(it).map(([, helper]) => helper))
  // A Subagents-app helper's one row (live, then settled) is counted in the
  // header like the Task/Agent ones and drawn where it was launched.
  const helperRows = entries
    .map(e => e?.item)
    .filter(it => it?.type === 'helper_result')
  const helperByName = new Map(helperRows.map(it => [it.task_key, it]))
  const launchedHere = new Set(
    entries.map(e => spawnedHelperName(e?.item)).filter(name => helperByName.has(name)),
  )
  const workingRows = helperRows.filter(isWorkingHelper).length
  const runningHelpers = subagentHelpers.filter(h => h.status === 'running').length
    + workingRows
  const failedHelpers = subagentHelpers.filter(
    h => h.status === 'failed' || h.status === 'killed' || h.status === 'stopped'
  ).length + helperRows.filter(
    it => !isWorkingHelper(it) && it.status !== 'completed'
  ).length
  const doneHelpers = subagentHelpers.length + helperRows.length
    - runningHelpers - failedHelpers
  // Count successes and failures separately: a failed/killed helper is not
  // "done", and the header must not label a red-dotted row as done.
  const subagentCount = subagentHelpers.length + helperRows.length > 0
    ? [
        runningHelpers > 0 ? `${runningHelpers} running` : null,
        doneHelpers > 0 ? `${doneHelpers} done` : null,
        failedHelpers > 0 ? `${failedHelpers} failed` : null,
      ].filter(Boolean).join(' · ')
    : null
  // The overview depends only on tool identity/status. Command output and exact
  // failures stay with ToolBlock after expansion, so typewriter/output frames do
  // not churn every collapsed activity summary above the live turn.
  const sig = activityMemoSig(entries, { liveThinkingTail })

  const meta = useMemo(() => {
    const tools = activitySummaryTools(entries)
    const state = activityStreamState(tools, { liveThinkingTail })
    return {
      state,
      toolCount: Number.isInteger(summaryToolCount)
        ? summaryToolCount
        : tools.length,
      thinkingOnly: tools.length === 0,
    }
  }, [sig, summaryToolCount]) // eslint-disable-line react-hooks/exhaustive-deps

  const { state, toolCount, thinkingOnly } = meta
  // The one presentation authority for icon and state class: a live
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

  // The user's toggle is the ONLY open/close intent — no force-open (see the
  // header comment). Historical detail may delay the rendered open state until
  // its first complete timeline is ready, so the disclosure never paints a
  // one-line placeholder and then changes height again.
  const detailReady = !needsDetail || detailEntries !== null || detailError
  const open = userOpen && detailReady
  const opening = userOpen && !open
  userOpenRef.current = userOpen
  visibleOpenRef.current = open

  // The step count rides in the accessible name. Command diagnostics do not:
  // screen-reader users get the same calm overview and can inspect the same
  // expanded child rows. The one-second clock is not in an aria-live region,
  // so it is not re-announced every tick.
  const stepNote = toolCount > 0
    ? ` (${toolCount} ${toolCount === 1 ? 'step' : 'steps'})`
    : ''
  const stateNote = displayState === 'running' ? ', in progress' : ''
  const iconKind = thinkingOnly ? 'reasoning' : leadToolIcon
  const timelineEntries = needsDetail
    ? detailEntries
    : compositeSegments
      ? restartCardActivityEntries(
          compositeSegments.flatMap(segment => segment.entries || []),
          suppressLatestRestart,
        )
      : entries

  function revealBeforeReady() {
    if (!userOpenRef.current || visibleOpenRef.current) return
    preserveTogglePosition(headerRef.current, timelineRef.current)
  }

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
        interactive
        open={open}
        preparing={opening}
        onPrepare={() => setDetailRequested(true)}
        ariaLabel={`${text}${stepNote}${stateNote}${opening ? ', loading details' : ''}`}
        controlsId={timelineId}
        // Togglable at any time, running or not: with default-collapse there is
        // no forced-open state for a tap to fight, so the user can peek into a
        // live run and close it again.
        onToggle={() => {
          const nextOpen = !userOpen
          if (nextOpen) {
            setDetailRequested(true)
            if (detailReady) {
              preserveTogglePosition(headerRef.current, timelineRef.current)
            }
          } else if (open) {
            preserveTogglePosition(headerRef.current, timelineRef.current)
          }
          if (!nextOpen) setDetailRequested(false)
          userOpenRef.current = nextOpen
          setUserOpen(nextOpen)
        }}
        // A delegating turn's helper rollup ("2 running · 1 done"); the header
        // owns it so it reads without expanding the line.
        count={subagentCount}
      />
      <div
        ref={timelineRef}
        id={timelineId}
        className="chat__activity-timeline"
        hidden={!open}
      >
        {open && detailError && (
          <div className="chat__lazy-status">
            <span className="chat__reasoning-load" role="status" aria-live="polite">
              Activity details unavailable.
            </span>
            <button
              type="button"
              className="chat__lazy-retry"
              onClick={() => {
                preserveTogglePosition(headerRef.current, timelineRef.current)
                setDetailError(false)
                setDetailAttempt(attempt => attempt + 1)
              }}
            >
              Retry
            </button>
          </div>
        )}
        {open && timelineEntries?.map(({ item, idx }) => {
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
          // A helper's one row stands where it was launched: its own spawn
          // call draws it, so its anchored event draws nothing more.
          if (item.type === 'helper_result' && launchedHere.has(item.task_key)) return null
          const launched = helperByName.get(spawnedHelperName(item))
          if (launched) {
            return (
              <HelperRow
                key={assistantBlockKey(item, idx)}
                event={launched}
                chatId={chatId}
                onInternalNav={onInternalNav}
              />
            )
          }
          // A provider's own helper call is its helper rows (each opens that
          // helper's conversation, which the provider recorded separately).
          if (item.type === 'tool' && agentHelperEntries(item).length > 0) {
            return (
              <SubagentChips
                key={assistantBlockKey(item, idx)}
                subagent={item.subagent}
                chatId={chatId}
                onInternalNav={onInternalNav}
              />
            )
          }
          if (item.type === 'helper_result') {
            return (
              <HelperResultCard
                key={item.activityId || item.id || idx}
                event={item}
                chatId={chatId}
                onInternalNav={onInternalNav}
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
              onInternalNav={onInternalNav}
            />
          )
        })}
      </div>
    </div>
  )
}

export default function ActivityStretch({
  entries,
  chatId,
  live = false,
  surfaceKey,
  detailRef = null,
  detailSegments = null,
  positionedEntries = [],
  summaryToolCount = null,
  suppressLatestRestart = false,
  onInternalNav,
}) {
  const loneItem = entries[0]?.item
  const loneHasHelpers = loneItem?.type === 'tool' && agentHelperEntries(loneItem).length > 0
  // A lone ordinary activity needs no redundant parent. A lone delegation does:
  // its broad background-work rollup is context for the named helper rows.
  if (entries.length === 1 && !detailRef && !detailSegments && !loneHasHelpers) {
    return (
      <SingleActivity
        entry={entries[0]}
        chatId={chatId}
        live={live}
        surfaceKey={surfaceKey}
        onInternalNav={onInternalNav}
      />
    )
  }
  return (
    <GroupedActivityStretch
      entries={entries}
      chatId={chatId}
      live={live}
      surfaceKey={surfaceKey}
      detailRef={detailRef}
      detailSegments={detailSegments}
      positionedEntries={positionedEntries}
      summaryToolCount={summaryToolCount}
      suppressLatestRestart={suppressLatestRestart}
      onInternalNav={onInternalNav}
    />
  )
}
