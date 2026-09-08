export function groupPeerMessages(messages) {
  const groups = []
  for (const message of Array.isArray(messages) ? messages : []) {
    const previous = groups.at(-1)
    const timestamp = new Date(message.created_at).getTime()
    const previousTimestamp = new Date(previous?.created_at).getTime()
    const sameStableSend = previous
      && message.send_id
      && message.send_id === previous.send_id
    const sameLegacySend = previous
      && !message.send_id
      && !previous.send_id
      && !message.broadcast
      && !previous.broadcast
      && message.sender_chat_id === previous.sender_chat_id
      && message.kind === previous.kind
      && message.body === previous.body
      && Number.isFinite(timestamp)
      && Number.isFinite(previousTimestamp)
      && Math.abs(timestamp - previousTimestamp) < 250
    if (!sameStableSend && !sameLegacySend) {
      groups.push({ ...message, recipient_names: [recipientLabel(message)] })
      continue
    }
    previous.recipient_names.push(recipientLabel(message))
  }
  return groups
}

export function scopeLabel(snapshot) {
  if (snapshot?.scope?.kind === 'project') return 'Project scope'
  if (snapshot?.scope?.kind === 'delegation') return 'Goal scope'
  return 'This scope'
}

export function peerNetworkStatus(snapshot) {
  const peers = Array.isArray(snapshot?.peers) ? snapshot.peers : []
  const live = peers.filter(peer => peer.online)
  const scopeIds = new Set(
    Array.isArray(snapshot?.scope_peer_ids)
      ? snapshot.scope_peer_ids.map(String)
      : [],
  )
  const here = live.filter(peer => scopeIds.has(String(peer.id)))
  const total = Number.isInteger(snapshot?.peer_total)
    ? snapshot.peer_total
    : peers.length
  const onlineTotal = Number.isInteger(snapshot?.online_peer_total)
    ? snapshot.online_peer_total
    : live.length
  const suffix = snapshot?.peers_truncated ? '+' : ''
  if (onlineTotal) {
    return here.length === onlineTotal
      ? `${onlineTotal} connected`
      : `${here.length} here · ${onlineTotal} connected`
  }
  return total > 1 ? `${total}${suffix} peers` : 'Quiet'
}

export function targetLabel(message, broadcastScope) {
  if (message.broadcast) return broadcastScope
  const names = [...new Set(message.recipient_names || [])]
  if (names.length === 1) return names[0]
  return `${names.length} agents`
}

function recipientLabel(message) {
  if (message.broadcast) return 'Scope'
  return message.recipient_name || 'Direct note'
}
