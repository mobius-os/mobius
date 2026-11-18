/* Render a durable continuation event without attributing it to the owner. */

import { ArrowRotateCw } from '@openai/apps-sdk-ui/components/Icon'
import MarkerCard from './MarkerCard.jsx'
import { pauseRecordTitle } from './PauseRecordCard.jsx'
import { formatDateTime, formatTime } from '../../lib/dateTimeFormat.js'
import { RESOURCE_PAUSE_LABELS } from './waitingPresentation.js'

function EventTime({ ts }) {
  if (!Number.isFinite(ts)) return null
  const date = new Date(ts)
  return <time dateTime={date.toISOString()} title={formatDateTime(date)}>
    {date.toDateString() === new Date().toDateString() ? formatTime(date) : formatDateTime(date)}
  </time>
}

export function continuationTitle(reason) {
  if (reason === 'goal_handoff') return 'Continuing Goal'
  if (reason === 'question_answer') return 'Continued after your answer'
  if (reason === 'helper_answer') return 'Continued after parent answer'
  return reason === 'manual' ? 'Resumed manually' : 'Resumed automatically'
}

export default function ContinuationCard({ msg }) {
  const manual = msg?.continuation_reason === 'manual'
  const reason = msg?.continuation_reason
  const title = continuationTitle(reason)
  const subtitle = {
    manual: 'Interrupted work continued',
    question_answer: 'Your answer continued the interrupted work',
    restart: 'Server restarted — continuing automatically',
    usage_limit: 'Retry check due — trying the provider again',
    ...Object.fromEntries(
      Object.entries(RESOURCE_PAUSE_LABELS).map(([kind, labels]) => [kind, labels.resumed]),
    ),
    model_capacity: 'Model available again — continuing automatically',
    goal_handoff: 'Continuing toward the Goal',
    helper_answer: 'The parent agent answered this helper’s question — continuing',
  }[reason] || (!manual ? 'Interrupted work recovered — continuing automatically' : null)

  // When the work continued: for a recovery answer or marker row, its own
  // timestamp is the moment the resumed turn started.
  const events = msg?.recovery_events || []
  const pauses = events.filter(event => event.type === 'pause')
  const causes = [...new Set(pauses.map(event => pauseRecordTitle(event.source)))]
  return (
    <MarkerCard
      title={Number.isFinite(msg?.ts) ? <>
        {title}
        {' · '}
        <EventTime ts={msg.ts} />
      </> : title}
      subtitle={causes.length ? causes.join(' · ') : subtitle}
      icon={<ArrowRotateCw width={14} height={14} aria-hidden="true" />}
    >
      {events.length > 0 && <ol className="chat__recovery-events">
        {events.map((event, index) => <li key={index}>
          <span>{event.type === 'pause' ? pauseRecordTitle(event.source)
            : continuationTitle(event.reason)}</span>
          <EventTime ts={event.ts} />
        </li>)}
      </ol>}
    </MarkerCard>
  )
}
