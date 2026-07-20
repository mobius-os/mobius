/**
 * Pure reducer helpers for useStreamConnection's question/tool SSE
 * dispatch. Extracted so the merge policy is unit-testable without
 * React (node --test imports this module directly).
 *
 * Why a question event must REPLACE a tool item: one AskUserQuestion
 * call arrives on the wire as TWO event families for the SAME tool
 * use. The runner translates the assistant tool_use block into
 * `tool_start` + `tool_input` BEFORE the SDK's can_use_tool callback
 * fires the `question` event, and after the user answers it emits
 * `tool_output` (the "Your questions have been answered: ..." echo)
 * + `tool_end` for that same block. Appending the question as a new
 * item therefore rendered the call twice during streaming — a
 * running "AskUserQuestion" tool block AND the question card — with
 * the answered echo popping in as a third state on the tool block.
 * The card is the canonical rendering; the tool item is its raw
 * twin and is absorbed in place (the card takes the tool item's
 * position, so nothing jumps).
 *
 * The persisted-history paths already merge question state by
 * identity (backend events.process_event coalesces by
 * question_block_key; ChatView's bridge promote carries answers by
 * questionKey). upsertQuestionItem applies the same keying to the
 * live stream so a re-delivered question event (catch-up replay,
 * partial re-emit) updates the existing card in place — including
 * keeping optimistically patched answers — instead of re-arming or
 * duplicating it.
 */
import { questionKey } from './questionKey.js'

// Tool names whose tool events describe an AskUserQuestion-style
// call: Claude's AskUserQuestion and Codex's request_user_input.
// Mirrors backend/app/tool_summaries.py's question-tool branch.
const QUESTION_TOOLS = new Set(['AskUserQuestion', 'request_user_input'])

export function isQuestionTool(tool) {
  return QUESTION_TOOLS.has(tool)
}

/**
 * Indices of redundant AskUserQuestion tool blocks in a PERSISTED
 * message's `blocks` array — the raw tool twin of a question card that
 * should not render alongside the card.
 *
 * The live stream absorbs the tool twin: upsertQuestionItem REPLACES the
 * running AskUserQuestion tool item with the question card in place. The
 * backend persistence path (events.process_event) does NOT — `tool_start`
 * appends a `tool` block and the later `question` event appends a separate
 * `question` block, so a reopened chat shows a collapsed "AskUserQuestion"
 * tool row above the answered card. This computes which tool blocks to skip
 * at render time so the persisted view matches the live view, with no
 * backend migration (it fixes already-persisted old chats too).
 *
 * A question-tool block is suppressed only when the message also contains
 * a question block — the card is the canonical rendering of the same call,
 * so the tool twin is pure noise. Non-question tools (Bash, Grep) and
 * question-tool blocks in a message with no card (a defensive edge that
 * shouldn't occur) are left untouched.
 *
 * @param {Array<object>} blocks  a persisted message's blocks
 * @returns {Set<number>} indices into `blocks` to skip when rendering
 */
export function suppressedQuestionToolIndices(blocks) {
  const suppressed = new Set()
  if (!Array.isArray(blocks)) return suppressed
  const hasQuestionCard = blocks.some(b => b?.type === 'question')
  if (!hasQuestionCard) return suppressed
  blocks.forEach((b, i) => {
    if (b?.type === 'tool' && isQuestionTool(b.tool)) suppressed.add(i)
  })
  return suppressed
}

/**
 * Applies a `question` event to the stream items.
 *
 * Merge policy, in priority order:
 *  1. A question item with the same questionKey exists → update it
 *     in place. The incoming event's `questions` win (partial
 *     deliveries grow the list), but answers already on the item
 *     (patchQuestionAnswers' optimistic update) are kept when the
 *     incoming event carries none — question events never carry
 *     answers, and wiping them flipped an answered card back to
 *     pending. Mirrors the answer-carry in ChatView's bridge
 *     promote and chat_writer's writeback merge.
 *  2. A RUNNING question-tool item exists → the card replaces it at
 *     that index (`absorbedTool` remembers the open tool lifecycle
 *     so the post-answer tool_output/tool_end resolve to this item
 *     instead of corrupting an unrelated tool block).
 *  3. Otherwise append (Codex publishes no tool_start for
 *     request_user_input today, so its cards take this path).
 *
 * @param {Array<object>} prev  current stream items
 * @param {object} incoming  `{type:'question', questions, question_id?}`
 * @returns {Array<object>} next stream items (new array when changed)
 */
