/* Activity is a read-only projection, not conversation or permission to resume. */
import { peerTime, projectPeerTimeline } from './peerTimeline.js'

export function projectChatActivity(messages, events, chatId, activeTools = []) {
  const unique = new Map(events.map(event => [event.id, event]))
  const peers = [...unique.values()].filter(event => event.type === 'peer_message').map(event => ({
    ...event,
    // The activity envelope namespaces IDs; legacy carriers retain mailbox IDs.
    id: event.id.replace(/^peer:/, ''),
  }))
  const { slots, tools, positions } = projectPeerTimeline(messages, peers, chatId, activeTools)
  for (const [index, notes] of slots) {
    slots.set(index, notes.map(note => ({ ...note, type: 'peer_message', activityId: `peer:${note.id}` })))
  }
  for (const [messageId, notes] of positions) {
    positions.set(messageId, notes.map(note => ({ ...note, type: 'peer_message', activityId: `peer:${note.id}` })))
  }
  for (const event of unique.values()) {
    if (event.type !== 'helper_result') continue
    const positionedMessageId = event.display_position?.assistant_message_id
    if (positionedMessageId) {
      const rows = positions.get(positionedMessageId) || []
      rows.push({ ...event, activityId: event.id })
      positions.set(positionedMessageId, rows)
      continue
    }
    const arrival = peerTime(event.created_at)
    if (!Number.isFinite(arrival) || (messages.length && arrival < messages[0].ts)) continue
    let index = messages.findIndex(message => message.ts > arrival)
    if (index < 0) index = messages.length
    const rows = slots.get(index) || []
    rows.push({ ...event, activityId: event.id })
    slots.set(index, rows)
  }
  for (const rows of slots.values()) {
    rows.sort((a, b) => peerTime(a.created_at) - peerTime(b.created_at) || a.activityId.localeCompare(b.activityId))
  }
  return { slots, tools, positions }
}
