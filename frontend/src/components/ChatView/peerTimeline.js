/* Project retained peer mail onto transcript boundaries without changing delivery or stored messages. */
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
export function foldPeerActivity(messages, projection, chatId, activeMirrorIndex = -1) {
  const rendered = [...messages]
  const slots = new Map(projection.slots)
  const tools = new Map(projection.tools)
  const prepended = new Map()
  const isActivity = block => block?.type === 'tool' || block?.type === 'thinking'
  for (const [index, notes] of slots) {
    // Helper results own a standalone disclosure. Keep a mixed timestamp slot
    // intact rather than folding only its peer rows across that ordering seam.
    if (notes.some(note => note.type === 'helper_result')) continue
    let next = index
    while (next < messages.length && messages[next].hidden) next++
    let prev = index - 1
    while (prev >= 0 && messages[prev].hidden) prev--
    const before = next !== activeMirrorIndex && rendered[next]?.role === 'assistant' && isActivity(rendered[next]?.blocks?.[0])
    const after = prev !== activeMirrorIndex && rendered[prev]?.role === 'assistant' && isActivity(rendered[prev]?.blocks?.at(-1))
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
  return { messages: rendered, slots, tools, positions: projection.positions }
}