export function upsertQuestionItem(prev, incoming) {
  const key = questionKey(incoming)
  const idx = prev.findIndex(
    it => it.type === 'question' && questionKey(it) === key
  )
  if (idx !== -1) {
    const existing = prev[idx]
    const merged = { ...incoming }
    if (existing.answers && !merged.answers) merged.answers = existing.answers
    if (existing.absorbedTool) merged.absorbedTool = existing.absorbedTool
    const updated = [...prev]
    updated[idx] = merged
    return updated
  }
  for (let i = prev.length - 1; i >= 0; i--) {
    const it = prev[i]
    if (it.type === 'tool' && it.status === 'running' && isQuestionTool(it.tool)) {
      const updated = [...prev]
      updated[i] = { ...incoming, absorbedTool: it.tool }
      return updated
    }
  }
  return [...prev, incoming]
}

/**
 * Index of the OPEN tool lifecycle: the last running tool item, or a
 * question item that absorbed its tool block (whose tool_output/
 * tool_end are still inbound). The runner guarantees tool events for
 * one tool are never interleaved with another tool's, so the latest
 * open lifecycle is always the right target.
 */
function openToolLifecycleIndex(items) {
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i]
    if (it.type === 'tool' && it.status === 'running') return i
    if (it.type === 'question' && it.absorbedTool) return i
  }
  return -1
}

/**
 * Applies a `tool_output` event. Replace-semantics on purpose:
 * Codex publishes per-delta outputs that each carry the full text so
 * far, and the completed event carries the final aggregate — both
 * backend (events.process_event) and this reducer overwrite rather
 * than append. Output aimed at an absorbed question is swallowed:
 * the answered card already shows the same content as the
 * "Your questions have been answered" echo.
 */
export function attachToolOutput(prev, content, event = null) {
  const i = openToolLifecycleIndex(prev)
  if (i === -1 || prev[i].type === 'question') return prev
  const updated = [...prev]
  const block = { ...updated[i], output: content }
  // The backend reduces large output before it reaches either SSE or
  // persistence. Preserve the reduction metadata on the live item too, so an
  // expanded tool can fetch its stashed full text immediately instead of
  // treating the excerpt as complete until the turn settles and reloads.
  if (event?.tool_use_id && !block.tool_use_id) {
    block.tool_use_id = event.tool_use_id
  }
  if (event?.output_truncated) {
    block.output_truncated = true
    block.output_full_len = event.output_full_len
    if (event.output_exit_code != null) {
      block.output_exit_code = event.output_exit_code
    }
  }
  updated[i] = block
  return updated
}

/**
 * Applies a `tool_sources` event to the search block that produced it.
 * Sources are small metadata, so they stay inline on the tool item.
 *
 * Matching is by `tool_use_id`: a turn can run several WebSearch calls in ONE
 * batch, and their results then arrive back to back while the LAST search item
 * is the trailing one — so matching by position alone lands every batch
 * member on that one item, each overwriting the previous, and only the final
 * search's sources survive. Events with no id (a provider that does not stamp
 * one) keep the historical last-WebSearch fallback rather than dropping.
 */
export function attachToolSources(prev, sources, toolUseId) {
  if (!Array.isArray(sources) || sources.length === 0) return prev
  let idx = -1
  if (toolUseId) {
    idx = prev.findLastIndex(
      it => it.type === 'tool'
        && it.tool === 'WebSearch'
        && it.tool_use_id === toolUseId,
    )
    // An explicit id is authoritative: never misattribute its sources to a
    // different search merely because that search is the trailing one.
    if (idx < 0) return prev
  } else {
    idx = prev.findLastIndex(it => it.type === 'tool' && it.tool === 'WebSearch')
  }
  if (idx < 0) return prev
  const updated = [...prev]
  // Merge rather than replace: keeps a block correct when one search reports
  // across several events, and stays idempotent under the catch-up burst,
  // which replays every event from the start of the turn.
  const existing = updated[idx].sources || []
  const seen = new Set(existing.map(s => s?.url).filter(Boolean))
  const merged = [...existing]
  for (const source of sources) {
    const url = source?.url
    if (!url || seen.has(url)) continue
    seen.add(url)
    merged.push(source)
  }
  updated[idx] = { ...updated[idx], sources: merged }
  return updated
}

