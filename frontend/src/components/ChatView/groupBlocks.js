import { toolBlockFailed } from './toolResultFormat.js'
import {
  toolActivityLabel,
  toolActivityPastLabel,
  toolActivitySingular,
  toolActivityPastSingular,
  effectiveToolName,
} from './toolActivityLabel.js'

// Fold runs of adjacent ACTIVITY entries — thinking AND tool blocks — into one
// activity node, including a lone entry. A build turn's pre-prose burst is one
// contiguous stretch of reasoning + tool calls with nothing prose-like between
// the pieces, so it honestly collapses to ONE quiet line (ActivityStretch)
// instead of alternating "> Thought" lines and bordered tool cards. Giving
// single- and multi-entry runs the same collapsed line also lets a lone tool (or
// lone thinking) grow into a multi-entry stretch without swapping visual
// primitives. MsgContent applies this to both DB-shaped history blocks and the
// converted live payload, so source selection cannot reshuffle the active answer.
//
// Rules:
//   - any run of entries whose item.type is 'tool' OR 'thinking' becomes a group
//   - any non-activity entry (text, question, error) breaks the run and passes
//     through, so interleave order is preserved exactly (interleave is sacred —
//     these are the blocks a reader must not lose the position of)
//
// Input: an array of entries, each `{ item, ... }` where `item.type` decides
// grouping. The rest of the entry (e.g. the caller's original index) is opaque
// and carried through untouched, so the caller can still key/answer correctly.
//
// Output: an array of nodes, each either `{ single: entry }` or
// `{ group: [entry, entry, ...] }`. Pure — no React, no mutation of inputs.
export function groupActivityRuns(entries) {
  const nodes = []
  let run = []

  const flush = () => {
    if (run.length >= 1) {
      nodes.push({ group: run })
    }
    run = []
  }

  for (const entry of entries) {
    const type = entry?.item?.type
    if (type === 'tool' || type === 'thinking') {
      run.push(entry)
    } else {
      flush()
      nodes.push({ single: entry })
    }
  }
  flush()
  return nodes
}

// Merge runs of ADJACENT thinking entries into one, so a persisted transcript
// renders a continuous reasoning pass as a SINGLE "Thought for Ns" disclosure
// instead of many tiny fragments. Fragments only exist in already-saved chats
// from before the backend stopped closing the thinking run on transparent
// bookkeeping events (see events.py _THINKING_INTERRUPTING_TYPES); the live path
// already coalesces in streamReducers.appendThinkingChunk. This is a render-time
// repair with no migration — same spirit as suppressedQuestionToolIndices.
//
// CRITICAL: this runs on the ENTRIES array (each `{ item, idx }`) AFTER idx has
// been assigned (post-suppression position — see MsgContent), and it PRESERVES
// the first merged entry's `idx`. idx feeds the React key of every
// ordinal-keyed entry, so re-deriving it from a shortened array would swap keys
// under already-mounted rows and force delete+insert remounts. Only
// truly-adjacent thinking entries merge; anything between them (a tool/text
// block) breaks the run, so distinct reasoning segments around tool calls stay
// separate. Pure — new entries, inputs untouched.
export function coalesceThinkingEntries(entries) {
  const out = []
  for (const entry of entries) {
    const prev = out[out.length - 1]
    if (prev?.item?.type === 'thinking' && entry?.item?.type === 'thinking') {
      out[out.length - 1] = {
        ...prev,
        item: {
          ...prev.item,
          content: (prev.item.content || '') + (entry.item.content || ''),
          duration_ms: (prev.item.duration_ms || 0) + (entry.item.duration_ms || 0),
        },
      }
    } else {
      out.push(entry)
    }
  }
  return out
}

// Derive the collapsed status of a tool group from its children: a failed tool
// dominates (so a broken step is visible without expanding), then a running
// tool, else done. Shared by ActivityStretch (via activityStreamState), which
// maps this to the header status — label shimmer while 'running', a danger
// triangle + exit chip on 'error', the muted type glyph on 'done' — all
// readable WITHOUT expanding,
// since the stretch stays collapsed by default (see ActivityStretch).
//
// "Failed" comes from the result's exit code, NOT a tool status — the stream
// contract only sets 'running' → 'done' (streamReducers.js), so a failed bash
// still ends 'done'. toolBlockFailed reads the explicit output_exit_code field
// when a reduced block carries one (contract rule 6), else the nonzero exit out
// of the parsed terminal envelope — the same signal ToolBlock shows on the
// block header. A
// still-running tool has no final output yet, so it can't be "failed" here.
//
// `running` wins over `error`: while ANY child is still live the header reads
// as in-progress (the shimmer), even if an earlier child already failed — the
// failure surfaces once the run settles and the state resolves to 'error'.
// Checking running first also short-circuits the parse-heavy failure scan on
// every streaming frame while the run is in flight.
export function toolGroupState(tools) {
  if (tools.some(t => t?.status === 'running')) return 'running'
  if (tools.some(t => toolBlockFailed(t))) return 'error'
  return 'done'
}

