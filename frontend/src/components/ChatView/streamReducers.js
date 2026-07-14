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
export function attachToolOutput(prev, content) {
  const i = openToolLifecycleIndex(prev)
  if (i === -1 || prev[i].type === 'question') return prev
  const updated = [...prev]
  updated[i] = { ...updated[i], output: content }
  return updated
}

/**
 * Applies a `tool_sources` event to the most recent WebSearch block.
 * Sources are small metadata, so they stay inline on the tool item.
 */
export function attachToolSources(prev, sources) {
  if (!Array.isArray(sources) || sources.length === 0) return prev
  for (let i = prev.length - 1; i >= 0; i--) {
    const it = prev[i]
    if (it.type === 'tool' && it.tool === 'WebSearch') {
      const updated = [...prev]
      updated[i] = { ...it, sources }
      return updated
    }
  }
  return prev
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
  const result = next.map((n, k) => {
    const p = prev[k]
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
    if (shallowEqualItem(p, n)) return p
    changed = true
    return { ...p, ...n }
  })
  return changed ? result : prev
}