/**
 * Applies a `thinking` event (the agent's extended reasoning). Like
 * text, consecutive thinking deltas coalesce into a single trailing
 * `{type:'thinking'}` item; a thinking that arrives after any other
 * item (text, a tool block) opens a fresh thinking item so the
 * reasoning stays in emit-order. Empty content is a no-op. The caller
 * is responsible for flushing any pending typewriter text first.
 */
export function appendThinkingChunk(
  prev,
  chunk,
  at = Date.now(),
  ts = null,
  segmentId = null,
) {
  if (!chunk) return prev
  const last = prev[prev.length - 1]
  const eventTs = Number.isFinite(ts) ? ts : null
  if (last && last.type === 'thinking') {
    const startedAt = Number.isFinite(last.startedAt) ? last.startedAt : at
    const firstTs = Number.isFinite(last.firstTs) ? last.firstTs : eventTs
    const updated = [...prev]
    const segmentChanged = segmentId != null
      && last.segmentId != null
      && segmentId !== last.segmentId
    updated[updated.length - 1] = {
      ...last,
      startedAt,
      firstTs,
      // Client wall-clock of THIS (latest) delta. Paired with duration_ms (the
      // runner-measured span up to here), this lets the live ticker survive a
      // reconnect/catch-up replay — see thinkingElapsedMs.
      lastAt: at,
      // Token deltas inside one provider segment concatenate verbatim. A new
      // summary/content index is a semantic paragraph, not another token.
      // Legacy events have no identity and keep the old raw-concat behavior.
      content: last.content + (segmentChanged ? '\n\n' : '') + chunk,
      ...(segmentId != null ? { segmentId } : {}),
      duration_ms: Number.isFinite(firstTs) && Number.isFinite(eventTs)
        ? Math.max(0, eventTs - firstTs)
        : Math.max(0, at - startedAt),
    }
    return updated
  }
  return [...prev, {
    type: 'thinking',
    content: chunk,
    startedAt: at,
    firstTs: eventTs,
    lastAt: at,
    duration_ms: 0,
    ...(segmentId != null ? { segmentId } : {}),
  }]
}

/** Render-time repair for already-persisted reasoning from clients that lost
 * provider segment identity. Adjacent bold summary headings were stored as
 * `****`; restore only that unambiguous Markdown seam. New events carry
 * segment_id and are separated before persistence, so this is legacy-only. */
export function thinkingContentForDisplay(content) {
  return String(content || '').replaceAll('****', '**\n\n**')
}

/**
 * Wall-clock elapsed (ms) to show on a LIVE thinking block, anchored to the
 * runner's clock so it survives a reconnect/catch-up replay.
 *
 * The naive `now - startedAt` breaks on reconnect: the server replays the whole
 * in-flight turn as a burst, so every thinking delta is re-processed "now" and
 * `startedAt` collapses to the reconnect instant — the timer restarts at 1s no
 * matter how long the runner has actually been thinking.
 *
 * Instead: `duration_ms` is the runner-measured span up to the most recent
 * delta (eventTs − firstTs, from timestamps the replayed events still carry),
 * and `lastAt` is the client time that delta was received. Elapsed is that span
 * plus the wall-clock tail since — replay-invariant, because a burst leaves
 * duration_ms at the true full span and lastAt at the reconnect moment.
 *
 * Falls back to `now - startedAt` for legacy items with no `lastAt` (and for a
 * provider that sends no runner timestamps, where duration_ms is itself
 * client-measured and paired with lastAt=at, the two forms coincide).
 */
