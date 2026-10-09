/* Present hidden same-run interruptions as one reply without rewriting source rows. */
import { isActivityBlock, storedBlockRange, withStoredBlockIndex } from './peerTimeline.js'
import { waitWokeItsAnswer } from './waitHistory.js'
import { assistantAnchorKey, messageKey } from '../../lib/chatDetailCache.js'
import { assistantReplyRoot, isHiddenReplyCarrier, projectSteerContinuationMessage } from './steerContinuity.js'

/** Groups retain every source row/index, including the invisible delivery carriers.
 * Only a committed hidden steer can connect two explicit same-run identities. */
const EMPTY_NOTES = Object.freeze([])

export function assistantReplyGroups(messages, { offset = 0, slots = new Map(), activeIndex = -1, activeKey, displayKeys = new Map(), previousGroups = null } = {}) {
  const groups = new Map()
  const previousByKey = new Map()
  if (previousGroups) {
    for (const old of previousGroups.values()) previousByKey.set(old.rows[0].key, old)
  }
  for (let start = 0; start < messages.length; start += 1) {
    const first = messages[start]
    if (first?.role !== 'assistant' || first.hidden) continue
    const rows = [{ message: first, index: start, notes: EMPTY_NOTES }]
    const root = assistantReplyRoot(first)
    let end = start
    while (root) {
      let next = end + 1
      let sawCarrier = false
      const notes = []
      while (next < messages.length) {
        const carrier = messages[next]
        if (!isHiddenReplyCarrier(carrier, root)) break
        sawCarrier = true
        notes.push(...(slots.get(next) || []))
        next += 1
      }
      const candidate = messages[next]
      if (!sawCarrier || assistantReplyRoot(candidate) !== root) break
      notes.push(...(slots.get(next) || []))
      rows.push({ message: candidate, index: next, notes: notes.length ? notes : EMPTY_NOTES })
      end = next
    }
    const lastVisibleIndex = rows.findLast(row => !row.message.hidden)?.index ?? -1
    const keyedRows = rows.map(row => ({
      ...row,
      key: row.index === activeIndex && activeKey ? activeKey
        : displayKeys.get(row.message.id) || messageKey(row.message, offset + row.index),
      anchorKey: assistantAnchorKey(offset + row.index),
    }))
    // Physical indices shift on prepend, but the persisted message and its
    // absolute keys do not. Keep the expensive reply projection's inputs by
    // identity while still publishing current indices for active-row lookup.
    const priorRows = previousByKey.get(keyedRows[0].key)?.presentationRows
    const samePresentation = priorRows?.length === keyedRows.length
      && keyedRows.every((row, index) => {
        const prior = priorRows[index]
        return prior.message === row.message && prior.key === row.key
          && prior.anchorKey === row.anchorKey && prior.notes === row.notes
      })
    const presentationRows = samePresentation ? priorRows : keyedRows.map(
      ({ message, key, anchorKey, notes }) => ({ message, key, anchorKey, notes }),
    )
    const group = { start, end, lastVisibleIndex, rows: keyedRows, presentationRows }
    for (let index = start; index <= end; index += 1) groups.set(index, group)
    start = end
  }
  return groups
}

/** The selected source already paints its question; only parallel saved rows dedup it. */
export function replyQuestionSuppression(questionKeys, activeRowIndex, rowIndex) {
  return rowIndex === activeRowIndex ? null : questionKeys
}

const sourceBlocks = message => Array.isArray(message.blocks) && message.blocks.length
  ? message.blocks : message.content ? [{ type: 'text', content: message.content }] : []

function hasTextPosition(notes, block, index) {
  return notes?.some(note => {
    const position = note.display_position
    return position?.text_offset > 0 || position?.block_index === (block.raw_index ?? index)
  })
}

// Memoized blocks compare media_dimensions by identity, so a join must return
// the same object on every recompute while its inputs are unchanged.
const joinedMediaDimensions = new WeakMap()

/** Media sizes for text joined from two rows; the later row wins for a shared path. */
function joinMediaDimensions(earlier, later) {
  if (!later || later === earlier) return earlier
  if (!earlier) return later
  let byLater = joinedMediaDimensions.get(earlier)
  if (!byLater) {
    byLater = new WeakMap()
    joinedMediaDimensions.set(earlier, byLater)
  }
  let joined = byLater.get(later)
  if (!joined) {
    joined = { ...earlier, ...later }
    byLater.set(later, joined)
  }
  return joined
}

/** Extend only an exact replay across an otherwise empty display seam. Real
 * thoughts/tools/timeline beats retain their position and existing safe cuts.
 * All rows keep their original keys and activity coordinates for restoration. */
