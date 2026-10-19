/* GoalAutoResumePrompt makes the existing server-owned limit continuation
   policy discoverable before a long Goal reaches a provider limit. */

import { useState } from 'react'

import {
  isGoalAutoResumeDismissed,
  rememberGoalAutoResumeDismissed,
} from './goalAutoResumeDismissal.js'

/* Only an open Goal continues automatically: the server never resumes a held
   (stopped) Goal, and a held Goal is the one that carries a pause_reason. */
function goalContinuesAutomatically(goal) {
  if (goal?.status === 'active') return true
  return goal?.status === 'paused' && !goal.pause_reason
}

/* `policyKnown` waits for chat info: until it loads, autoResumeEnabled reads
   false even when the policy is on. A pending limit park already offers the
   same switch on its own card, so the banner steps aside for it. */
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

  /* The copy states what enabling really does: the switch is per chat, and
     saving it also becomes the default for chats created afterwards
     (owner.auto_resume_on_limit_default in routes/chats.py). */
  return (
    <aside className="chat__goal-auto-resume" aria-label="Goal continuation">
      <div className="chat__goal-auto-resume-copy">
        <strong>Continue this chat after usage limits?</strong>
        <span>
          Möbius can retry at the provider&apos;s reset time, even while this
          device sleeps. Retries may use paid usage, and new chats will start
          with this on. Manual stops stay stopped.
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
