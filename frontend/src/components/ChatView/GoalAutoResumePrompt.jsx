/* GoalAutoResumePrompt makes the existing server-owned limit continuation
   policy discoverable before a long Goal reaches a provider limit. */

import { useState } from 'react'

import {
  isGoalAutoResumeDismissed,
  rememberGoalAutoResumeDismissed,
} from './goalAutoResumeDismissal.js'

/* Only an open Goal continues automatically: the server never resumes a held
   (stopped) Goal (goals.goal_allows_automatic_resume), and a held Goal is the
   one that carries a pause_reason (goal_plans._goal_presentation). */
function goalContinuesAutomatically(goal) {
  if (goal?.status === 'active') return true
  return goal?.status === 'paused' && !goal.pause_reason
}

/* `policyKnown` waits for chat info: until it loads, autoResumeEnabled reads
   false even when the policy is on. While a limit park is pending, its own
   card owns this decision (and Settings always carries the switch), so the
   banner stays out of the way. */
export function shouldOfferGoalAutoResume({
  embedded = false,
  goal = null,
  autoResumeEnabled = false,
  policyKnown = false,
  limitParked = false,
}) {
  return policyKnown
    && !embedded
    && !limitParked
    && goalContinuesAutomatically(goal)
    && !autoResumeEnabled
}

export default function GoalAutoResumePrompt({
  chatId = '',
  goalKey,
  saving = false,
  error = '',
  onEnable,
}) {
  const [dismissedGoalKey, setDismissedGoalKey] = useState(null)

  if (!goalKey || !onEnable) return null
  if (dismissedGoalKey === goalKey) return null
  if (isGoalAutoResumeDismissed(chatId, goalKey)) return null

  const dismiss = () => {
    rememberGoalAutoResumeDismissed(chatId, goalKey)
    setDismissedGoalKey(goalKey)
  }

  /* The copy states what enabling really does: the switch is per chat, saving
     it also seeds the default for chats the owner starts afterwards
     (owner.auto_resume_on_limit_default in routes/chats.py), and the server
     retries around the reported reset, or after a fixed wait when none is
     reported, never exactly at the reset (chat._limit_park_fields). */
  return (
    <aside className="chat__goal-auto-resume" aria-label="Goal continuation">
      <div className="chat__goal-auto-resume-copy">
        <strong>Continue this chat after usage limits?</strong>
        <span>
          Möbius can try again around the provider&apos;s reset time, or after
          a short wait if none is given, even while this device sleeps. Retries
          may use paid usage, and new chats you start will have this on. Manual
          stops stay stopped.
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
          {saving ? 'Enabling…' : 'Auto-continue this chat'}
        </button>
        <button
          type="button"
          className="chat__goal-auto-resume-later"
          onPointerDown={(event) => event.preventDefault()}
          onClick={dismiss}
          disabled={saving}
        >
          Not now
        </button>
      </div>
      {error && <span className="chat__goal-auto-resume-error" role="alert">{error}</span>}
    </aside>
  )
}