export function presentAssistantReply(rows, { activeIndex = -1, positions = new Map() } = {}) {
  const presented = rows.map(row => ({ ...row, message: row.message }))
  let textOwner = null
  for (let index = 1; index < presented.length; index += 1) {
    const previous = rows[index - 1].message
    const current = rows[index].message
    const projected = projectSteerContinuationMessage(previous, current, { active: index === activeIndex })
    presented[index].message = projected
    const replay = projected?.steer_replay
    const before = sourceBlocks(previous)
    const after = sourceBlocks(current)
    const terminalIndex = before.length - 1
    const terminal = before[terminalIndex]
    const canJoin = replay && replay.text.startsWith(replay.prefix)
      && replay.textIndex === 0 && terminal?.type === 'text'
      && !previous.goal_summaries?.length && !previous.wait_summaries?.length
      && !current.continuation_reason && !current.wait_summaries?.length
      && !rows[index].notes.length
      && !hasTextPosition(positions.get(previous.id), terminal, terminalIndex)
      && !hasTextPosition(positions.get(current.id), after[0], 0)
    if (canJoin) {
      const owner = textOwner && terminalIndex === 0
        ? textOwner : { row: index - 1, block: terminalIndex }
      const ownerMessage = presented[owner.row].message
      const ownerBlocks = [...sourceBlocks(ownerMessage)]
      ownerBlocks[owner.block] = {
        ...ownerBlocks[owner.block], content: replay.text,
        reply_text_owner: true,
        reply_live_text: index === activeIndex && after.length === 1,
      }
      // The owner now shows the later row's text, so it needs that row's
      // image sizes too.
      presented[owner.row].message = {
        ...ownerMessage, blocks: ownerBlocks,
        media_dimensions: joinMediaDimensions(ownerMessage.media_dimensions, current.media_dimensions),
      }
      const nextBlocks = [...sourceBlocks(projected)]
      nextBlocks[0] = { ...nextBlocks[0], content: '', source_text_offset: replay.sourceOffset + replay.text.length }
      presented[index].message = {
        ...projected, blocks: nextBlocks, content: '',
        reply_text_owner_key: presented[owner.row].key,
      }
      textOwner = after.length === 1 ? owner : null
    } else {
      textOwner = null
    }
  }
  return presented
}

// Message-level cards with their own decided treatment (see isAgentWorkBlock):
// a continuation cause or Wait wake opens a fragment, a Goal outcome or ended
// Wait closes one. Activity never joins across them.
const hasLeadingCause = message => message.continuation_reason
  || message.wait_summaries?.some(waitWokeItsAnswer)
const hasTrailingOutcome = message => message.goal_summaries?.length
  || message.wait_summaries?.some(wait => !waitWokeItsAnswer(wait))

/** The rows are already one proven reply, with live/DB sources selected.
 * Move only each leading activity seam; retain every source row and coordinate.
 * The tail owner can precede empty anchors, but never an intervening outcome. */
export function presentAssistantActivity(rows, { activeIndex = -1, positions: sourcePositions = new Map() } = {}) {
  const positions = new Map(sourcePositions)
  const presented = rows.map((row, index) => {
    if (index !== activeIndex || !row.message.blocks?.length) return { ...row }
    const blocks = [...row.message.blocks]
    const last = blocks.findLastIndex(block => !(block.type === 'text' && !block.content?.trim()))
    if (last < 0 || !isActivityBlock(blocks[last])) return { ...row }
    blocks[last] = { ...blocks[last], reply_activity_live: true }
    return { ...row, message: { ...row.message, blocks } }
  })
  let tailOwner = 0
  for (let index = 1; index < presented.length; index += 1) {
    const target = presented[tailOwner].message
    const candidate = presented[index].message
    const targetBlocks = target.blocks || []
    const candidateBlocks = candidate.blocks || []
    if (rows[index].notes.length || hasTrailingOutcome(rows[index - 1].message)
        || hasLeadingCause(candidate) || !isActivityBlock(targetBlocks.at(-1))
        || !isActivityBlock(candidateBlocks[0])) {
      tailOwner = index
      continue
    }
    let end = 0
    while (end < candidateBlocks.length && isActivityBlock(candidateBlocks[end])) end += 1
    const stored = candidateBlocks.map(withStoredBlockIndex)
    const leading = stored.slice(0, end)
    presented[tailOwner].message = { ...target, blocks: [...targetBlocks,
      ...leading.filter(block => block.type !== 'text').map(block => ({
        ...block, source_message_id: block.source_message_id ?? candidate.id,
      })),
    ] }
    const boundary = leading.reduce((at, block) => Math.max(at, storedBlockRange(block)?.end ?? 0), 0)
    const notes = positions.get(candidate.id) || []
    const moving = notes.filter(note => {
      const at = note.display_position?.block_index
      return Number.isInteger(at) && (at < boundary || (end === stored.length && at === boundary))
    })
    if (moving.length) {
      positions.set(target.id, [...(positions.get(target.id) || []), ...moving.map(note => ({
        ...note, display_position: { ...note.display_position, assistant_message_id: target.id,
          source_message_id: note.display_position.source_message_id ?? candidate.id },
      }))])
      const staying = notes.filter(note => !moving.includes(note))
      if (staying.length) positions.set(candidate.id, staying)
      else positions.delete(candidate.id)
    }
    const remaining = stored.slice(end)
    presented[index].message = { ...candidate, blocks: remaining,
      content: remaining.length ? candidate.content : '' }
    if (remaining.length) tailOwner = index
  }
  return { rows: presented, positions }
}
