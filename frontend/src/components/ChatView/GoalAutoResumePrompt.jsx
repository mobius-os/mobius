/* GoalAutoResumePrompt makes the existing server-owned limit continuation
   policy discoverable before a long Goal reaches a provider limit. */

import { useState } from 'react'

export function shouldOfferGoalAutoResume({
  embedded = false,
  goalStatus = '',
  autoResumeEnabled = false,
}) {
  return !embedded
    && ['active', 'paused'].includes(goalStatus)
    && !autoResumeEnabled
}

export default function GoalAutoResumePrompt({
  goalKey,
  saving = false,
  error = '',
  onEnable,
}) {
  const [dismissedGoalKey, setDismissedGoalKey] = useState(null)

  if (!goalKey || dismissedGoalKey === goalKey || !onEnable) return null

  return (
    <aside className="chat__goal-auto-resume" aria-label="Goal continuation">
      <div className="chat__goal-auto-resume-copy">
        <strong>Keep this Goal moving after a usage limit?</strong>
        <span>
          Möbius can continue at the provider&apos;s reported reset—even while
          this device sleeps. Manual stops stay stopped.
        </span>
      </div>
      <div className="chat__goal-auto-resume-actions">
        <button
          type="button"
          className="chat__goal-auto-resume-enable"
          onPointerDown={(event) => event.preventDefault()}
          onClick={() => onEnable(true)}
          disabled={saving}
        >
          {saving ? 'Enabling…' : 'Continue after resets'}
        </button>
        <button
          type="button"
          className="chat__goal-auto-resume-later"
          onPointerDown={(event) => event.preventDefault()}
          onClick={() => setDismissedGoalKey(goalKey)}
          disabled={saving}
        >
          Not now
        </button>
      </div>
      {error && <span className="chat__goal-auto-resume-error" role="alert">{error}</span>}
    </aside>
  )
}
