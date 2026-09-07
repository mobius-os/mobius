// Pure view model for the peer-message tool card, keeping bounds and shapes
// directly testable without mounting React. Mirrors memoryRecallCard.js.

// Preserve the full note (the platform caps a note at 4,000 chars) so the
// expandable card shows the whole thing in its scrollable detail region — the
// owner never sees a silent excerpt. body_truncated only trips past this bound.
const MAX_BODY_CHARS = 4000
const MAX_NAME_CHARS = 120
const MAX_NOTES = 8
const MAX_RESULT_MESSAGES = 200 // the read tool's max page; count can't exceed it
const PEER_STATUSES = new Set([
  'sending', 'reading', 'sent', 'received', 'empty', 'failed',
])
// The Möbius-owned message-kind taxonomy (app.agent_coordination). An unknown
// value falls back to the neutral "note" so the badge never renders empty.
const PEER_KINDS = new Set(['note', 'finding', 'request', 'blocker', 'handoff'])

function cleanLabelText(value, limit) {
  if (typeof value !== 'string') return ''
  return value.replace(/\s+/g, ' ').trim().slice(0, limit)
}

function cleanBody(value) {
  if (typeof value !== 'string') return ''
  return value.trim().slice(0, MAX_BODY_CHARS)
}

function cleanKind(value) {
  return PEER_KINDS.has(value) ? value : 'note'
}

function cleanNames(value) {
  const names = []
  const seen = new Set()
  for (const raw of Array.isArray(value) ? value : []) {
    const name = cleanLabelText(raw, MAX_NAME_CHARS)
    if (!name || seen.has(name)) continue
    seen.add(name)
    names.push(name)
    if (names.length >= MAX_NOTES) break
  }
  return names
}

export function peerMessageCardModel(pm) {
  if (!pm || typeof pm !== 'object' || !PEER_STATUSES.has(pm.status)) return null
  const direction = pm.direction === 'send'
    ? 'send'
    : (pm.direction === 'read' ? 'read' : null)
  // Every marker — including a failure — must carry a valid direction.
  if (!direction) return null

  if (['sending', 'sent'].includes(pm.status) && direction !== 'send') return null
  if (['reading', 'received', 'empty'].includes(pm.status)
      && direction !== 'read') return null

  if (pm.status === 'sent') {
    const peers = cleanNames(pm.peers)
    const body = cleanBody(pm.body)
    // The backend counts distinct recipient chats; trust it, bounded to the
    // API page. "truncated" (more than the 8 shown) is derived, not mirrored.
    const count = Number.isInteger(pm.count) && pm.count >= peers.length
      ? Math.min(pm.count, MAX_RESULT_MESSAGES)
      : peers.length
    return {
      status: 'sent',
      direction: 'send',
      kind: cleanKind(pm.kind),
      peers,
      count,
      truncated: count > peers.length,
      broadcast: Boolean(pm.broadcast),
      body,
      bodyTruncated: Boolean(pm.body_truncated),
      hasDetail: Boolean(body) || Boolean(pm.broadcast) || peers.length > 0,
    }
  }

  if (pm.status === 'received') {
    const notes = []
    const source = Array.isArray(pm.notes) ? pm.notes : []
    for (let i = 0; i < source.length && notes.length < MAX_NOTES; i += 1) {
      const note = source[i]
      const body = cleanBody(note?.body)
      if (!body) continue
      notes.push({
        key: i,
        sender: cleanLabelText(note?.sender, MAX_NAME_CHARS),
        kind: cleanKind(note?.kind),
        body,
        bodyTruncated: Boolean(note?.body_truncated),
      })
    }
    const count = Number.isInteger(pm.count) && pm.count >= notes.length
      ? Math.min(pm.count, MAX_RESULT_MESSAGES)
      : notes.length
    return {
      status: 'received',
      direction: 'read',
      count,
      notes,
      truncated: count > notes.length,
      hasDetail: notes.length > 0,
    }
  }

  if (pm.status === 'failed') {
    const reason = cleanLabelText(pm.reason, MAX_BODY_CHARS)
    return { status: 'failed', direction, reason, hasDetail: Boolean(reason) }
  }
  return { status: pm.status, direction, hasDetail: false }
}
