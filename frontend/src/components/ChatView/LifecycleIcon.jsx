/* LifecycleIcon keeps event identity separate from its outcome across chat notices. */
import { Flag, Clock, Warning, Stop } from '@openai/apps-sdk-ui/components/Icon'

export default function LifecycleIcon({ kind, children }) {
  const Icon = kind === 'goal' ? Flag : Clock
  return <span className="chat__lifecycle-icon" aria-hidden="true">
    {children || <Icon width={18} height={18} />}
  </span>
}

export function LifecycleOutcome({ tone }) {
  if (tone === 'completed') return null
  const Icon = tone === 'stopped' ? Stop : Warning
  return <Icon className="chat__lifecycle-outcome" width={14} height={14} aria-hidden="true" />
}
