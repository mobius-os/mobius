/**
 * Wiring Settings → Credits to the credits wallet of a paired Möbius.
 *
 * The wallet lives on the credits service and changes without this screen:
 * every paid app run spends from it. So the figures are read again whenever
 * the owner comes back to the window, not only when Settings opens.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { apiFetch, jsonOrThrow } from '../../api/client.js'
import { settleTopup } from '../../lib/creditsWallet.js'

const read = async path => jsonOrThrow(await apiFetch(path))

/**
 * Stripe Checkout refuses to load in a frame and a new window must open inside
 * the tap, before its address is known, or the browser blocks it as a popup.
 */
function openStripeWindow() {
  const popup = window.open('about:blank', 'mobius-stripe-checkout', 'width=560,height=780')
  try {
    popup.document.title = 'Opening Stripe…'
    popup.document.body.innerHTML =
      '<p style="font:16px system-ui,sans-serif;padding:28px;color:#555">Opening Stripe…</p>'
  } catch {
    // A browser may forbid writing into it; the window still works.
  }
  return popup
}

export function useWallet({ active }) {
  const [summary, setSummary] = useState(null)
  const [terms, setTerms] = useState(null)
  const [activity, setActivity] = useState(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState(null)
  const [refusal, setRefusal] = useState(null)
  const generation = useRef(0)

  const load = useCallback(async () => {
    const mine = (generation.current += 1)
    let next
    try {
      next = await read('/identity/credits')
    } catch {
      // Unreadable: Settings then leaves the section out.
      next = { mode: 'unknown' }
    }
    if (mine !== generation.current) return
    setSummary(next)
    if (next?.mode !== 'customer') return
    try {
      const body = await read('/identity/credits/transactions?limit=8')
      if (mine === generation.current) setActivity(body?.transactions || [])
    } catch {
      if (mine === generation.current) setActivity(null)
    }
  }, [])

  useEffect(() => {
    if (!active) return undefined
    void load()
    const again = () => { if (document.visibilityState !== 'hidden') void load() }
    window.addEventListener('focus', again)
    document.addEventListener('visibilitychange', again)
    return () => {
      window.removeEventListener('focus', again)
      document.removeEventListener('visibilitychange', again)
    }
  }, [active, load])

  const paired = summary?.mode === 'customer'
  useEffect(() => {
    if (!active || !paired || terms) return
    read('/identity/credits/purchase-terms').then(setTerms).catch(() => {
      // Without the service's amounts the buy control stays hidden rather
      // than offering amounts it may refuse.
    })
  }, [active, paired, terms])

  const buy = useCallback(async ({ amountCents, waiverAccepted }) => {
    setRefusal(null)
    setNotice(null)
    if (terms?.waiver_required && !waiverAccepted) {
      setRefusal('Please accept the cancellation terms first.')
      return
    }
    const popup = openStripeWindow()
    if (!popup) {
      setRefusal('Your browser blocked the Stripe window. Allow popups for this Möbius, then try again.')
      return
    }
    setBusy(true)
    try {
      const started = await jsonOrThrow(await apiFetch('/identity/credits/topup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ amount_cents: amountCents, waiver_accepted: Boolean(waiverAccepted) }),
      }))
      if (!started?.checkout_url) throw new Error('Stripe did not return a page to open.')
      popup.location.href = started.checkout_url
      await new Promise(resolve => {
        const watch = setInterval(() => {
          if (popup.closed) { clearInterval(watch); resolve() }
        }, 800)
      })
      setNotice('checking')
      const status = await settleTopup(
        started.topup_id,
        id => read(`/identity/credits/topups/${encodeURIComponent(id)}`),
        { sleep: ms => new Promise(resolve => setTimeout(resolve, ms)) },
      )
      // A purchase held for review was paid but not yet credited: same advice
      // as a slow one. A cancelled checkout charged nothing, so say nothing.
      setNotice(status === 'paid' ? 'paid'
        : status === 'pending' || status === 'needs_review' ? 'late' : null)
      await load()
    } catch (error) {
      popup.close()
      setRefusal(error?.message || 'That purchase could not be started.')
    } finally {
      setBusy(false)
    }
  }, [terms, load])

  return { summary, terms, activity, busy, notice, refusal, buy }
}