export function thinkingElapsedMs(item, now) {
  if (item && Number.isFinite(item.lastAt)) {
    const base = Number.isFinite(item.duration_ms) ? item.duration_ms : 0
    return base + Math.max(0, now - item.lastAt)
  }
  const startedAt = item && Number.isFinite(item.startedAt) ? item.startedAt : now
  return Math.max(0, now - startedAt)
}

/**
 * Re-anchor the trailing live thinking block when a catch-up replay finishes.
 *
 * Replayed deltas arrive in a burst, so their client `lastAt` values describe
 * replay time rather than the time the runner originally emitted them. The
 * catch_up_done marker carries the server clock at the end of that replay;
 * comparing it with the block's server-authored `firstTs` restores the quiet
 * interval since the most recent reasoning delta. Without this step a block
 * containing one summary event restarts at "1 second" whenever the chat
 * remounts, even though the turn has been thinking for minutes.
 */
export function anchorReplayedThinking(items, replayTs, at = Date.now()) {
  if (!Array.isArray(items) || items.length === 0) return items
  if (!Number.isFinite(replayTs) || !Number.isFinite(at)) return items
  const i = items.length - 1
  const item = items[i]
  if (item?.type !== 'thinking' || !Number.isFinite(item.firstTs)) return items

  const replayElapsed = Math.max(0, replayTs - item.firstTs)
  const currentDuration = Number.isFinite(item.duration_ms) ? item.duration_ms : 0
  const duration_ms = Math.max(currentDuration, replayElapsed)
  const updated = [...items]
  updated[i] = { ...item, duration_ms, lastAt: at }
  return updated
}

/**
 * Applies a `tool_end` event. A running tool flips to done; an
 * absorbed question's lifecycle closes by dropping `absorbedTool`
 * (the card itself has no status), so a later tool_output can never
 * resolve to it.
 */
export function closeToolLifecycle(prev) {
  const i = openToolLifecycleIndex(prev)
  if (i === -1) return prev
  const updated = [...prev]
  if (updated[i].type === 'question') {
    const { absorbedTool: _absorbed, ...rest } = updated[i]
    updated[i] = rest
  } else {
    updated[i] = { ...updated[i], status: 'done' }
  }
  return updated
}

/**
 * Terminal sweep for an `error` event: every open lifecycle closes —
 * running tools flip to done, absorbed questions drop their flag (no
 * tool_end is coming after a provider error).
 */
export function closeAllToolLifecycles(prev) {
  return prev.map(b => {
    if (b.type === 'tool' && b.status === 'running') {
      return { ...b, status: 'done' }
    }
    if (b.type === 'question' && b.absorbedTool) {
      const { absorbedTool: _absorbed, ...rest } = b
      return rest
    }
    return b
  })
}

// ---------------------------------------------------------------------------
// Live subagent helpers — task_start / task_progress / task_done ENRICH the
// existing Task/Agent tool block; they are metadata about a tool call that
// already exists and owns its own output/expand/lifecycle. This mirrors the
// skill_loaded enrichment (a `skill` field stamped onto the Skill tool block),
// NOT the question card (which is a second wire shape of one UI object). There
// is no standalone subagent stream item and no tool-twin suppression: the Task
// ToolBlock stays intact and its helper metadata rides catch-up reconcile (tool
// blocks reconcile by tool_use_id) and promotion (streamItemToBlock spreads all
// tool fields) for free. Backend card 247 persists the identical shape, so the
// live, promoted, and reloaded views are the same rendering.
//
// Shape stamped onto the block:
//   tool.subagent = { "<task_id>": {description, task_type, status,
//                                   last_tool_name, usage, summary,
//                                   startedAt, lastAt} }
// status ∈ 'running' | 'done' | 'failed' | 'killed' | 'stopped'.

// The tool blocks a helper attaches to. A Set so lookups never walk the
// prototype chain.
const SUBAGENT_TOOLS = new Set(['Task', 'Agent'])

// A helper is terminal once it reaches any of these; a late or replayed
// task_start / task_progress must never move it back to 'running'.
const TERMINAL_TASK_STATUSES = new Set(['done', 'failed', 'killed', 'stopped'])

