import { useState, useEffect, useRef, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client.js'
import { settingsQueries } from '../../hooks/queries.js'
import { closeAuthWindow, navigateAuthWindow, reserveAuthWindow } from '../../utils/authWindow.js'

/**
 * Codex device-auth flow. Lifted out of SettingsView so SetupWizard
 * can reuse the same component instead of duplicating the polling
 * logic + race-safe cancellation.
 *
 * The pre-flight hint about the ChatGPT account "Enable device code
 * authorization" toggle is critical — without that toggle on, the
 * device-auth flow returns "contact your workspace admin" even on a
 * personal account, which sends users down the wrong path.
 */
export default function CodexAuth({ onConnected, showSetupHint = true }) {
  const queryClient = useQueryClient()
  const [status, setStatus] = useState('idle') // idle | connecting | pending | complete | failed
  const [url, setUrl] = useState('')
  const [code, setCode] = useState('')
  const [copyState, setCopyState] = useState(null)
  const [error, setError] = useState('')
  const [openedAuthWindow, setOpenedAuthWindow] = useState(true)
  const pollRef = useRef(null)
  const copyTimerRef = useRef(null)
  // Generation counter for in-flight poll fetches. setInterval gets
  // cleared on cancel, but a request that was already awaiting a
  // response when cancel ran could still resolve after and call
  // setStatus('complete'/'failed') over the user's intended 'idle'.
  // Each startLogin bumps the gen; each poll captures it and bails
  // if it no longer matches.
  const pollGenRef = useRef(0)
  // The sign-in tab is reserved before the auth URL exists, so every path that
  // ends without navigating it -- failed start, network error, a cancel or
  // unmount mid-fetch -- has to close it, or the owner is left staring at a
  // blank tab. Holding the handle here is what lets those later paths reach it.
  const authWindowRef = useRef(null)

  // Three callers can be checking status at once: the interval poll, a pageshow
  // after the sign-in tab hands control back, and a visibilitychange. Comparing
  // the generation is not enough on its own -- concurrent checks all captured
  // the SAME generation, so all of them pass that guard and each would run the
  // terminal transition, firing onConnected more than once. Whoever reaches a
  // terminal answer first claims it by advancing the generation, which turns
  // every other in-flight check stale.
  const claimTerminal = useCallback((pollGen) => {
    if (pollGen !== pollGenRef.current) return false
    pollGenRef.current += 1
    return true
  }, [])

  const releaseAuthWindow = useCallback(() => {
    closeAuthWindow(authWindowRef.current)
    authWindowRef.current = null
  }, [])

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [])

  const showCopyState = useCallback((nextState) => {
    setCopyState(nextState)
    if (copyTimerRef.current) clearTimeout(copyTimerRef.current)
    copyTimerRef.current = setTimeout(() => {
      setCopyState(null)
      copyTimerRef.current = null
    }, 1800)
  }, [])

  // On unmount, also bump the gen so any in-flight fetch (login or
  // poll) that resolves after the component is gone won't call
  // setStatus/onConnected on a dead React tree. The original cancel
  // path bumps this, but unmount-from-parent never did.
  useEffect(() => () => {
    pollGenRef.current += 1
    stopPolling()
    releaseAuthWindow()
    if (copyTimerRef.current) clearTimeout(copyTimerRef.current)
  }, [stopPolling, releaseAuthWindow])

  async function copyCodeToClipboard(value = code) {
    if (!value) return
    try {
      await navigator.clipboard.writeText(value)
      showCopyState('copied')
    } catch {
      showCopyState('failed')
    }
  }

  function openVerificationPage() {
    if (!url) return
    window.open(url, '_blank', 'noopener,noreferrer')
  }

  async function checkLoginStatus(pollGen) {
    const r = await api.auth.provider.codex.status()
    if (pollGen !== pollGenRef.current) return 'stale'
    if (!r.ok) {
      if (!claimTerminal(pollGen)) return 'stale'
      stopPolling()
      setStatus('failed')
      setError('Sign-in check failed. Please try again.')
      return 'failed'
    }
    const s = await r.json()
    if (pollGen !== pollGenRef.current) return 'stale'
    if (s.status === 'complete') {
      if (!claimTerminal(pollGen)) return 'stale'
      stopPolling()
      setStatus('complete')
      setUrl('')
      setCode('')
      settingsQueries.owner.invalidate(queryClient)
      onConnected?.()
      return 'complete'
    }
    if (s.status === 'failed') {
      if (!claimTerminal(pollGen)) return 'stale'
      stopPolling()
      setStatus('failed')
      setError('Login failed. Please try again.')
      return 'failed'
    }
    return s.status || 'pending'
  }

  async function startLogin() {
    const authWindow = reserveAuthWindow('Opening Codex sign-in...')
    authWindowRef.current = authWindow
    setError('')
    setStatus('connecting')
    setCopyState(null)
    setOpenedAuthWindow(!!authWindow)
    // Capture the gen as of this call so a login that completes
    // after unmount/cancel doesn't transition the state machine.
    pollGenRef.current += 1
    const myGen = pollGenRef.current
    try {
      const res = await api.auth.provider.codex.startLogin()
      if (myGen !== pollGenRef.current) {
        releaseAuthWindow()
        return
      }
      if (!res.ok) {
        const data = await res.json()
        setError(data.detail || 'Could not start Codex login.')
        setStatus('idle')
        releaseAuthWindow()
        return
      }
      const data = await res.json()
      setUrl(data.url)
      setCode(data.code)
      setStatus('pending')
      const navigated = navigateAuthWindow(authWindow, data.url)
      setOpenedAuthWindow(navigated)
      // Once navigated the tab belongs to the owner's sign-in flow, so stop
      // tracking it; if it never navigated it is a blank tab worth closing.
      if (navigated) authWindowRef.current = null
      else releaseAuthWindow()

      // Poll for completion. Bump the generation again for the poll
      // loop so cancel/unmount invalidates pending /status fetches.
      stopPolling()
      pollGenRef.current += 1
      const pollGen = pollGenRef.current
      // Cap the poll so a stuck server-side flow can't loop forever.
      // 60 attempts at 3s ≈ 3 minutes — long enough for a sleepy user
      // to finish device-code entry, short enough that a permanently
      // broken flow surfaces an error instead of polling silently.
      const maxPollAttempts = 60
      let attempts = 0
      pollRef.current = setInterval(async () => {
        attempts += 1
        try {
          const nextStatus = await checkLoginStatus(pollGen)
          if (nextStatus === 'complete' || nextStatus === 'failed' || nextStatus === 'stale') return
          if (attempts >= maxPollAttempts) {
            stopPolling()
            setStatus('failed')
            setError('Sign-in timed out. Please try again.')
          }
        } catch { /* ignore polling errors */ }
      }, 3000)
    } catch {
      setError('Network error.')
      setStatus('idle')
      releaseAuthWindow()
    }
  }

  useEffect(() => {
    if (status !== 'pending') return undefined
    const pollGen = pollGenRef.current
    const onReturn = () => {
      if (document.visibilityState !== 'visible') return
      checkLoginStatus(pollGen).catch(() => {})
    }
    window.addEventListener('pageshow', onReturn)
    document.addEventListener('visibilitychange', onReturn)
    return () => {
      window.removeEventListener('pageshow', onReturn)
      document.removeEventListener('visibilitychange', onReturn)
    }
    // checkLoginStatus reads guarded refs and stable setters; the guard above
    // intentionally registers only while the device-code flow is pending.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status])

  function cancelPending() {
    // Bump the gen so any poll request that's already mid-fetch will
    // bail when it resolves, rather than overwriting our 'idle' with
    // a stale 'complete'/'failed'.
    pollGenRef.current += 1
    stopPolling()
    releaseAuthWindow()
    setStatus('idle')
    setUrl('')
    setCode('')
    setCopyState(null)
    setError('')
  }

  if (status === 'pending') {
    return (
      <div className="codex-auth">
        <p className="pa__muted">
          Complete sign-in in your browser. {openedAuthWindow ? 'The page opened in a new tab;' : 'Open the page below;'}
          {' '}if it asks for a code, copy this one.
        </p>
        <div className="codex-auth__device">
          <div className="codex-auth__step">
            <span className="codex-auth__step-num">1</span>
            <span>
              Open{' '}
              <a href={url} target="_blank" rel="noopener noreferrer">
                verification page
              </a>
            </span>
          </div>
          <div className="codex-auth__step">
            <span className="codex-auth__step-num">2</span>
            <span className="codex-auth__code-copy">
              <span className="codex-auth__code-label">Enter code</span>
              <code
                className="codex-auth__code"
                title="Click to copy"
                onClick={() => copyCodeToClipboard()}
              >
                {code}
              </code>
              <button
                type="button"
                className="pa__btn pa__btn--sm codex-auth__copy-btn"
                onClick={() => copyCodeToClipboard()}
              >
                {copyState === 'copied' ? 'Copied' : 'Copy code'}
              </button>
            </span>
          </div>
          {copyState === 'failed' && (
            <p className="pa__error codex-auth__copy-error">
              Could not copy. Select the code above.
            </p>
          )}
        </div>
        <div className="codex-auth__pending-actions">
          <p className="pa__muted codex-auth__waiting">
            Waiting for sign-in to complete…
          </p>
          <button
            type="button"
            className="pa__btn pa__btn--sm"
            onClick={openVerificationPage}
          >
            Open page
          </button>
          <button
            type="button"
            className="pa__btn pa__btn--sm"
            onClick={cancelPending}
          >
            Cancel
          </button>
        </div>
      </div>
    )
  }

  if (status === 'complete') {
    return (
      <div className="codex-auth">
        <span className="pa__success">Connected to Codex</span>
      </div>
    )
  }

  return (
    <div className="codex-auth">
      {showSetupHint && (
        <p className="pa__muted codex-auth__hint">
          First time? In your ChatGPT account, open
          {' '}<strong>Settings → Security</strong> and turn on
          {' '}<strong>Enable device code authorization for Codex</strong>.
          Without it, sign-in below will fail with a "contact your
          workspace admin" message.
        </p>
      )}
      <button
        className="pa__btn"
        onClick={startLogin}
        disabled={status === 'connecting'}
      >
        {status === 'connecting' ? 'Starting…' : 'Connect to Codex'}
      </button>
      {error && <p className="pa__error">{error}</p>}
    </div>
  )
}
