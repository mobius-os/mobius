/* Place recorded peer activity within an assistant response without renumbering its blocks. */
import { marked } from 'marked'
import { peerRecordTool, peerTime } from './peerTimeline.js'
import { suppressedQuestionToolIndices } from './streamReducers.js'

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

export function insertPositionedActivity(entries, notes, sourceBlocks, chatId) {
  if (!notes?.length) return entries
  const skipped = suppressedQuestionToolIndices(sourceBlocks)
  const boundaries = new Map()
  for (const note of notes) {
    const position = note.display_position
    if (!position || !Number.isInteger(position.block_index)) continue
    const reference = position.block_key && entries.find(({ item }) => {
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
        : entries.length + 1
      : position.block_index - [...skipped].filter(i => i < position.block_index).length
    const list = boundaries.get(index) || []
    list.push(note)
    boundaries.set(index, list)
  }
  const result = []
  const activity = note => note.type === 'helper_result'
    ? { idx: `activity-${note.activityId || note.id}`, item: note }
    : { idx: `peer-${note.id}`, item: { ...peerRecordTool(note, chatId), tool_use_id: `peer-${note.id}` } }
  for (const entry of entries) {
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
