import { useState, useEffect, useRef, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client.js'
import { settingsQueries } from '../../hooks/queries.js'

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
  const [error, setError] = useState('')
  const pollRef = useRef(null)
  // Generation counter for in-flight poll fetches. setInterval gets
  // cleared on cancel, but a request that was already awaiting a
  // response when cancel ran could still resolve after and call
  // setStatus('complete'/'failed') over the user's intended 'idle'.
  // Each startLogin bumps the gen; each poll captures it and bails
  // if it no longer matches.
  const pollGenRef = useRef(0)

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

  async function startLogin() {
    setError('')
    setStatus('connecting')
    // Capture the gen as of this call so a login that completes
    // after unmount/cancel doesn't transition the state machine.
    pollGenRef.current += 1
    const myGen = pollGenRef.current
    try {
      const res = await api.auth.provider.codex.startLogin()
      if (myGen !== pollGenRef.current) return
      if (!res.ok) {
        const data = await res.json()
        setError(data.detail || 'Could not start Codex login.')
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
          const r = await api.auth.provider.codex.status()
          if (pollGen !== pollGenRef.current) return
          // Surface non-OK responses instead of trying to parse them.
          // A 401 here would otherwise trip the global apiFetch handler
          // (which reloads to login), restart the poll on remount, and
          // loop. Bail out cleanly and let the user retry.
          if (!r.ok) {
            stopPolling()
            setStatus('failed')
            setError('Sign-in check failed. Please try again.')
            return
          }
          const s = await r.json()
          if (pollGen !== pollGenRef.current) return
          if (s.status === 'complete') {
            stopPolling()
            setStatus('complete')
            setUrl('')
            setCode('')
            settingsQueries.owner.invalidate(queryClient)
            onConnected?.()
          } else if (s.status === 'failed') {
            stopPolling()
            setStatus('failed')
            setError('Login failed. Please try again.')
          } else if (attempts >= maxPollAttempts) {
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

  function cancelPending() {
    // Bump the gen so any poll request that's already mid-fetch will
    // bail when it resolves, rather than overwriting our 'idle' with
    // a stale 'complete'/'failed'.
    pollGenRef.current += 1
    stopPolling()
    setStatus('idle')
    setUrl('')
    setCode('')
    setError('')
  }

  if (status === 'pending') {
    return (
      <div className="codex-auth">
        <p className="pa__muted">
          Complete sign-in in your browser:
        </p>
        <div className="codex-auth__device">
          <div className="codex-auth__step">
            <span className="codex-auth__step-num">1</span>
            <span>Open <a href={url} target="_blank" rel="noopener noreferrer">{url}</a></span>
          </div>
          <div className="codex-auth__step">
            <span className="codex-auth__step-num">2</span>
            <span>Enter code: <strong className="codex-auth__code">{code}</strong></span>
          </div>
        </div>
        <div className="codex-auth__pending-actions">
          <p className="pa__muted codex-auth__waiting">
            Waiting for sign-in to complete…
          </p>
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
