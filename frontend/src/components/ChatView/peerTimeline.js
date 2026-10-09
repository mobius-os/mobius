/* Project retained peer mail onto transcript boundaries without changing delivery or stored messages. */
import { isAgentWorkBlock } from './activityGrouping.js'
/** Stored coordinates of one projected block. Recorded positions name a
 * boundary in the stored message's blocks; projections that renumber those
 * blocks (the compact transcript and assistant-fragment folding) declare the
 * stored range each emitted block came from. */
export function storedBlockRange(item) {
  if (item?.type === 'activity' && Number.isInteger(item.start) && Number.isInteger(item.end)) {
    return { start: item.start, end: item.end }
  }
  if (Number.isInteger(item?.raw_index)) return { start: item.raw_index, end: item.raw_index + 1 }
  return null
}

const NO_ACTIVITY_EVENTS = Object.freeze([])

/** Merge loaded activity pages into unique events. The result's identity is
 * the transcript-source signal for ChatView's scroll re-hold, so it changes
 * only with content: query structural sharing keeps `pages` stable across
 * identical refetches, and every empty read shares one array. */
export function activityEventsFromPages(pages) {
  const events = [...new Map((pages || []).flatMap(page => page.events).map(event => [event.id, event])).values()]
  return events.length ? events : NO_ACTIVITY_EVENTS
}

