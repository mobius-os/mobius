import { toolResultFailed } from './toolResultFormat.js'

// Fold runs of 2+ adjacent tool entries into one group node, so a build turn's
// wall of individual tool blocks collapses into a single "Activity" card. This
// runs on BOTH render paths — MsgContent (persisted msg.blocks) and
// StreamingMessage (live streamItems) — so the transcript looks the same before
// and after a streaming turn is promoted. Keep them calling the same function.
//
// Rules:
//   - a run of >=2 adjacent entries whose item.type === 'tool' becomes a group
//   - a LONE tool stays ungrouped (no card chrome for a single call)
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
    if (run.length >= 2) {
      nodes.push({ group: run })
    } else if (run.length === 1) {
      nodes.push({ single: run[0] })
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
// tool, else done. Shared by ToolActivityGroup so the header chip and the
// auto-expand-while-running behavior read from one rule.
//
// "Failed" comes from the result's exit code, NOT a tool status — the stream
// contract only sets 'running' → 'done' (streamReducers.js), so a failed bash
// still ends 'done'. toolResultFailed reads the nonzero exit out of the parsed
// terminal envelope, the same signal ToolBlock shows on the block header. A
// still-running tool has no final output yet, so it can't be "failed" here.
//
// `running` wins over `error`: while ANY child is still live the group must
// stay expanded to show it (ToolActivityGroup keys auto-expand off this), even
// if an earlier child already failed — the failure surfaces once the run
// settles. Checking running first also short-circuits the parse-heavy failure
// scan on every streaming frame while the run is in flight.
export function toolGroupState(tools) {
  if (tools.some(t => t?.status === 'running')) return 'running'
  if (tools.some(t => toolResultFailed(t?.output))) return 'error'
  return 'done'
}

// A compact header summary: distinct tool names in first-seen order, first 3
// shown, the rest folded into "+N". "Read, Edit, Bash +2" for a 5-name mix.
export function toolGroupSummary(tools) {
  const seen = []
  for (const t of tools) {
    const name = t?.tool || 'Tool'
    if (!seen.includes(name)) seen.push(name)
  }
  const head = seen.slice(0, 3).join(', ')
  const extra = seen.length - 3
  return extra > 0 ? `${head} +${extra}` : head
}
