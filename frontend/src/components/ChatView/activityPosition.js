/* Place recorded peer activity within an assistant response without renumbering its blocks. */
import { marked } from 'marked'
import { peerRecordTool, peerTime } from './peerTimeline.js'
import { suppressedQuestionToolIndices } from './streamReducers.js'
import { isDistinctiveActivityTool } from './toolActivityLabel.js'

// Keep markdown constructs whole. The captured prefix never grows, so its
// preceding complete block boundary is stable even while the paragraph or code
// fence containing the arrival continues streaming.
export function activityTextBoundary(content, offset) {
  const prefix = content.slice(0, offset)
  const tokens = marked.lexer(prefix)
  if (!tokens.length) return 0
  if (tokens.at(-1).type === 'space') return prefix.length
  return tokens.slice(0, -1).reduce((length, token) => length + token.raw.length, 0)
}

/** Merge durable peer events back into a compact activity range by their raw
 * transcript boundary. Compact summaries sample repeated tools, while lazy
 * detail returns every raw entry; using the same coordinate keeps both views
 * chronological without turning peer notes into standalone rows. */
export function mergePositionedActivityEntries(entries, positionedEntries = []) {
  if (!positionedEntries.length) return entries
  return [...entries, ...positionedEntries].sort((a, b) => {
    const aPosition = Number.isInteger(a.positionIndex)
      ? a.positionIndex : (Number.isInteger(a.idx) ? a.idx : Number.MAX_SAFE_INTEGER)
    const bPosition = Number.isInteger(b.positionIndex)
      ? b.positionIndex : (Number.isInteger(b.idx) ? b.idx : Number.MAX_SAFE_INTEGER)
    if (aPosition !== bPosition) return aPosition - bPosition
    // A peer boundary at N was recorded immediately before raw block N.
    const aBoundary = Number.isInteger(a.positionIndex) ? 0 : 1
    const bBoundary = Number.isInteger(b.positionIndex) ? 0 : 1
    return aBoundary - bBoundary || String(a.idx).localeCompare(String(b.idx))
  })
}

const isCompactActivityEntry = entry => (
  entry?.item?.type === 'activity' && Array.isArray(entry.item.entries)
)

const isPeerActivityEntry = entry => (
  entry?.item?.type === 'tool'
  && entry.item.tool === 'PeerMessage'
  && typeof entry.item.tool_use_id === 'string'
  && entry.item.tool_use_id.startsWith('peer-')
)

/** Absorb an adjacent run of projected peer messages into an already-compact
 * activity. The compact block is the visible summary for that tool/thinking
 * run, so rendering the messages beside it would manufacture a second
 * high-level row for one continuous stretch. */
export function mergeAdjacentPeerActivityEntries(entries = []) {
  const attachments = new Map()
  const consumed = new Set()
  for (let start = 0; start < entries.length;) {
    if (!isPeerActivityEntry(entries[start])) {
      start += 1
      continue
    }
    let end = start + 1
    while (end < entries.length && isPeerActivityEntry(entries[end])) end += 1
    const target = isCompactActivityEntry(entries[end])
      ? { index: end, before: true }
      : isCompactActivityEntry(entries[start - 1])
        ? { index: start - 1, before: false }
        : null
    if (target) {
      attachments.set(target.index, [
        ...(attachments.get(target.index) || []),
        { before: target.before, peers: entries.slice(start, end) },
      ])
      for (let index = start; index < end; index += 1) consumed.add(index)
    }
    start = end
  }
  if (!attachments.size) return entries
  return entries.flatMap((entry, index) => {
    if (consumed.has(index)) return []
    const groups = attachments.get(index)
    if (!groups) return [entry]
    const activity = entry.item
    const additions = groups.flatMap(({ before, peers }) => {
      const base = before
        ? (Number.isInteger(activity.start) ? activity.start : 0) - peers.length
        : (Number.isInteger(activity.end) ? activity.end : activity.entries.length)
      return peers.map((peer, offset) => ({
        ...peer,
        positionIndex: base + offset,
      }))
    })
    return [{
      ...entry,
      item: {
        ...activity,
        positioned_entries: [
          ...(Array.isArray(activity.positioned_entries) ? activity.positioned_entries : []),
          ...additions,
        ],
      },
    }]
  })
}

const isMergeableActivityEntry = entry => {
  const item = entry?.item
  return item?.type === 'activity'
    || item?.type === 'thinking'
    || (item?.type === 'tool' && !isDistinctiveActivityTool(item))
}

const namespacedEntries = (entries, namespace) => entries.map(entry => ({
  ...entry,
  idx: `${namespace}:${entry.idx}`,
}))

/** Compact activity blocks are storage/render optimization boundaries, not
 * conversation boundaries. When several saved assistant fragments meet with
 * no prose or card between them, represent their adjacent compact/raw runs as
 * one disclosure while retaining every lazy-detail range for expansion. */
