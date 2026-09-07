import MessagesSquare from 'lucide-react/dist/esm/icons/messages-square.mjs'
import './AgentCoordinationFeed.css'
import {
  groupPeerMessages,
  peerNetworkStatus,
  scopeLabel,
  targetLabel,
} from './agentNetworkModel.js'

function clockLabel(value) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

export default function AgentCoordinationFeed({
  snapshot,
  compact = false,
  loading = false,
  error = false,
  onRetry,
}) {
  const peers = Array.isArray(snapshot?.peers) ? snapshot.peers : []
  const livePeers = peers.filter(peer => peer.online)
  const messages = groupPeerMessages(snapshot?.messages)
  const visibleMessages = messages.slice(compact ? -3 : -6)
  if (compact && peers.length <= 1 && visibleMessages.length === 0) return null

  const liveNames = livePeers
    .map(peer => peer.name)
    .filter(Boolean)
    .slice(0, 3)
  const status = peerNetworkStatus(snapshot)
  const broadcastScope = scopeLabel(snapshot)

  return (
    <section className={`agent-relay${compact ? ' agent-relay--compact' : ''}`} aria-label="Agent network">
      <div className="agent-relay__head">
        <span className={`agent-relay__signal${livePeers.length ? ' is-live' : ''}`} aria-hidden="true" />
        <MessagesSquare size={14} aria-hidden="true" />
        <strong>Agent network</strong>
        <span title={liveNames.join(', ')}>{status}</span>
      </div>

      {loading ? (
        <div className="agent-relay__skeleton" aria-label="Loading agent network"><i /><i /></div>
      ) : error ? (
        <button type="button" className="agent-relay__retry" onClick={onRetry}>Retry agent network</button>
      ) : visibleMessages.length ? (
        <ol className="agent-relay__messages">
          {visibleMessages.map(message => (
            <li key={message.id} className={`agent-relay__message agent-relay__message--${message.kind || 'note'}`}>
              <div>
                <strong>{message.sender_name || 'Agent'}</strong>
                <span aria-hidden="true">→</span>
                <span>{targetLabel(message, broadcastScope)}</span>
                {message.created_at && <time dateTime={message.created_at}>{clockLabel(message.created_at)}</time>}
              </div>
              <p>{message.body}</p>
            </li>
          ))}
        </ol>
      ) : (
        <p className="agent-relay__empty">
          {peers.length > 1
            ? 'No handoffs yet. Decision-changing notes will appear here.'
            : 'Peer handoffs will appear when agents work together.'}
        </p>
      )}
    </section>
  )
}
