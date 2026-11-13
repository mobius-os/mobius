/**
 * Rules for the credits wallet a paired Möbius keeps on its credits service.
 *
 * Settings → Credits shows that wallet whenever this Möbius is paired, because
 * it is the balance apps spend from. These are the decisions that screen
 * takes, kept here so they are tested rather than eyeballed.
 */

/** Statuses after which a purchase will not change again on its own. */
const SETTLED = new Set(['paid', 'rejected', 'expired', 'needs_review'])

function pause(ms, signal) {
  return new Promise(resolve => {
    const done = () => {
      clearTimeout(timer)
      signal?.removeEventListener('abort', done)
      resolve()
    }
    const timer = setTimeout(done, ms)
    signal?.addEventListener('abort', done, { once: true })
    if (signal?.aborted) done()
  })
}


/**
 * Wait for a purchase to settle after Stripe's window closes.
 *
 * Reading a purchase makes the service confirm its checkout with Stripe, so a
 * few reads are enough even before the webhook lands. Resolves to the last
 * status seen; `pending` means it had not settled yet, which is not a failure.
 * The checkout lifecycle supplies a signal to stop reads and sleeps together.
 */
export async function settleTopup(topupId, read, {
  attempts = 6, waitMs = 1_500, sleep = pause, signal,
} = {}) {
  let status = 'pending'
  for (let attempt = 0; attempt < attempts && !signal?.aborted; attempt += 1) {
    try {
      status = (await read(topupId))?.status || status
    } catch {
      // A read that fails says nothing about the payment; keep looking.
    }
    if (signal?.aborted || SETTLED.has(status)) return status
    if (attempt + 1 < attempts) await sleep(waitMs, signal)
  }
  return status
}

/**
 * Stripe Checkout refuses to load in a frame and a new window must open inside
 * the tap, before its address is known, or the browser blocks it as a popup.
 * `noopener` would hide that window from us, so the link back to this window
 * is cut by hand: the page that loads there cannot reach or redirect Möbius.
 */
export function openStripeWindow() {
  const popup = window.open('about:blank', 'mobius-stripe-checkout', 'width=560,height=780')
  if (!popup) return null
  try {
    popup.opener = null
  } catch {
    // Never navigate a window whose link back to Möbius could not be cut.
    try { popup.close() } catch { /* The browser may deny this too. */ }
    return null
  }
  try {
    popup.document.title = 'Opening Stripe…'
    popup.document.body.innerHTML =
      '<p style="font:16px system-ui,sans-serif;padding:28px;color:#555">Opening Stripe…</p>'
  } catch {
    // A browser may forbid writing into it; the window still works.
  }
  return popup
}

/**
 * The checkout address to open, or null. The server already accepts only
 * Stripe's own page; this keeps the window, which starts on Möbius's origin,
 * from ever being sent anywhere else if that changes: only an https page on
 * Stripe Checkout's host, with no credentials or port.
 */
export function checkoutPage(address) {
  let page
  try {
    page = new URL(address)
  } catch {
    return null
  }
  const stripes = page.protocol === 'https:' && page.hostname === 'checkout.stripe.com'
    && !page.username && !page.password && !page.port
  return stripes ? page.href : null
}

/**
 * The purchase terms to show and buy with, or null.
 *
 * Terms are one credits service's: its currency, amounts and waiver rule.
 * Pairing again may point this Möbius at another service, so terms read under
 * any pairing but the summary's current one are never shown or bought with.
 */
export function termsFor(terms, summary) {
  const pairing = summary?.mode === 'customer' ? summary.pairing : null
  return pairing && terms?.pairing === pairing ? terms : null
}

/**
 * Which Credits section Settings shows: the wallet once paired, the one-tap
 * connect card for an unpaired Möbius launched from mobius.you, or none (an
 * unpaired self-hosted Möbius pairs with a key from its credits operator).
 */
export function creditsSection(summary) {
  if (summary?.mode === 'customer') return 'wallet'
  if (summary?.mode === 'unpaired' && summary.can_pair_with_account === true) return 'connect'
  return null
}

/**
 * Whether the wallet offers to reconnect: the service refuses the key this
 * Möbius holds (revoked, or retired by a later connect), and the launcher can
 * issue a new one. A key that may still work is never replaced.
 */
export function canReconnect(summary) {
  return summary?.mode === 'customer'
    && summary.can_pair_with_account === true
    && ['key_refused', 'invalid_api_key'].includes(summary.error?.code)
}

/**
 * What the account page's Credits row shows at a glance: the spendable
 * balance, a debt, or that credits are not connected yet. Null when there is
 * nothing trustworthy to show (still loading or unreadable).
 */
export function walletGlance(summary) {
  if (creditsSection(summary) === 'connect') return { kind: 'unconnected' }
  const balance = summary?.mode === 'customer' ? summary.balance : null
  if (!balance) return null
  if (typeof balance.balance === 'string' && balance.balance.startsWith('-')) {
    return { kind: 'owed', figure: balance.balance.slice(1) }
  }
  return typeof balance.available === 'string' ? { kind: 'balance', figure: balance.available } : null
}

/** The time of day a line of activity happened, for rows under a day heading. */
export function activityTime(at, locale = undefined) {
  const date = new Date(at)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' })
}

/**
 * Wallet history grouped under day headings, newest first: "Today",
 * "Yesterday", then "9 November" (with the year once it is not this year).
 * Entries keep their order; a day that continues across pages stays one group.
 */
export function activityDays(entries, now = new Date(), locale = undefined) {
  const startOf = d => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()
  const today = startOf(now)
  const days = []
  for (const entry of entries) {
    const date = new Date(entry.at)
    const key = Number.isNaN(date.getTime()) ? 'unknown' : String(startOf(date))
    let day = days[days.length - 1]
    if (!day || day.key !== key) {
      const gap = key === 'unknown' ? null : Math.round((today - Number(key)) / 86_400_000)
      const label = gap === null ? 'Earlier'
        : gap === 0 ? 'Today'
          : gap === 1 ? 'Yesterday'
            : date.toLocaleDateString(locale, {
              day: 'numeric', month: 'long',
              ...(date.getFullYear() === now.getFullYear() ? {} : { year: 'numeric' }),
            })
      day = { key, label, entries: [] }
      days.push(day)
    }
    day.entries.push(entry)
  }
  return days
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
