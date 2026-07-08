import { toolResultFailed } from './toolResultFormat.js'
import { toolActivityLabel } from './toolActivityLabel.js'

// Fold runs of adjacent tool entries into one activity node, including a lone
// tool. This gives single-tool and multi-tool runs the same collapsed header and
// lets a live single tool become a multi-tool run without swapping visual
// primitives. This runs on BOTH render paths — MsgContent (persisted msg.blocks)
// and StreamingMessage (live streamItems) — so the transcript looks the same
// before and after a streaming turn is promoted. Keep them calling the same
// function.
//
// Rules:
//   - any run of entries whose item.type === 'tool' becomes a group
//   - any non-tool entry (text, question, error) breaks the run and passes
//     through, so interleave order is preserved exactly
//
// Input: an array of entries, each `{ item, ... }` where `item.type` decides
// grouping. The rest of the entry (e.g. the caller's original index) is opaque
// and carried through untouched, so the caller can still key/answer correctly.
//
// Output: an array of nodes, each either `{ single: entry }` or
// `{ group: [entry, entry, ...] }`. Pure — no React, no mutation of inputs.
export function groupToolRuns(entries) {
  const nodes = []
  let run = []

  const flush = () => {
    if (run.length >= 1) {
      nodes.push({ group: run })
    }
    run = []
  }

  for (const entry of entries) {
    if (entry?.item?.type === 'tool') {
      run.push(entry)
    } else {
      flush()
      nodes.push({ single: entry })
    }
  }
  flush()
  return nodes
}

// Derive the collapsed status of a tool group from its children: a failed tool
// dominates (so a broken step is visible without expanding), then a running
// tool, else done. Shared by ToolActivityGroup, which maps this to the header
// chrome — spinner while 'running', a check on 'done', a Failed chip on
// 'error' — all readable WITHOUT expanding the card, since the card stays
// collapsed by default (see ToolActivityGroup).
//
// "Failed" comes from the result's exit code, NOT a tool status — the stream
// contract only sets 'running' → 'done' (streamReducers.js), so a failed bash
// still ends 'done'. toolResultFailed reads the nonzero exit out of the parsed
// terminal envelope, the same signal ToolBlock shows on the block header. A
// still-running tool has no final output yet, so it can't be "failed" here.
//
// `running` wins over `error`: while ANY child is still live the header reads
// as in-progress (the spinner), even if an earlier child already failed — the
// failure surfaces once the run settles and the state resolves to 'error'.
// Checking running first also short-circuits the parse-heavy failure scan on
// every streaming frame while the run is in flight.
export function toolGroupState(tools) {
  if (tools.some(t => t?.status === 'running')) return 'running'
  if (tools.some(t => toolResultFailed(t?.output))) return 'error'
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
// run's first tool. This is the group's liveness signal while collapsed (with
// the header spinner) now that the card no longer force-opens mid-run — see
// ToolActivityGroup. The running tool is normally the tail item; when nothing
// is running (a done/persisted group) the order is plain first-seen.
// Pure — no React, no mutation of the input array.
export function toolGroupSummary(tools) {
  // Search from the tail so "currently running" reads as the most-recent live
  // tool. Seeding `seen` with its label pins it first; the first-seen scan then
  // fills the rest, and the dedupe folds the running label back out if it also
  // appears earlier.
  const running = [...tools].reverse().find(t => t?.status === 'running')
  const seen = []
  if (running) seen.push(toolActivityLabel(running.tool))
  for (const t of tools) {
    const label = toolActivityLabel(t?.tool)
    if (!seen.includes(label)) seen.push(label)
  }
  const head = seen.slice(0, 3).join(' · ')
  const extra = seen.length - 3
  return extra > 0 ? `${head} +${extra}` : head
}
