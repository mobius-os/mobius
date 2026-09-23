// Pure view model for the Memory tool card, keeping bounds and safe links
// directly testable without mounting React.

import { noteHref, noteLabel } from './memoryRecall.js'

const MAX_QUERY_CHARS = 600
const MAX_SUMMARY_CHARS = 300
const RECALL_STATUSES = new Set(['searching', 'hit', 'empty', 'failed'])
const RECALL_PHASES = new Set(['catalog', 'read'])

function cleanText(value, limit) {
  if (typeof value !== 'string') return ''
  return value.replace(/\s+/g, ' ').trim().slice(0, limit)
}

export function memoryRecallCardModel(recall) {
  if (!recall || typeof recall !== 'object'
      || !RECALL_STATUSES.has(recall.status)) return null

  const notes = []
  const seen = new Set()
  for (const note of Array.isArray(recall.notes) ? recall.notes : []) {
    const label = noteLabel(note)
    const href = noteHref(note)
    const key = typeof note?.path === 'string' && note.path
      ? note.path
      : note?.id
    if (!label || !key || seen.has(key)) continue
    seen.add(key)
    const rawSummary = cleanText(note?.excerpt, MAX_SUMMARY_CHARS)
    notes.push({
      key,
      label,
      href,
      summary: rawSummary.toLocaleLowerCase() === label.toLocaleLowerCase()
        ? ''
        : rawSummary,
    })
  }

  const phase = RECALL_PHASES.has(recall.phase) ? recall.phase : ''
  const page = recall?.page && typeof recall.page === 'object'
    ? recall.page
    : {}
  const count = value => Number.isInteger(value) && value >= 0 ? value : null
  return {
    status: recall.status,
    phase,
    query: cleanText(recall.query, MAX_QUERY_CHARS),
    notes,
    noteCount: notes.length,
    candidateCount: count(page.candidate_count),
    hasMore: typeof page.next_cursor === 'string' && page.next_cursor.length > 0,
    reused: recall.reused === true,
    discoveryComplete: typeof recall.discovery_complete === 'boolean'
      ? recall.discovery_complete
      : null,
    receiptMissing: recall.receipt_missing === true,
  }
}