export function peerTime(value) {
  if (typeof value === 'number') return value
  if (typeof value !== 'string' || !value) return NaN
  return Date.parse(/(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`)
}

// Older hidden carriers are the only retained copy of some exchanges. Read only
// the platform-owned carrier kind, never owner prose containing the same tags.
export function carrierMessages(message) {
  if (!message?.hidden || message.kind !== 'peer_message') return []
  const content = message.content || ''
  const start = content.indexOf('<agent_coordination>')
  const end = content.lastIndexOf('</agent_coordination>')
  if (start < 0 || end <= start) return []
  try {
    const payload = JSON.parse(content.slice(start + 20, end).trim())
    return Array.isArray(payload.messages) ? payload.messages.filter(m => m?.id && typeof m.body === 'string') : []
  } catch {
    // A damaged historical carrier must not take down the owner's transcript.
    return []
  }
}

export function projectPeerTimeline(messages, history, chatId, activeTools = []) {
  const records = new Map(history.map(m => [m.id, m]))
  const delivered = new Map()
  messages.forEach((msg, index) => {
    for (const note of carrierMessages(msg)) {
      delivered.set(note.id, { index, mode: msg.steered ? 'during_work' : 'next_turn' })
      if (!records.has(note.id)) records.set(note.id, { ...note, created_at: msg.ts })
    }
  })
  const ordered = [...records.values()].sort((a, b) => peerTime(a.created_at) - peerTime(b.created_at) || a.id.localeCompare(b.id))
  const tools = new Map()
  const represented = new Set()
  // Historical tool markers preserve body but not the mailbox ID. Match one
  // exact outgoing receipt per marker, inside that assistant segment's time range.
  const receiptSegments = [...messages, { ts: messages.at(-1)?.ts ?? 0, blocks: activeTools }]
  receiptSegments.forEach((msg, index) => {
    const next = messages[index + 1]?.ts ?? Infinity
    for (const block of msg.blocks || []) {
      const pm = block.peer_message
      if (pm?.status !== 'sent' || !block.tool_use_id || tools.has(block.tool_use_id)) continue
      const matches = ordered.filter(note => !represented.has(note.id)
        && note.sender_chat_id === chatId && note.body === pm.body
        && (pm.broadcast ? note.broadcast : !note.broadcast && (!pm.peers?.length || pm.peers.includes(note.recipient_name)))
        && peerTime(note.created_at) >= msg.ts && peerTime(note.created_at) <= next).slice(0, pm.count || 1)
      if (!matches.length) continue
      tools.set(block.tool_use_id, matches)
      matches.forEach(note => represented.add(note.id))
    }
  })
  const slots = new Map()
  const positions = new Map()
  for (const note of ordered) {
    if (represented.has(note.id)) continue
    if (note.display_position?.assistant_message_id) {
      const id = note.display_position.assistant_message_id
      const rows = positions.get(id) || []
      rows.push(note)
      positions.set(id, rows)
      tools.set(`peer-${note.id}`, [note])
      continue
    }
    const arrival = peerTime(note.created_at)
    if (!Number.isFinite(arrival)) continue
    const delivery = delivered.get(note.id)
    // Do not pull unseen older history ahead of the loaded transcript window.
    if (!delivery && messages.length && arrival < messages[0].ts) continue
    let index = delivery?.index ?? messages.findIndex(msg => msg.ts > arrival)
    if (index < 0) index = messages.length
    const rows = slots.get(index) || []
    rows.push({ ...note, observedDelivery: delivery?.mode })
    slots.set(index, rows)
  }
  return { slots, tools, positions }
}

export function peerRecordTool(note, chatId) {
  const sent = note.sender_chat_id === chatId
  return {
    type: 'tool', status: 'done', tool: 'PeerMessage',
    peer_message: sent ? {
      direction: 'send', status: 'sent', peers: [note.recipient_name || 'Agent'],
      count: 1, kind: note.kind, delivery: note.delivery, body: note.body, body_truncated: Boolean(note.truncated), broadcast: note.broadcast,
    } : {
      direction: 'read', status: 'received', count: 1,
      notes: [{ sender: note.sender_name || 'Agent', body: note.body, kind: note.kind, delivery: note.delivery, body_truncated: Boolean(note.truncated) }],
    },
  }
}

// Join mail to adjoining tool stretches in the render projection only. Prose,
// questions, and owner messages remain boundaries; hidden carriers are not UI.
const isTransparentActivitySeparator = block => (
  block?.type === 'text' && typeof block.content === 'string' && !block.content.trim()
)

export const isActivityBlock = block => (
  isAgentWorkBlock(block) || isTransparentActivitySeparator(block)
)

/** Synthetic mail has no stored index. Other projected copies keep theirs. */
export function withStoredBlockIndex(block, index) {
  return storedBlockRange(block) || projectedActivityId(block)
    ? block : { ...block, raw_index: index }
}

export function foldPeerActivity(messages, projection, chatId) {
  const rendered = [...messages]
  const slots = new Map(projection.slots)
  const tools = new Map(projection.tools)
  const prepended = new Map()
  for (const [index, notes] of slots) {
    let next = index
    while (next < messages.length && messages[next].hidden) next++
    let prev = index - 1
    while (prev >= 0 && messages[prev].hidden) prev--
    const before = rendered[next]?.role === 'assistant' && isActivityBlock(rendered[next]?.blocks?.[0])
    const after = rendered[prev]?.role === 'assistant' && isActivityBlock(rendered[prev]?.blocks?.at(-1))
    if (!before && !after) continue
    const target = before ? next : prev
    const blocks = notes.map(note => {
      if (note.type === 'helper_result') return note
      const tool = { ...peerRecordTool(note, chatId), tool_use_id: `peer-${note.id}` }
      tools.set(tool.tool_use_id, [note])
      return tool
    })
    const prefixLength = prepended.get(target) || 0
    // Recorded positions count stored blocks, never the synthetic mail rows
    // inserted here. Preserve that coordinate before prepending any activity.
    const stored = rendered[target].blocks.map(withStoredBlockIndex)
    rendered[target] = { ...rendered[target], blocks: before
      ? [...stored.slice(0, prefixLength), ...blocks, ...stored.slice(prefixLength)]
      : [...stored, ...blocks] }
    if (before) prepended.set(target, prefixLength + blocks.length)
    slots.delete(index)
  }
  // Cross-fragment folding happens in AssistantReply, after the authoritative
  // live payload has been selected. A saved partial must never replace it.
  return { messages: rendered, slots, tools, positions: projection.positions }
}

const projectedActivityId = block => {
  if (block?.type === 'helper_result') return block.activityId || block.id
  return block?.type === 'tool' && block.tool === 'PeerMessage'
    && typeof block.tool_use_id === 'string' && block.tool_use_id.startsWith('peer-')
    ? block.tool_use_id : null
}

const blockIdentity = block => block?.tool_use_id
  || block?.thinking_id
  || block?.question_id
  || null

const sameBlock = (left, right, rawIndex) => left === right || (
  blockIdentity(left) && blockIdentity(left) === blockIdentity(right)
) || (
  // Projection stamps source coordinates even on id-less prose/card blocks.
  // Those copies must still locate the raw mirror inside its boundary rows.
  left?.type === right?.type
  && storedBlockRange(right)?.start === (storedBlockRange(left)?.start ?? rawIndex)
)

/** Carry projected peer/helper rows around the selected live payload. The raw
 * mirror locates both boundaries; its older tool state never replaces live data. */
export function mergeProjectedActivity(liveBlocks = [], projectedBlocks = [], rawBlocks = []) {
  if (projectedBlocks.length <= rawBlocks.length) return liveBlocks
  const start = rawBlocks.length
    ? projectedBlocks.findIndex((_, index) => rawBlocks.every((block, offset) => sameBlock(block, projectedBlocks[index + offset], offset)))
    : projectedBlocks.length
  if (start < 0) return liveBlocks
  const existing = new Set(liveBlocks.map(projectedActivityId).filter(Boolean))
  const missing = blocks => blocks.filter(block => {
    const id = projectedActivityId(block)
    if (!id || existing.has(id)) return false
    existing.add(id)
    return true
  })
  const before = missing(projectedBlocks.slice(0, start))
  const after = missing(projectedBlocks.slice(start + rawBlocks.length))
  if (!before.length && !after.length) return liveBlocks
  // Recorded positions count stored blocks, never the projected rows inserted
  // here. Stamp that coordinate before prepending, as foldPeerActivity does.
  return [...before, ...liveBlocks.map(withStoredBlockIndex), ...after]
}
