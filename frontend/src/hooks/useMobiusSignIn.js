/* React state for signing in to a Möbius account in place: which provider is pending, the owner-facing error, and an in-memory retry when completion could not be confirmed. */
import { useCallback, useEffect, useRef, useState } from 'react'
import { completeAccountLink, linkMobiusAccount } from '../lib/mobiusAccountLink.js'

/**
 * Returns { pending, error, canRetry, begin(provider), retry(), cancel() }.
 * `pending` is '', 'google', 'apple' or 'complete'. Call `begin` directly from
 * a click handler: the sign-in window must open inside the click. While
 * `pending === 'complete'` the account has already approved, so cancel is a
 * no-op until the local completion settles. `onSignedIn(identity)` receives
 * the confirmed linked identity even when the page has since closed, so the
 * caller can always apply it to the shared account cache
 * (mobiusAccountQueries.signedIn).
 */
export default function useMobiusSignIn({ onSignedIn, link = linkMobiusAccount, complete = completeAccountLink } = {}) {
  const [pending, setPending] = useState('')
  const [error, setError] = useState('')
  // The approved code/state/attempt, kept only in memory for one retry path.
  const [retryPayload, setRetryPayload] = useState(null)
  const busyRef = useRef(false)
  const completingRef = useRef(false)
  const abortRef = useRef(null)
  const mountedRef = useRef(true)
  const onSignedInRef = useRef(onSignedIn)
  onSignedInRef.current = onSignedIn

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      abortRef.current?.abort()
    }
  }, [])

  const settle = useCallback(outcome => {
    // The account is already linked on the server: leaving the page during
    // "Finishing sign-in…" must not drop it, or Settings shows signed out.
    if (outcome.status === 'linked') onSignedInRef.current?.(outcome.identity)
    if (!mountedRef.current) return
    if (outcome.status === 'linked') {
      setRetryPayload(null)
      setError('')
    } else {
      setRetryPayload(outcome.completion)
      setError(outcome.message)
    }
  }, [])

  const begin = useCallback(provider => {
    if (busyRef.current) return
    busyRef.current = true
    completingRef.current = false
    const controller = new AbortController()
    abortRef.current = controller
    setPending(provider)
    setError('')
    setRetryPayload(null)
    // Synchronous up to the popup: linkMobiusAccount opens it before awaiting.
    const run = link({
      provider,
      signal: controller.signal,
      onCompleting: () => {
        completingRef.current = true
        if (mountedRef.current) setPending('complete')
      },
    })
    run.then(settle, failure => {
      if (!mountedRef.current || controller.signal.aborted) return
      setError(failure?.message || 'Sign-in could not finish.')
    }).finally(() => {
      if (abortRef.current === controller) abortRef.current = null
      busyRef.current = false
      completingRef.current = false
      if (mountedRef.current) setPending('')
    })
  }, [link, settle])

  const retry = useCallback(() => {
    if (busyRef.current || !retryPayload) return
    busyRef.current = true
    completingRef.current = true
    setPending('complete')
    setError('')
    complete(retryPayload).then(settle, failure => {
      if (!mountedRef.current) return
      setRetryPayload(null)
      setError(failure?.message || 'Sign-in could not finish.')
    }).finally(() => {
      busyRef.current = false
      completingRef.current = false
      if (mountedRef.current) setPending('')
    })
  }, [complete, retryPayload, settle])

  const cancel = useCallback(() => {
    if (completingRef.current) return
    abortRef.current?.abort()
    setRetryPayload(null)
    setError('')
  }, [])

  return { pending, error, canRetry: Boolean(retryPayload), begin, retry, cancel }
}
