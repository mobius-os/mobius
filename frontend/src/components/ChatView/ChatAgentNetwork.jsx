/* ChatAgentNetwork owns the Brain's on-demand chat collaboration disclosure. */
import { useId, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Chat, ChevronDown } from '@openai/apps-sdk-ui/components/Icon'
import { api, jsonOrThrow } from '../../api/client.js'
import AgentCoordinationFeed from '../Agents/AgentCoordinationFeed.jsx'

function NetworkActivity({ chatId }) {
  const query = useQuery({
    queryKey: ['agent-coordination', 'chat', chatId],
    queryFn: async () => jsonOrThrow(
      await api.agentCoordination.chat(chatId), 'Agent network failed:',
    ),
    refetchInterval: 5_000,
    staleTime: 1_500,
    retry: 0,
  })
  return <AgentCoordinationFeed
    snapshot={query.data}
    loading={query.isLoading}
    error={query.isError}
    onRetry={() => query.refetch()}
  />
}

export default function ChatAgentNetwork({ chatId }) {
  const [expanded, setExpanded] = useState(false)
  const regionId = useId()
  if (!chatId) return null
  return <div className="composer-popover__section composer-network">
    <button
      type="button"
      className="composer-popover__row"
      aria-expanded={expanded}
      aria-controls={regionId}
      onClick={() => setExpanded(value => !value)}
    >
      <span className="composer-popover__row-icon" aria-hidden="true"><Chat width={18} height={18} /></span>
      <span className="composer-popover__row-main">
        <span className="composer-popover__row-title">Agent network</span>
        <span className="composer-popover__row-sub">Collaborating agents and recent handoffs</span>
      </span>
      <ChevronDown width={15} height={15} aria-hidden="true" style={{ transform: expanded ? 'rotate(180deg)' : undefined }} />
    </button>
    {expanded && <div id={regionId} className="composer-network__activity">
      <NetworkActivity chatId={chatId} />
    </div>}
  </div>
}
