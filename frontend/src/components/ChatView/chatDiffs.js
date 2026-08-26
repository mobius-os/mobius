/* Pure chat edit-diff projection shared by the Brain menu and full viewer. */

import { toolEditPreview } from './toolEditPreview.js'

function hashText(value) {
  let hash = 2166136261
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return (hash >>> 0).toString(36)
}

function normalizeEntry(value, fallbackId = '') {
  const rawPreview = value?.preview || value?.edit_preview
  const preview = toolEditPreview(rawPreview)
  if (!preview) return null
  const rawDiff = String(rawPreview.diff || '')
  return {
    id: String(value?.id || value?.tool_use_id || fallbackId
      || `diff-${rawDiff.length}-${hashText(rawDiff)}`),
    tool: String(value?.tool || 'Edit'),
    ts: value?.ts ?? null,
    preview,
  }
}

export function collectChatDiffs(messages) {
  const entries = []
  const positions = new Map()
  for (const message of Array.isArray(messages) ? messages : []) {
    const blocks = Array.isArray(message?.blocks) ? message.blocks : []
    for (const block of blocks) {
      if (block?.type !== 'tool') continue
      const entry = normalizeEntry({
        ...block,
        ts: message?.ts ?? null,
      })
      if (!entry) continue
      const position = positions.get(entry.id)
      if (position === undefined) {
        positions.set(entry.id, entries.length)
        entries.push(entry)
      } else {
        entries[position] = entry
      }
    }
  }
  return entries
}

export function normalizeChatDiffEntries(values) {
  return (Array.isArray(values) ? values : [])
    .map((value, index) => normalizeEntry(value, `diff-${index}`))
    .filter(Boolean)
}

export function mergeChatDiffEntries(authoritative, live) {
  const merged = [...(Array.isArray(authoritative) ? authoritative : [])]
  const positions = new Map(merged.map((entry, index) => [entry.id, index]))
  for (const entry of Array.isArray(live) ? live : []) {
    const position = positions.get(entry.id)
    if (position === undefined) {
      positions.set(entry.id, merged.length)
      merged.push(entry)
    } else {
      // The mounted chat can be a few stream events ahead of the read route.
      // Keep a complete authoritative sidecar over its bounded live twin.
      const current = merged[position]
      merged[position] = current?.preview?.truncated === false
        ? current
        : entry
    }
  }
  return merged
}

export function summarizeChatDiffs(entries) {
  const paths = new Set()
  for (const entry of Array.isArray(entries) ? entries : []) {
    for (const file of entry?.preview?.files || []) {
      if (file?.path) paths.add(file.path)
    }
  }
  return {
    updateCount: Array.isArray(entries) ? entries.length : 0,
    fileCount: paths.size,
  }
}
