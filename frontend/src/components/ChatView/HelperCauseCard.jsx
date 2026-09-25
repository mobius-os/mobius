/* Name the helpers whose results started this answer; their rows hold the rest. */

import { ArrowDown } from '@openai/apps-sdk-ui/components/Icon'
import MarkerCard from './MarkerCard.jsx'

export default function HelperCauseCard({ causes }) {
  const names = causes.map(cause => cause.task_key || 'Helper').join(', ')
  const settled = causes.every(cause => cause.status === 'completed')
  const title = causes.length === 1
    ? (settled ? 'Helper finished' : 'Helper stopped')
    : (settled ? 'Helpers finished' : 'Helpers settled')
  return (
    <MarkerCard
      title={title}
      subtitle={names}
      icon={<ArrowDown width={14} height={14} aria-hidden="true" />}
    />
  )
}
