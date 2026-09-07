/* ChatAgentNetwork summarizes this chat's mailbox while the Brain is open. */
import { useQuery } from '@tanstack/react-query'
import { Chat } from '@openai/apps-sdk-ui/components/Icon'
import { api, jsonOrThrow } from '../../api/client.js'

export function networkSummary(data) {
  if (!data) return 'Message history for this chat'
  if (!data.total) return 'No messages yet'
  return `${data.total} ${data.total === 1 ? 'message' : 'messages'} · ${data.sent} sent · ${data.received} received`
}

export default function ChatAgentNetwork({ chatId, onOpen }) {
  const query = useQuery({
    queryKey: ['chat-network-summary', chatId],
    queryFn: async () => jsonOrThrow(await api.agentCoordination.history(chatId, { limit: 1 }), 'Agent network failed:'),
    enabled: Boolean(chatId),
    staleTime: 5_000,
    refetchInterval: 10_000,
    retry: 0,
  })
  if (!chatId) return null
  return <div className="composer-popover__section">
    <button type="button" className="composer-popover__row" onClick={onOpen} aria-haspopup="dialog">
      <span className="composer-popover__row-icon" aria-hidden="true"><Chat width={18} height={18} /></span>
      <span className="composer-popover__row-main">
        <span className="composer-popover__row-title">Agent network</span>
        <span className="composer-popover__row-sub">{query.isLoading ? 'Checking messages…' : query.isError ? 'History unavailable · open to retry' : networkSummary(query.data)}</span>
      </span>
    </button>
  </div>
}
