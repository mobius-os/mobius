/**
 * Rules for the credits wallet a paired Möbius keeps on its credits service.
 *
 * Settings → Credits shows that wallet whenever this Möbius is paired, because
 * it is the balance apps spend from. These are the decisions that screen
 * takes, kept here so they are tested rather than eyeballed.
 */

/** Statuses after which a purchase will not change again on its own. */
const SETTLED = new Set(['paid', 'rejected', 'expired', 'needs_review'])

/**
 * Wait for a purchase to settle after Stripe's window closes.
 *
 * Reading a purchase makes the service confirm its checkout with Stripe, so a
 * few reads are enough even before the webhook lands. Resolves to the last
 * status seen; `pending` means it had not settled yet, which is not a failure.
 */
export async function settleTopup(topupId, read, { attempts = 6, waitMs = 1_500, sleep } = {}) {
  let status = 'pending'
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      status = (await read(topupId))?.status || status
    } catch {
      // A read that fails says nothing about the payment; keep looking.
    }
    if (SETTLED.has(status)) return status
    if (attempt + 1 < attempts) await sleep(waitMs)
  }
  return status
}

/**
 * One line of wallet activity, in words.
 *
 * A charge is described by the service as "Used <seller>/<app>/<action>"; the
 * action names an internal id, so the line keeps the seller and the app.
 */
export function activityLabel(entry) {
  const description = String(entry?.description || '')
  if (entry?.kind === 'app_call') {
    const match = /^Used ([^/]+)\/([^/]+)\//.exec(description)
    if (match) return `Paid @${match[1]} in ${match[2]}`
  }
  if (entry?.kind === 'topup') return 'Bought credits'
  return description || 'Credits moved'
}

/** Minor units in the currency the service sells in, e.g. 500 usd -> "$5". */
export function money(minorUnits, currency) {
  const symbol = { GBP: '£', USD: '$', EUR: '€' }[String(currency || '').toUpperCase()] || ''
  const amount = (Number(minorUnits) / 100).toFixed(2).replace(/\.00$/, '')
  return symbol ? `${symbol}${amount}` : `${amount} ${currency || ''}`.trim()
}
