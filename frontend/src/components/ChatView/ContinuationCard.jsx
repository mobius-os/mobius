/* Render a durable continuation event without attributing it to the owner. */

import { ArrowRotateCw } from '@openai/apps-sdk-ui/components/Icon'
import MarkerCard from './MarkerCard.jsx'

export default function ContinuationCard({ msg }) {
  const manual = msg?.continuation_reason === 'manual'
  const restarted = msg?.continuation_reason === 'restart'
  const title = manual
    ? 'Resumed manually'
    : (restarted
        ? 'Server restarted — continuing automatically'
        : 'Usage available again — continuing automatically')

  return (
    <MarkerCard
      title={title}
      icon={<ArrowRotateCw width={14} height={14} aria-hidden="true" />}
    />
  )
}
