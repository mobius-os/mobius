/* Render a durable continuation event without attributing it to the owner. */

import { ArrowRotateCw } from '@openai/apps-sdk-ui/components/Icon'
import MarkerCard from './MarkerCard.jsx'
import { formatDateTime, formatTime } from '../../lib/dateTimeFormat.js'

export default function ContinuationCard({ msg }) {
  const manual = msg?.continuation_reason === 'manual'
  const reason = msg?.continuation_reason
  const title = reason === 'goal_handoff'
    ? 'Continuing Goal'
    : manual ? 'Resumed manually' : 'Resumed automatically'
  const subtitle = {
    manual: 'Interrupted work continued',
    restart: 'Server restarted — continuing automatically',
    usage_limit: 'Retry check due — trying the provider again',
    memory: 'Memory freed up — continuing automatically',
    storage: 'Storage freed up — continuing automatically',
    model_capacity: 'Model available again — continuing automatically',
    goal_handoff: 'Continuing toward the Goal',
    helper_answer: 'The parent agent answered this helper’s question — continuing',
  }[reason] || (!manual ? 'Interrupted work recovered — continuing automatically' : null)

  // When the work continued: for a recovery answer or marker row, its own
  // timestamp is the moment the resumed turn started.
  const resumedAt = Number.isFinite(msg?.ts) ? new Date(msg.ts) : null
  return (
    <MarkerCard
      title={resumedAt ? <>
        {title}
        {' · '}
        <time dateTime={resumedAt.toISOString()} title={formatDateTime(resumedAt)}>
          {resumedAt.toDateString() === new Date().toDateString()
            ? formatTime(resumedAt) : formatDateTime(resumedAt)}
        </time>
      </> : title}
      subtitle={subtitle}
      icon={<ArrowRotateCw width={14} height={14} aria-hidden="true" />}
    />
  )
}
