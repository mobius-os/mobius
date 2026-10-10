// Partition a fetched history page without splitting a displayed message group.
import { assistantReplyGroups } from './assistantReplies.js'
import { ownerMessageBatch } from './chatRuntimeState.js'

// Build newest-first contiguous prefixes. A cut through a displayed reply or
// provider owner batch would temporarily change its identity/presentation.
export function olderHistoryBatches(older, visible, offset, targetSize = 5) {
  if (!older.length) return []
  const combined = [...older, ...visible]
  const groups = assistantReplyGroups(combined, { offset })
  const safeCut = index => {
    const left = groups.get(index - 1)
    if (left && left === groups.get(index)) return false
    const batch = ownerMessageBatch(combined, index - 1)
      || ownerMessageBatch(combined, index)
    return !batch || batch.end < index || batch.start >= index
  }
  const batches = []
  let end = older.length
  while (end > 0) {
    let start = Math.max(0, end - targetSize)
    if (start > 0 && !safeCut(start)) {
      // Prefer a slightly smaller batch. An indivisible group may exceed the
      // target, in which case paint it whole rather than corrupt its display.
      let later = start + 1
      while (later < end && !safeCut(later)) later += 1
      if (later < end) start = later
      else {
        let earlier = start - 1
        while (earlier > 0 && !safeCut(earlier)) earlier -= 1
        start = earlier
      }
    }
    batches.push({ rows: older.slice(start, end), offset: offset + start })
    end = start
  }
  return batches
}
