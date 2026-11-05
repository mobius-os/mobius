/**
 * Wiring Settings → Credits to the credits wallet of a paired Möbius.
 *
 * The wallet lives on the credits service and changes without this screen:
 * every paid app run spends from it. So the figures are read again whenever
 * the owner comes back to the window, not only when Settings opens.
 *
 * The purchase terms (currency, amounts, waiver) belong to the service this
 * Möbius is paired with. Pairing again can point it at another service, so
 * terms carry the `pairing` they were read under and are only shown, and only
 * bought with, while that is still the current pairing.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { apiFetch, jsonOrThrow } from '../../api/client.js'
import { checkoutPage, openStripeWindow, settleTopup, termsFor } from '../../lib/creditsWallet.js'

const read = async path => jsonOrThrow(await apiFetch(path))

export function useWallet({ active }) {
  const [summary, setSummary] = useState(null)
  const [readTerms, setReadTerms] = useState(null)
  const [activity, setActivity] = useState(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState(null)
  const [refusal, setRefusal] = useState(null)
  const generation = useRef(0)
  const pairing = useRef(null)

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
    const nextPairing = next?.mode === 'customer' ? next.pairing ?? null : null
    if (nextPairing !== pairing.current) {
      // Another service's history must not sit under this one's balance.
      pairing.current = nextPairing
      setActivity(null)
    }
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

  const terms = termsFor(readTerms, summary)
  const currentPairing = summary?.mode === 'customer' ? summary.pairing ?? null : null
  useEffect(() => {
    if (!active || !currentPairing || terms) return undefined
    let current = true
    read('/identity/credits/purchase-terms').then(body => {
      if (!current) return
      // Read under the pairing this screen shows: keep them. Under another
      // (paired again meanwhile): read the summary again rather than show them.
      if (body?.pairing === pairing.current) setReadTerms(body)
      else void load()
    }).catch(() => {
      // Without the service's amounts the buy control stays hidden rather
      // than offering amounts it may refuse.
    })
    return () => { current = false }
  }, [active, currentPairing, terms, load])

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
        body: JSON.stringify({
          amount_cents: amountCents,
          waiver_accepted: Boolean(waiverAccepted),
          // The server refuses if this Möbius was paired again since.
          pairing: terms?.pairing,
        }),
      }))
      const page = checkoutPage(started?.checkout_url)
      if (!page) throw new Error('Stripe did not return a page to open.')
      popup.location.href = page
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
      if (error?.detail?.code === 'pairing_changed') void load()
    } finally {
      setBusy(false)
    }
  }, [terms, load])

  return { summary, terms, activity, busy, notice, refusal, buy }
}