// A compact header summary: the run's distinct ACTIVITIES, first 3 shown, the
// rest folded into "+N". Activities are the owner-facing labels from
// toolActivityLabel, deduped on the LABEL so Read+Glob+Read collapses to one
// "Reading files" — the header reads "Reading files · Editing code", never
// "Read, Read, Edit". Raw tool names stay on the expanded children (ToolBlock)
// for inspection.
//
// While the run is LIVE, the currently-running tool's activity leads the
// summary, so the collapsed header reads what is executing NOW rather than the
// run's first tool. This is what the collapsed line's shimmer sweeps over,
// since the line never force-opens mid-run — see
// ActivityStretch. The running tool is normally the tail item; when nothing
// is running (a done/persisted group) the order is plain first-seen.
// Pure — no React, no mutation of the input array.
export function toolGroupSummary(tools) {
  // Search from the tail so "currently running" reads as the most-recent live
  // tool. Seeding `seen` with its label pins it first; the first-seen scan then
  // fills the rest, and the dedupe folds the running label back out if it also
  // appears earlier. Count per label so a lone occurrence reads singular
  // ("Running a command"); a countable activity with 2+ tools stays plural.
  const running = [...tools].reverse().find(t => t?.status === 'running')
  const seen = []
  const counts = new Map()
  const bump = label => counts.set(label, (counts.get(label) || 0) + 1)
  if (running) seen.push(toolActivityLabel(effectiveToolName(running)))
  for (const t of tools) {
    const label = toolActivityLabel(effectiveToolName(t))
    bump(label)
    if (!seen.includes(label)) seen.push(label)
  }
  const head = seen.slice(0, 3)
    .map(label => counts.get(label) === 1 ? toolActivitySingular(label) : label)
    .join(' · ')
  const extra = seen.length - 3
  return extra > 0 ? `${head} +${extra}` : head
}

// The SETTLED twin of toolGroupSummary: past-tense activities in first-seen
// order, joined as one calm sentence fragment — "Read files, ran commands"
// (the Codex idiom) rather than "Running commands · Reading files" frozen
// mid-run. Known phrases lowercase mid-sentence; an unmapped tool keeps its
// raw name and casing (it is an identifier, not prose). Dedupe is on the
// label, same as the live summary. Pure — no React, no mutation.
export function toolGroupPastSummary(tools) {
  const seen = []
  const counts = new Map()
  for (const t of tools) {
    const name = effectiveToolName(t)
    const past = toolActivityPastLabel(name)
    const label = past || name || 'Tool'
    counts.set(label, (counts.get(label) || 0) + 1)
    if (!seen.some(s => s.label === label)) seen.push({ label, known: !!past })
  }
  const shown = seen.slice(0, 3).map(({ label, known }, i) => {
    // A lone occurrence of a countable activity reads singular ("Ran a
    // command"); 2+ stay plural. Only known (mapped) labels have singulars.
    const text = counts.get(label) === 1 ? toolActivityPastSingular(label) : label
    return i > 0 && known ? text.charAt(0).toLowerCase() + text.slice(1) : text
  })
  const head = shown.join(', ')
  const extra = seen.length - 3
  return extra > 0 ? `${head} +${extra}` : head
}

// Round a live/persisted thinking duration (ms) to whole seconds, clamping any
// positive sub-second span to 1s so a real reasoning pass never reads "0s".
function thoughtSeconds(durationMs) {
  if (!Number.isFinite(durationMs)) return null
  return Math.max(1, Math.round(durationMs / 1000))
}

// Spell out the seconds ("12 seconds", "1 second") — the calm reasoning voice
// today's "> Thought for Ns" disclosure used, reused here verbatim so a
// thinking-only stretch reads byte-for-byte as it did before the unification.
function formatSeconds(seconds) {
  if (!Number.isFinite(seconds)) return null
  return `${seconds} ${seconds === 1 ? 'second' : 'seconds'}`
}

// The dim per-block "Thought for Ns" label on an expanded thinking row (and the
// whole collapsed line for a thinking-only stretch). Drops the old "> " prefix —
// the timeline rail now supplies the reasoning framing.
export function thoughtDurationLabel(durationMs) {
  const secondsText = formatSeconds(thoughtSeconds(durationMs))
  return secondsText ? `Thought for ${secondsText}` : 'Thought'
}