export function mergeAdjacentCompactActivityEntries(entries = []) {
  // Providers can persist empty text separators at stream-cut boundaries.
  // They are already invisible to groupActivityRuns and must not split the
  // higher-level compact activity either.
  const visibleEntries = entries.filter(entry => !(
    entry?.item?.type === 'text'
    && typeof entry.item.content === 'string'
    && !entry.item.content.trim()
  ))
  const output = []
  for (let start = 0; start < visibleEntries.length;) {
    if (!isMergeableActivityEntry(visibleEntries[start])) {
      output.push(visibleEntries[start])
      start += 1
      continue
    }
    let end = start + 1
    while (end < visibleEntries.length && isMergeableActivityEntry(visibleEntries[end])) end += 1
    const run = visibleEntries.slice(start, end)
    if (run.length < 2 || !run.some(isCompactActivityEntry)) {
      output.push(...run)
      start = end
      continue
    }
    const segments = run.map((entry, index) => {
      const item = entry.item
      const namespace = `segment-${index}`
      if (item.type !== 'activity') {
        return {
          key: namespace,
          entries: namespacedEntries([entry], namespace),
          tool_count: item.type === 'tool' ? 1 : 0,
        }
      }
      const summary = mergePositionedActivityEntries(
        item.entries,
        item.positioned_entries,
      )
      return {
        key: namespace,
        entries: namespacedEntries(summary, namespace),
        detail_ref: Number.isInteger(item.message_index)
          && Number.isInteger(item.start)
          && Number.isInteger(item.end)
          ? {
              message_index: item.message_index,
              start: item.start,
              end: item.end,
            }
          : null,
        positioned_entries: item.positioned_entries || [],
        tool_count: Number.isInteger(item.tool_count)
          ? item.tool_count + (item.positioned_entries || [])
            .filter(positioned => positioned?.item?.type === 'tool').length
          : summary.filter(summaryEntry => summaryEntry?.item?.type === 'tool').length,
        source: item,
      }
    })
    output.push({
      idx: run[0].idx,
      item: {
        type: 'activity',
        activity_id: `combined:${run.map(entry => entry.item.activity_id || entry.idx).join('|')}`,
        entries: segments.flatMap(segment => segment.entries),
        detail_segments: segments,
        activity_sources: run
          .filter(isCompactActivityEntry)
          .map(entry => entry.item),
        tool_count: segments.reduce((count, segment) => count + segment.tool_count, 0),
      },
    })
    start = end
  }
  return output
}

export function insertPositionedActivity(entries, notes, sourceBlocks, chatId) {
  if (!notes?.length) return entries
  const skipped = suppressedQuestionToolIndices(sourceBlocks)
  const boundaries = new Map()
  const activity = note => note.type === 'helper_result'
    ? { idx: `activity-${note.activityId || note.id}`, item: note }
    : { idx: `peer-${note.id}`, item: { ...peerRecordTool(note, chatId), tool_use_id: `peer-${note.id}` } }
  const compactNotes = new Map()
  const remainingNotes = []
  for (const note of notes) {
    const position = note.display_position
    const compactEntry = note.type !== 'helper_result'
      && Number.isInteger(position?.block_index)
      && entries.find(({ item }) => (
        item?.type === 'activity'
        && Number.isInteger(item.start)
        && Number.isInteger(item.end)
        && position.block_index >= item.start
        && position.block_index < item.end
      ))
    if (!compactEntry) {
      remainingNotes.push(note)
      continue
    }
    const positioned = {
      ...activity(note),
      positionIndex: position.block_index,
    }
    const list = compactNotes.get(compactEntry.idx) || []
    list.push(positioned)
    compactNotes.set(compactEntry.idx, list)
  }
  const projectedEntries = compactNotes.size
    ? entries.map(entry => {
        const positioned = compactNotes.get(entry.idx)
        return positioned?.length
          ? { ...entry, item: { ...entry.item, positioned_entries: positioned } }
          : entry
      })
    : entries
  for (const note of remainingNotes) {
    const position = note.display_position
    if (!position || !Number.isInteger(position.block_index)) continue
    const reference = position.block_key && projectedEntries.find(({ item }) => {
      const key = item.type === 'question' ? item.question_id
        : item.type === 'thinking' ? item.thinking_id : item.tool_use_id
      if (key && `${item.type}:${key}` === position.block_key) return true
      return item.type === 'activity' && Array.isArray(item.entries)
        && item.entries.some(({ item: nested }) => {
          const nestedKey = nested?.type === 'question' ? nested.question_id
            : nested?.type === 'thinking' ? nested.thinking_id : nested?.tool_use_id
          return nestedKey && `${nested.type}:${nestedKey}` === position.block_key
        })
    })
    const index = position.block_key
      ? reference
        // A compact activity run changes the anchor's top-level surface, not
        // the boundary it recorded. Its distance still selects the matching
        // later visible block.
        ? reference.idx + position.block_distance
        : projectedEntries.length + 1
      : position.block_index - [...skipped].filter(i => i < position.block_index).length
    const list = boundaries.get(index) || []
    list.push(note)
    boundaries.set(index, list)
  }
  const result = []
  for (const entry of projectedEntries) {
    const notesHere = boundaries.get(entry.idx) || []
    boundaries.delete(entry.idx)
    notesHere.sort((a, b) => (a.display_position.text_offset || 0) - (b.display_position.text_offset || 0) || peerTime(a.created_at) - peerTime(b.created_at) || a.id.localeCompare(b.id))
    if (entry.item.type !== 'text') {
      result.push(...notesHere.map(activity), entry)
      continue
    }
    let offset = 0
    for (const note of notesHere) {
      const capturedOffset = Math.max(0, Math.min(entry.item.content.length, (note.display_position.text_offset || 0) - (entry.item.source_text_offset || 0)))
      const next = Math.max(offset, activityTextBoundary(entry.item.content, capturedOffset))
      if (next > offset) result.push({ ...entry, idx: offset ? `${entry.idx}:after:${offset}` : entry.idx, item: { ...entry.item, content: entry.item.content.slice(offset, next) } })
      result.push(activity(note))
      offset = next
    }
    if (!notesHere.length || offset < entry.item.content.length) result.push({ ...entry, idx: offset ? `${entry.idx}:after:${offset}` : entry.idx, item: { ...entry.item, content: entry.item.content.slice(offset) } })
  }
  // An anchor can arrive before its corresponding stream snapshot. Keep it at
  // the recorded boundary, rather than losing it while the snapshot catches up.
  for (const [, notesHere] of [...boundaries].sort(([a], [b]) => a - b)) result.push(...notesHere.map(activity))
  return result
}