// task_done carries status ∈ done/completed/failed/killed/stopped. Normalize the
// SDK's 'completed' to 'done'; every other terminal value renders as-is. An
// unrecognised terminal reads as a plain completion.
function normalizeTaskStatus(status) {
  if (status === 'completed') return 'done'
  if (TERMINAL_TASK_STATUSES.has(status)) return status
  return 'done'
}

function helperShallowEqual(a, b) {
  if (a === b) return true
  if (!a || !b) return false
  const ka = Object.keys(a)
  const kb = Object.keys(b)
  if (ka.length !== kb.length) return false
  for (const k of ka) {
    if (a[k] !== b[k]) return false
  }
  return true
}

// Merge one task_* event into a helper record, returning the SAME reference when
// nothing changed (the idempotent-replay no-op). Enforces the invariants: a
// terminal helper never downgrades to running; description/summary already
// present are never wiped by a later event that carries null.
function mergeSubagentHelper(existing, event, now) {
  const base = existing || {}
  const wasTerminal = !!existing && TERMINAL_TASK_STATUSES.has(existing.status)
  let next
  if (event.type === 'task_start') {
    next = {
      ...base,
      description: event.description || base.description || '',
      task_type: event.task_type || base.task_type || '',
      // A re-delivered start on an already-finished helper keeps the terminal
      // status; a genuine first start opens it as running.
      status: wasTerminal ? existing.status : 'running',
      startedAt: Number.isFinite(base.startedAt) ? base.startedAt : now,
    }
  } else if (event.type === 'task_progress') {
    // A tick only annotates a live helper; one replayed after task_done must not
    // revive the timer or reopen the terminal state.
    if (wasTerminal) return existing
    next = {
      ...base,
      status: base.status || 'running',
      last_tool_name: event.last_tool_name != null
        ? event.last_tool_name : (base.last_tool_name ?? null),
      usage: event.usage != null ? event.usage : (base.usage ?? null),
      startedAt: Number.isFinite(base.startedAt) ? base.startedAt : now,
      lastAt: now,
    }
  } else if (event.type === 'task_done') {
    const nextStatus = normalizeTaskStatus(event.status)
    const nextSummary = event.summary != null ? event.summary : (base.summary ?? null)
    // A redundant terminal done — same status and summary, re-delivered at a
    // fresh wall-clock (both terminal SDK surfaces firing, or a catch-up replay)
    // — must return the SAME reference. Rewriting lastAt to a new now would mint
    // a new helper/tool/items array on every replay and defeat the tool block's
    // identity-preserving reconciliation.
    if (wasTerminal
        && existing.status === nextStatus
        && (existing.summary ?? null) === nextSummary) {
      return existing
    }
    next = {
      ...base,
      status: nextStatus,
      summary: nextSummary,
      startedAt: Number.isFinite(base.startedAt) ? base.startedAt : now,
      lastAt: now,
    }
  } else {
    return existing
  }
  return helperShallowEqual(base, next) ? existing : next
}

/**
 * Applies a task_start / task_progress / task_done event by ENRICHING the
 * matching Task/Agent tool block's `subagent` map. One reducer for all three
 * (mirrors the skill_loaded stamp). Pure and idempotent — replaying a whole
 * lifecycle burst leaves exactly one helper on the block.
 *
 * Target resolution, in order:
 *   1. by task_id — the tool block already carrying this helper. MANDATORY
 *      first: Claude's terminal TaskUpdatedMessage emits tool_use_id:null
 *      (claude_sdk_runner.py), so a task_done may have no tool_use_id and can
 *      only be routed through the helper it already created.
 *   2. by tool_use_id — the parent Task/Agent tool block. This is how a
 *      task_start finds its block, and how a task_done materializes a helper
 *      whose task_start was missed.
 * No host tool block for either key → a no-op (nothing to annotate).
 *
 * @param {Array<object>} items  current stream items
 * @param {object} event  a task_start/task_progress/task_done event
 * @param {number} now  client clock, stamped onto startedAt/lastAt
 * @returns {Array<object>} next items (new array when changed, else `items`)
 */
