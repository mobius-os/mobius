import { useEffect, useId, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client.js'
import { authQueries } from '../../hooks/queries.js'
import { closeAuthWindow, navigateAuthWindow, reserveAuthWindow } from '../../utils/authWindow.js'
import './ProviderAuth.css'

/**
 * Shared provider auth flow used by SetupWizard and SettingsView.
 *
 * Props:
 *   authenticated — current auth state, owned by the parent and read
 *                   from the canonical `authQueries.provider.statuses`
 *                   query both consumers already use. Passing it down
 *                   instead of re-running the query here keeps the
 *                   "is Claude connected?" fact in one place per
 *                   render tree.
 *   onDone        — called after successful auth (optional)
 *   compact       — if true, renders inline status row instead of full card
 *   className     — additional class on the wrapper (optional)
 */
export default function ProviderAuth({ authenticated, onDone, compact = false, className = '' }) {
  const queryClient = useQueryClient()
  const [authUrl, setAuthUrl] = useState('')
  const [authCode, setAuthCode] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [starting, setStarting] = useState(false)
  const [justConnected, setJustConnected] = useState(false)
  const [openedAuthWindow, setOpenedAuthWindow] = useState(true)
  const authCodeId = useId()
  // The sign-in tab is reserved before the auth URL exists, so any path that
  // ends without navigating it has to close it or the owner keeps a blank tab.
  const authWindowRef = useRef(null)

  function releaseAuthWindow() {
    closeAuthWindow(authWindowRef.current)
    authWindowRef.current = null
  }

  useEffect(() => () => {
    closeAuthWindow(authWindowRef.current)
    authWindowRef.current = null
  }, [])

  async function startAuth() {
    const authWindow = reserveAuthWindow('Opening Claude sign-in...')
    authWindowRef.current = authWindow
    setError('')
    setAuthUrl('')
    setOpenedAuthWindow(!!authWindow)
    setStarting(true)
    try {
      const res = await api.auth.provider.claude.startLogin()
      if (!res.ok) {
        const data = await res.json()
        setError(data.detail || 'Could not start auth.')
        releaseAuthWindow()
        return
      }
      const data = await res.json()
      setAuthUrl(data.auth_url)
      const navigated = navigateAuthWindow(authWindow, data.auth_url)
      setOpenedAuthWindow(navigated)
      // A navigated tab is the owner's sign-in page and no longer ours; one we
      // could not navigate is a blank tab, so close it.
      if (navigated) authWindowRef.current = null
      else releaseAuthWindow()
    } catch {
      setError('Network error.')
      releaseAuthWindow()
    } finally {
      setStarting(false)
    }
  }

  async function submitCode(e) {
    e.preventDefault()
    if (!authCode.trim()) return
    setError('')
    setSubmitting(true)
    try {
      const res = await api.auth.provider.claude.submitCode(authCode.trim())
      if (!res.ok) {
        const data = await res.json()
        setError(data.detail || 'Failed to submit code.')
        return
      }
      // A 200 from /provider/code is authoritative: the backend only returns
      // after credentials have been written. Update the shared cache now so a
      // still-fresh persisted "not connected" value cannot reject a valid,
      // one-shot authorization code. Revalidate in the background afterward.
      authQueries.provider.statuses.markConnected(queryClient, 'claude')
      setAuthUrl('')
      setAuthCode('')
      setJustConnected(true)
      setTimeout(() => setJustConnected(false), 3000)
      onDone?.()
    } catch {
      setError('Network error.')
    } finally {
      setSubmitting(false)
    }
  }

  // The "Checking…" placeholder formerly rendered here while the
  // local statusQuery was loading. After consolidation, the parent
  // owns the query and is expected to gate the render itself (see
  // SettingsView's `providerLoaded` guard). Removing the local
  // gate avoids a render with `authenticated === undefined` from
  // showing the "Not connected" state for one frame.

  // Active auth flow — always show the code input when authUrl is set.
  if (authUrl) {
    return (
      <div className={`pa__flow ${className}`}>
        <p className="pa__muted">
          {openedAuthWindow
            ? 'A sign-in page opened. Paste the code below when done.'
            : 'Your browser blocked the sign-in tab. Paste the code below when done.'}
          {' '}
          <a href={authUrl} target="_blank" rel="noopener noreferrer">Open the sign-in page</a>.
        </p>
        <form className="pa__form" onSubmit={submitCode}>
          <label className="pa__field" htmlFor={authCodeId}>
            <span className="pa__input-label">Authorization code</span>
            <input
              id={authCodeId}
              name="authorization-code"
              className="pa__input"
              value={authCode}
              onChange={(e) => setAuthCode(e.target.value)}
              placeholder="Paste authorization code…"
              autoFocus
              autoComplete="off"
              spellCheck={false}
            />
          </label>
          <button
            className="pa__btn"
            type="submit"
            disabled={submitting || !authCode.trim()}
          >
            {submitting ? 'Connecting…' : 'Connect'}
          </button>
        </form>
        {error && <p className="pa__error" role="alert">{error}</p>}
      </div>
    )
  }

  // Connected state.
  if (authenticated) {
    if (compact) {
      return (
        <div className={`pa__row ${className}`}>
          <span className="pa__label">
            {justConnected ? <span className="pa__success" role="status">Connected</span> : 'Connected'}
          </span>
          <button className="pa__btn pa__btn--sm" onClick={startAuth} disabled={starting}>
            Reconnect
          </button>
        </div>
      )
    }
    return (
      <div className={`pa__done ${className}`}>
        <p className="pa__label">
          {justConnected ? <span className="pa__success" role="status">Connected</span> : 'Connected'}
        </p>
        {error && <p className="pa__error" role="alert">{error}</p>}
      </div>
    )
  }

  // Not connected.
  return (
    <div className={`pa__flow ${className}`}>
      {compact && (
        <p className="pa__muted">Not connected. Sign in to use the agent.</p>
      )}
      <button className="pa__btn" onClick={startAuth} disabled={starting}>
        {starting ? 'Starting…' : 'Sign in with Claude'}
      </button>
      {error && <p className="pa__error" role="alert">{error}</p>}
    </div>
  )
}
