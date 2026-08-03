import {
  toolActivityLabel,
  toolActivityPastLabel,
  toolActivitySingular,
  toolActivityPastSingular,
  effectiveToolName,
} from './toolActivityLabel.js'
export { groupActivityRuns } from './activityGrouping.js'

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
//   - a DISTINCTIVE tool (isDistinctiveActivityTool — today an image view)
//     breaks the run and stands as its OWN single-entry group, so notable beats
//     punctuate the flow on their own line instead of folding into the combined
//     "Edited files, read files, ran commands" summary (owner ref 2026-07-17).
//     Consecutive distinctive tools each get their own line — they do not
//     accumulate.
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
    if (prev?.item?.type === 'thinking' && entry?.item?.type === 'thinking'
        && !prev.item.thinking_id && !entry.item.thinking_id) {
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

// Derive only the state a COLLAPSED activity overview can honestly communicate:
// in progress while a child runs, settled otherwise. A shell exit is a
// low-level diagnostic, not a reliable verdict on the turn — agents commonly
// recover from optional probes, guarded test invocations, or stale patch
// attempts. Exact failures remain on the individual ToolBlock after the owner
// expands the activity. Keeping them out of this overview also avoids parsing
// every command output merely to paint a misleading alarm.
export function toolGroupState(tools) {
  if (tools.some(t => t?.status === 'running')) return 'running'
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

// Collapsed status of a whole activity stretch. A LIVE thinking tail forces
// 'running'; otherwise the overview is running while a tool runs and settled
// when the stretch is done. Command-level diagnostics belong inside expansion.
export function activityStreamState(tools, { liveThinkingTail = false } = {}) {
  if (liveThinkingTail) return 'running'
  return toolGroupState(tools)
}

// The SINGLE presentation authority for a collapsed line's settled/running
// face. A live trailing stretch stays in progress through the gap between tool
// events, so tense and shimmer never contradict each other.
export function activityDisplayState(state, { live = false } = {}) {
  return live || state === 'running' ? 'running' : 'done'
}

// The memo signature ActivityStretch keys its overview derivations on. The
// overview depends on tool identity/status, never command output; ToolBlock owns
// output and failure rendering after expansion. Thinking content likewise stays
// out so typewriter frames do not rebuild every settled overview above them.
export function activityMemoSig(entries, { liveThinkingTail = false } = {}) {
  return entries
    .map(e => {
      const it = e?.item
      if (it?.type === 'tool') {
        return `t:${it.tool || ''}:${it.status || ''}`
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
// without a memo.
export function activityCollapsedLabel(entries, { live = false } = {}) {
  const tools = entries
    .filter(e => e?.item?.type === 'tool')
    .map(e => e.item)
  const lastItem = entries[entries.length - 1]?.item
  const liveThinkingTail = live && lastItem?.type === 'thinking'
  const toolRunning = tools.some(t => t?.status === 'running')

  if (tools.length === 0) {
    // Thinking-only stretches keep the live wording stable and retain their
    // measured duration after settle. This explicit branch is also the label
    // contract for their reasoning-glyph activity chrome.
    if (liveThinkingTail) return 'Thinking'

    // Sum only finite durations. With no measurement, `undefined` deliberately
    // produces a bare "Thought" instead of inventing a one-second duration.
    const durations = entries
      .filter(e => e?.item?.type === 'thinking')
      .map(e => e.item?.duration_ms)
      .filter(Number.isFinite)
    const durationMs = durations.length
      ? durations.reduce((sum, ms) => sum + ms, 0)
      : undefined
    return thoughtDurationLabel(durationMs)
  }

  if (liveThinkingTail && !toolRunning) {
    // A reasoning tail becomes the visible live status between tools; the
    // measured duration remains available on its expanded timeline row.
    return 'Thinking'
  }

  // A real running status keeps progressive copy even outside the trailing
  // live stretch; otherwise the row could read past-tense while a tool is
  // visibly running. Only fully settled tools flip to the past sentence.
  return live || toolRunning ? toolGroupSummary(tools) : toolGroupPastSummary(tools)
}