export function applyTaskEvent(items, event, now = Date.now()) {
  if (!event || event.task_id == null) return items
  const taskId = event.task_id
  const toolUseId = event.tool_use_id ?? null

  let idx = items.findIndex(
    it => it.type === 'tool'
      && it.subagent
      && Object.prototype.hasOwnProperty.call(it.subagent, taskId)
  )
  if (idx === -1 && toolUseId != null) {
    idx = items.findIndex(
      it => it.type === 'tool'
        && SUBAGENT_TOOLS.has(it.tool)
        && it.tool_use_id === toolUseId
    )
  }
  if (idx === -1) return items

  const tool = items[idx]
  const existing = (tool.subagent && tool.subagent[taskId]) || null
  const merged = mergeSubagentHelper(existing, event, now)
  if (merged === existing) return items

  const updated = [...items]
  updated[idx] = {
    ...tool,
    subagent: { ...(tool.subagent || {}), [taskId]: merged },
  }
  return updated
}

/**
 * True when two stream items carry identical own-enumerable fields by ===.
 * Nested values (a tool's `sources` array, a question's `answers`/`questions`)
 * compare by reference, so an item that gained or replaced one of those reads
 * as changed and is merged rather than reused — 2a's key still preserves its
 * DOM node, only the props update.
 */
function shallowEqualItem(a, b) {
  if (a === b) return true
  if (!a || !b) return false
  const ka = Object.keys(a)
  const kb = Object.keys(b)
  if (ka.length !== kb.length) return false
  for (const k of ka) {
    if (a[k] !== b[k]) return false
  }
  return true
}

/**
 * Reconcile a catch-up replay (`next`) onto the on-screen stream (`prev`),
 * merging by key so unchanged items keep their object + DOM identity through
 * the commit instead of remounting the whole answer (contract v2 item 2, lever
 * 2c — "return without redraw").
 *
 * The replay is deterministic and in-order, so position k in `next` is the same
 * logical item as position k in `prev`. Tool items additionally require a
 * matching `tool_use_id` (lever 2a) before reusing identity, so a positional
 * collision between two different tools can never silently merge; text/thinking
 * match positionally (until lever 2b gives them a synthetic id). For each
 * replayed item:
 *   - identity matches an on-screen item AND all fields are equal → reuse the
 *     on-screen object, so React skips even a memoized re-render;
 *   - identity matches but fields differ → merge `{...prev, ...next}` (a new
 *     object under the SAME key, so the DOM node survives and only props
 *     update);
 *   - no identity match → take the replayed object (a genuinely new item).
 *
 * Items on-screen but absent from `next` are dropped — catch-up is
 * authoritative, so a steer-dropped or trimmed pre-reconnect segment is never
 * resurrected. Returns `prev` unchanged (same array ref) when nothing changed,
 * making a no-op reconnect commit a complete React bail-out.
 */
export function reconcileStreamItems(prev, next) {
  if (!Array.isArray(next)) return prev
  if (!Array.isArray(prev) || prev.length === 0) return next
  let changed = next.length !== prev.length
  const result = next.map((incoming, k) => {
    const p = prev[k]
    let n = incoming
    if (!p || p.type !== n.type) {
      changed = true
      return n
    }
    if (n.type === 'tool') {
      const bothTagged = p.tool_use_id != null && n.tool_use_id != null
      const bothLegacy = p.tool_use_id == null && n.tool_use_id == null
      const idMatch = bothTagged ? p.tool_use_id === n.tool_use_id : bothLegacy
      if (!idMatch) {
        changed = true
        return n
      }
    }
    if (n.type === 'thinking' && Number.isFinite(n.lastAt)) {
      // A bounded replay may no longer contain the first delta, while the
      // visible/session snapshot still has the older clock anchor. Never let
      // reconciliation move a live timer backwards in that case.
      const previousElapsed = thinkingElapsedMs(p, n.lastAt)
      const replayElapsed = thinkingElapsedMs(n, n.lastAt)
      if (previousElapsed > replayElapsed) {
        n = {
          ...n,
          duration_ms: previousElapsed,
          ...(Number.isFinite(p.startedAt) ? { startedAt: p.startedAt } : {}),
        }
      }
    }
    if (shallowEqualItem(p, n)) return p
    changed = true
    return { ...p, ...n }
  })
  return changed ? result : prev
}
