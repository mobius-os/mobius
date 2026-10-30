/* History of a pause that a later continuation resolved: the chat keeps a quiet
 * record of why work stopped (a platform update, low storage, a limit) next to
 * the "Resumed" notice, with no Resume control or "will continue" promise. */

import { Pause } from '@openai/apps-sdk-ui/components/Icon'
import MarkerCard from './MarkerCard.jsx'
import { errorCardViewModel } from './ErrorCard.jsx'

const TITLES = {
  restart: 'Paused for a platform update',
  storage: 'Paused while storage was low',
  memory: 'Paused while memory was low',
  rate_limit: 'Paused at a rate limit',
  credits: 'Paused while out of credits',
  model_capacity: 'Paused while the model was busy',
  model_capacity_exhausted: 'Paused while the model was busy',
}

// The record wraps the original pause, so it uses the live card's
// classification, including legacy notes that predate `pause.kind`.
export function pauseRecordTitle(source = {}) {
  const view = errorCardViewModel(source)
  if (view.settlementRecovery) return 'Work interrupted'
  if (view.goalHandoff) return 'Goal paused'
  if (TITLES[source.pause?.kind]) return TITLES[source.pause.kind]
  if (view.parked) return 'Paused at a usage limit'
  return view.benign ? 'Paused' : 'Work interrupted'
}

export default function PauseRecordCard({ block }) {
  return (
    <MarkerCard
      title={pauseRecordTitle(block.source)}
      icon={<Pause width={14} height={14} aria-hidden="true" />}
    />
  )
}
