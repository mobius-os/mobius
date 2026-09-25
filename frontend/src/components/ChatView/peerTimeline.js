/* Project retained peer mail onto transcript boundaries without changing delivery or stored messages. */
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
      count: 1, kind: note.kind, body: note.body, body_truncated: Boolean(note.truncated), broadcast: note.broadcast,
    } : {
      direction: 'read', status: 'received', count: 1,
      notes: [{ sender: note.sender_name || 'Agent', body: note.body, kind: note.kind, body_truncated: Boolean(note.truncated) }],
    },
  }
}

// Join mail to adjoining tool stretches in the render projection only. Prose,
// questions, and owner messages remain boundaries; hidden carriers are not UI.
const assistantFragmentRoot = message => {
  if (message?.role !== 'assistant' || typeof message.id !== 'string') return null
  return message.id.replace(/:assistant:\d+$/, '')
}

const isTransparentActivitySeparator = block => (
  block?.type === 'text' && typeof block.content === 'string' && !block.content.trim()
)

const isActivityBlock = block => (
  block?.type === 'tool'
  || block?.type === 'thinking'
  || block?.type === 'activity'
  || isTransparentActivitySeparator(block)
)

/** Join the activity-only seam between saved fragments of one assistant turn.
 * Hidden delivery carriers may sit between the fragments, but prose, cards,
 * errors, owner messages, or an unplaced timeline row remain hard boundaries. */
export function foldAssistantActivityFragments(
  messages,
  slots,
  activeMirrorIndex = -1,
  sourcePositions = new Map(),
) {
  const rendered = [...messages]
  const positions = new Map(sourcePositions)
  const nextVisible = start => {
    let index = start
    while (index < rendered.length && rendered[index]?.hidden) index += 1
    return index
  }
  const hasSlotBetween = (start, end) => {
    for (let index = start + 1; index <= end; index += 1) {
      if (slots.has(index)) return true
    }
    return false
  }
  for (let index = 0; index < rendered.length; index += 1) {
    if (index === activeMirrorIndex || rendered[index]?.hidden) continue
    const root = assistantFragmentRoot(rendered[index])
    if (!root) continue
    let targetIndex = index
    let target = rendered[targetIndex]
    let candidateIndex = nextVisible(targetIndex + 1)
    while (
      candidateIndex < rendered.length
      && candidateIndex !== activeMirrorIndex
      && assistantFragmentRoot(rendered[candidateIndex]) === root
      && !hasSlotBetween(targetIndex, candidateIndex)
    ) {
      const targetBlocks = target.blocks || []
      const candidate = rendered[candidateIndex]
      const candidateBlocks = candidate.blocks || []
      if (!isActivityBlock(targetBlocks.at(-1)) || !isActivityBlock(candidateBlocks[0])) break
      let activityEnd = 0
      while (activityEnd < candidateBlocks.length && isActivityBlock(candidateBlocks[activityEnd])) {
        activityEnd += 1
      }
      // Folding renumbers the candidate's blocks, so each one carries the
      // stored index its recorded positions refer to; moved blocks also name
      // the stored message they came from.
      const storedCandidate = candidateBlocks.map((block, storedIndex) => (
        storedBlockRange(block) ? block : { ...block, raw_index: storedIndex }
      ))
      const leadingActivity = storedCandidate
        .slice(0, activityEnd)
        .filter(block => !isTransparentActivitySeparator(block))
        .map(block => ({
          ...block,
          source_message_id: block.source_message_id ?? candidate.id,
        }))
      target = { ...target, blocks: [...targetBlocks, ...leadingActivity] }
      rendered[targetIndex] = target
      const rawBoundary = storedCandidate
        .slice(0, activityEnd)
        .reduce((boundary, block) => Math.max(boundary, storedBlockRange(block).end), 0)
      const candidateNotes = positions.get(candidate.id) || []
      const movingNotes = candidateNotes.filter(note => (
        Number.isInteger(note.display_position?.block_index)
        && note.display_position.block_index < rawBoundary
      ))
      if (movingNotes.length) {
        positions.set(target.id, [
          ...(positions.get(target.id) || []),
          ...movingNotes.map(note => ({
            ...note,
            display_position: {
              ...note.display_position,
              assistant_message_id: target.id,
              source_message_id: note.display_position.source_message_id ?? candidate.id,
            },
          })),
        ])
        const movingIds = new Set(movingNotes.map(note => note.id))
        const stayingNotes = candidateNotes.filter(note => !movingIds.has(note.id))
        if (stayingNotes.length) positions.set(candidate.id, stayingNotes)
        else positions.delete(candidate.id)
      }
      const remaining = storedCandidate.slice(activityEnd)
      rendered[candidateIndex] = remaining.length
        ? { ...candidate, blocks: remaining }
        : { ...candidate, hidden: true, _folded_activity_fragment: true }
      if (remaining.length) break
      candidateIndex = nextVisible(candidateIndex + 1)
    }
  }
  return { messages: rendered, positions }
}