// Collapsed status of a whole activity stretch: reuses toolGroupState (running >
// error > done, failure read from the exit code) but a LIVE thinking tail forces
// 'running' — while the agent is actively reasoning the line reads in-progress
// (the shimmering bare "Thinking"), and an earlier nonzero exit stays quiet until the run
// settles (running-wins). Empty tools + no live tail settles 'done'.
export function activityStreamState(tools, { liveThinkingTail = false } = {}) {
  if (liveThinkingTail) return 'running'
  return toolGroupState(tools)
}

// The SINGLE presentation authority for the collapsed line's icon, danger
// chip, and state class, applied on top of an already-derived stream state
// (so the caller's memo keeps owning the parse-heavy failure scan). A live
// trailing stretch is in-progress for its WHOLE life — including the gap
// between one tool ending and the next event, where no tool is 'running' —
// so tense (activityCollapsedLabel's live||running branch) and the shimmer
// stay in agreement instead of a settled face or the
// failure triangle flashing in mid-turn beside present-tense copy (review
// round 2, both reviewers). Failure surfaces when the stretch actually
// settles (live=false), consistent with running-wins.
export function activityDisplayState(state, { live = false } = {}) {
  if (live && state !== 'running') return 'running'
  return state
}

// The memo signature ActivityStretch keys its parse-heavy derivations on.
// Pure and exported so the staleness contract is unit-testable. Per tool:
// name + status + output length + the output's HEAD and TAIL slices + the
// explicit exit-code field. The two slices cover both places a failure marker
// can live in replace-semantics output — Claude's plain-text "Exit code N"
// head is START-anchored and a JSON envelope may serialize exit_code first or
// last — so an equal-length replacement that flips the exit code cannot leave
// a stale success line (review round 2). Thinking entries contribute a
// constant: their content length must NOT bust the memo on typewriter frames.
export function activityMemoSig(entries, { liveThinkingTail = false } = {}) {
  return entries
    .map(e => {
      const it = e?.item
      if (it?.type === 'tool') {
        return `t:${it.tool || ''}:${it.status || ''}:${it.output?.length || 0}`
          + `:${it.output?.slice(0, 16) || ''}:${it.output?.slice(-14) || ''}`
          + `:${it.output_exit_code ?? ''}`
      }
      return 'k'
    })
    .join('|') + `|${entries.length}|${liveThinkingTail ? 'T' : ''}`
}

// The single localization surface for the collapsed line's primary text. One
// rule for the whole stretch, computed from its entries + the live hint:
//   - live thinking tail (no tool running) → a bare "Thinking" (shimmer is the
//     motion; no clock, no dots)
//   - a live stretch or any running tool → the progressive, running-first
//     activity rollup (toolGroupSummary)
//   - settled tools → the first-seen past-tense rollup (toolGroupPastSummary)
//     Activities say WHAT happened, never "N tool calls" (implementation
//     vocabulary the product avoids); the step count lives in the header's
//     aria-label instead
//   - thinking-only → "Thought for Ns" (the reasoning duration IS the content)
// Cheap on every call (Map lookups + a duration sum), so it runs each render
// without a memo; the parse-heavy failure state lives in activityStreamState.
export function activityCollapsedLabel(entries, { live = false } = {}) {
  const tools = entries
    .filter(e => e?.item?.type === 'tool')
    .map(e => e.item)
  const lastItem = entries[entries.length - 1]?.item
  const liveThinkingTail = live && lastItem?.type === 'thinking'
  const toolRunning = tools.some(t => t?.status === 'running')

  if (liveThinkingTail && !toolRunning) {
    // Bare "Thinking" — no ticking clock, no ellipsis dots (owner ask,
    // 2026-07-17, matching the Codex-app idiom): the label shimmer is the
    // only movement. The measured duration still surfaces at settle as
    // "Thought for Ns".
    return 'Thinking'
  }

  if (tools.length > 0) {
    // A real running status keeps progressive copy even outside the trailing
    // live stretch; otherwise the row could read past-tense while a tool is
    // visibly running. Only fully settled tools flip to the past sentence.
    return live || toolRunning ? toolGroupSummary(tools) : toolGroupPastSummary(tools)
  }

  // Sum only the FINITE thinking durations; if none carry one (a thinking block
  // promoted without a measured span), pass `undefined` so the label reads a bare
  // "Thought" — matching the old "> Thought" disclosure exactly, rather than the
  // sub-second clamp turning a missing duration into "1 second".
  const durations = entries
    .filter(e => e?.item?.type === 'thinking')
    .map(e => e.item?.duration_ms)
    .filter(Number.isFinite)
  const durationMs = durations.length
    ? durations.reduce((sum, ms) => sum + ms, 0)
    : undefined
  return thoughtDurationLabel(durationMs)
}
