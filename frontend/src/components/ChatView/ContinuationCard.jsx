/* Render a durable continuation event without attributing it to the owner. */

import { ArrowRotateCw } from '@openai/apps-sdk-ui/components/Icon'
import MarkerCard from './MarkerCard.jsx'

export default function ContinuationCard({ msg }) {
  const manual = msg?.continuation_reason === 'manual'
  const reason = msg?.continuation_reason
  const title = manual ? 'Resumed manually' : 'Resumed automatically'
  const subtitle = {
    restart: 'Server restarted — continuing automatically',
    usage_limit: 'Usage available again — continuing automatically',
    memory: 'Memory freed up — continuing automatically',
    storage: 'Storage freed up — continuing automatically',
  }[reason]

  return (
    <MarkerCard
      title={title}
      subtitle={subtitle}
      icon={<ArrowRotateCw width={14} height={14} aria-hidden="true" />}
    />
  )
}