export function foldPeerActivity(messages, projection, chatId, activeMirrorIndex = -1) {
  const rendered = [...messages]
  const slots = new Map(projection.slots)
  const tools = new Map(projection.tools)
  const prepended = new Map()
  for (const [index, notes] of slots) {
    // Helper results own a standalone disclosure. Keep a mixed timestamp slot
    // intact rather than folding only its peer rows across that ordering seam.
    if (notes.some(note => note.type === 'helper_result')) continue
    let next = index
    while (next < messages.length && messages[next].hidden) next++
    let prev = index - 1
    while (prev >= 0 && messages[prev].hidden) prev--
    const before = rendered[next]?.role === 'assistant' && isActivityBlock(rendered[next]?.blocks?.[0])
    const after = rendered[prev]?.role === 'assistant' && isActivityBlock(rendered[prev]?.blocks?.at(-1))
    if (!before && !after) continue
    const target = before ? next : prev
    const blocks = notes.map(note => {
      const tool = { ...peerRecordTool(note, chatId), tool_use_id: `peer-${note.id}` }
      tools.set(tool.tool_use_id, [note])
      return tool
    })
    const prefixLength = prepended.get(target) || 0
    rendered[target] = { ...rendered[target], blocks: before
      ? [...rendered[target].blocks.slice(0, prefixLength), ...blocks, ...rendered[target].blocks.slice(prefixLength)]
      : [...rendered[target].blocks, ...blocks] }
    if (before) prepended.set(target, prefixLength + blocks.length)
    slots.delete(index)
  }
  const folded = foldAssistantActivityFragments(
    rendered,
    slots,
    activeMirrorIndex,
    projection.positions,
  )
  return {
    messages: folded.messages,
    slots,
    tools,
    positions: folded.positions,
  }
}

const peerToolId = block => (
  block?.type === 'tool'
  && block.tool === 'PeerMessage'
  && typeof block.tool_use_id === 'string'
  && block.tool_use_id.startsWith('peer-')
    ? block.tool_use_id
    : null
)

const blockIdentity = block => block?.tool_use_id
  || block?.thinking_id
  || block?.question_id
  || null

const sameBlock = (left, right) => left === right || (
  blockIdentity(left) && blockIdentity(left) === blockIdentity(right)
)

/** Carry boundary peer rows from the read-only timeline projection into the
 * live payload without replacing the stream's newer tool/thinking state.
 * foldPeerActivity only adds peer tools at a message boundary, so the raw
 * mirror identifies whether they belong before or after its blocks. */
export function mergeProjectedPeerActivity(liveBlocks = [], projectedBlocks = [], rawBlocks = []) {
  if (!projectedBlocks.length || projectedBlocks.length <= rawBlocks.length) return liveBlocks
  const extra = projectedBlocks.length - rawBlocks.length
  let peers = []
  let atStart = false
  if (rawBlocks.every((block, index) => sameBlock(block, projectedBlocks[index + extra]))) {
    peers = projectedBlocks.slice(0, extra).filter(peerToolId)
    atStart = true
  } else if (rawBlocks.every((block, index) => sameBlock(block, projectedBlocks[index]))) {
    peers = projectedBlocks.slice(rawBlocks.length).filter(peerToolId)
  }
  if (!peers.length) return liveBlocks
  const existing = new Set(liveBlocks.map(peerToolId).filter(Boolean))
  const missing = peers.filter(block => !existing.has(peerToolId(block)))
  if (!missing.length) return liveBlocks
  return atStart ? [...missing, ...liveBlocks] : [...liveBlocks, ...missing]
}
