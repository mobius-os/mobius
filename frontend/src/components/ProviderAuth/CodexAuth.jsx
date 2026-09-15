import { useState, useEffect, useRef, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client.js'
import { authQueries } from '../../hooks/queries.js'
import { detailToMessage } from '../../lib/errorDetail.js'

const CHATGPT_SECURITY_URL = 'https://chatgpt.com/#settings/Security'
const OPENAI_DATA_CONTROLS_URL = 'https://help.openai.com/en/articles/7730893-data-controls-faq'

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
  const pollRef = useRef(null)
  // Generation counter for in-flight poll fetches. setInterval gets
  // cleared on cancel, but a request that was already awaiting a
  // response when cancel ran could still resolve after and call
  // setStatus('complete'/'failed') over the user's intended 'idle'.
  // Each startLogin bumps the gen; each poll captures it and bails
  // if it no longer matches.
  const pollGenRef = useRef(0)
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

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [])


  // On unmount, also bump the gen so any in-flight fetch (login or
  // poll) that resolves after the component is gone won't call
  // setStatus/onConnected on a dead React tree. The original cancel
  // path bumps this, but unmount-from-parent never did.
  useEffect(() => () => {
    pollGenRef.current += 1
    stopPolling()
  }, [stopPolling])

  async function copyCode(value = code) {
    if (!value) return
    try {
      await navigator.clipboard.writeText(value)
      setCopyState('copied')
    } catch {
      setCopyState('failed')
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
      authQueries.provider.statuses.markConnected(queryClient, 'codex')
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
    setError('')
    setStatus('connecting')
    setCopyState(null)
    // Capture the gen as of this call so a login that completes
    // after unmount/cancel doesn't transition the state machine.
    pollGenRef.current += 1
    const myGen = pollGenRef.current
    try {
      const res = await api.auth.provider.codex.startLogin()
      if (myGen !== pollGenRef.current) {
        return
      }
      if (!res.ok) {
        const data = await res.json()
        setError(detailToMessage(data.detail, 'Could not start Codex login.'))
        setStatus('idle')
        return
      }
      const data = await res.json()
      setUrl(data.url)
      setCode(data.code)
      setStatus('pending')

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
          Copy this one-time code, then open ChatGPT and paste it to continue.
        </p>
        <div className="codex-auth__device">
          <div className="codex-auth__step">
            <span className="codex-auth__step-num">3</span>
            <span className="codex-auth__code-copy">
              <span className="codex-auth__code-label">Copy your sign-in code</span>
              <code
                className="codex-auth__code"
                title="Click to copy"
                onClick={() => copyCode()}
              >
                {code}
              </code>
              <button
                type="button"
                className="pa__btn pa__btn--sm codex-auth__copy-btn"
                onClick={() => copyCode()}
              >
                {copyState === 'copied' ? 'Copied' : 'Copy code'}
              </button>
            </span>
          </div>
          {copyState === 'failed' && (
            <p className="pa__error codex-auth__copy-error" role="alert">
              Could not copy. Select the code above and copy it manually.
            </p>
          )}
          {copyState === 'copied' && (
            <p className="pa__muted codex-auth__copy-result" role="status">
              Code copied. Open ChatGPT, then paste it to continue.
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
            Open ChatGPT
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
        <span className="pa__success" role="status">Connected to Codex</span>
      </div>
    )
  }

  return (
    <div className="codex-auth">
      {showSetupHint && (
        <div className="codex-auth__preflight">
          <div className="codex-auth__preflight-head">
            <span className="codex-auth__step-num" aria-hidden="true">1</span>
            <div>
              <strong>Connect ChatGPT</strong>
              <p className="pa__muted codex-auth__hint">
                Open <strong>Settings → Security</strong>, scroll to the very
                bottom, and turn on
                {' '}<strong>Enable device code authorization for Codex</strong>.
                This is a one-time account setting.
              </p>
            </div>
          </div>
          <a
            className="pa__btn pa__btn--sm codex-auth__settings-link"
            href={CHATGPT_SECURITY_URL}
            target="_blank"
            rel="noopener noreferrer"
          >
            Open ChatGPT settings
          </a>
          <div className="codex-auth__privacy">
            <span className="codex-auth__step-num" aria-hidden="true">2</span>
            <div>
              <strong>Optional: disable data sharing</strong>
              <p className="pa__muted codex-auth__hint">
                If you do not want new Codex conversations used to improve
                OpenAI’s models, open <strong>Settings → Data Controls</strong>
                {' '}and turn off <strong>Improve the model for everyone</strong>.
                {' '}<a href={OPENAI_DATA_CONTROLS_URL} target="_blank" rel="noopener noreferrer">
                  OpenAI’s data-controls guide
                </a>
              </p>
            </div>
          </div>
        </div>
      )}
      <div className="codex-auth__connect-step">
        {showSetupHint && <span className="codex-auth__step-num" aria-hidden="true">3</span>}
        <div>
          {showSetupHint && <strong>Copy your sign-in code</strong>}
          <button
            className="pa__btn"
            onClick={startLogin}
            disabled={status === 'connecting'}
          >
            {status === 'connecting' ? 'Getting code…' : 'Get sign-in code'}
          </button>
        </div>
      </div>
      {error && <p className="pa__error" role="alert">{error}</p>}
    </div>
  )
}
