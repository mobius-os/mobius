const MINUTE = 60 * 1000
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

// The last day counts down in rough steps: "~12 hrs left" until six hours
// remain, "~6 hrs left" until one hour remains, then "~1 hr left".
const LAST_DAY_STEPS = [[HOUR, '~1 hr'], [6 * HOUR, '~6 hrs'], [DAY, '~12 hrs']]

/**
 * How long an uninstalled app can still be recovered, or null once it is gone.
 *
 * `deadline` is when the recovery window closes, in epoch milliseconds on this
 * device's clock. `label` is "6 days left" … "1 day left" in whole days, then
 * the last-day steps. `tone` colours the pill: '' | 'soon' (under 3 days) |
 * 'urgent' (the last day). `changesAt` is the moment, in epoch milliseconds,
 * when the label next changes, so a view can wake exactly then instead of
 * polling.
 */
export function expiryCountdown(deadline, now = Date.now()) {
  const left = deadline - now
  if (!Number.isFinite(left) || left <= 0) return null
  const tone = left < DAY ? 'urgent' : left < 3 * DAY ? 'soon' : ''
  if (left >= DAY) {
    const days = Math.floor(left / DAY)
    return {
      label: `${days} ${days === 1 ? 'day' : 'days'} left`,
      tone,
      changesAt: now + left - days * DAY + 1,
    }
  }
  const step = LAST_DAY_STEPS.findIndex(([limit]) => left <= limit)
  const below = step === 0 ? 0 : LAST_DAY_STEPS[step - 1][0]
  return { label: `${LAST_DAY_STEPS[step][1]} left`, tone, changesAt: now + left - below }
}

/** "Uninstalled on October 27": the month written out, and the year only when it is not this year. The
 *  App Store's note on an uninstalled app's page writes its dates the same way. */
export function removedLabel(deletedAt, now = new Date()) {
  const when = new Date(deletedAt)
  if (Number.isNaN(when.getTime())) return ''
  const options = { day: 'numeric', month: 'long' }
  if (when.getFullYear() !== now.getFullYear()) options.year = 'numeric'
  return `Uninstalled on ${when.toLocaleDateString(undefined, options)}`
}

/** Why a recover or permanent delete failed: the server's reason when it gave one, else `fallback`. */
export async function failureMessage(response, fallback) {
  try {
    const body = await response.json()
    if (typeof body?.detail === 'string' && body.detail) return body.detail
  } catch {
    // fall through to the generic text
  }
  return fallback
}
