import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client.js'
import { authQueries } from '../../hooks/queries.js'
import './ProviderAuth.css'

/**
 * Shared provider auth flow used by SetupWizard and SettingsView.
 *
 * Props:
 *   authenticated — current auth state, owned by the parent and read
 *                   from the same `authQueries.provider.claudeStatus`
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

  async function startAuth() {
    setError('')
    setAuthUrl('')
    setStarting(true)
    try {
      const res = await api.auth.provider.claude.startLogin()
      if (!res.ok) {
        const data = await res.json()
        setError(data.detail || 'Could not start auth.')
        return
      }
      const data = await res.json()
      setAuthUrl(data.auth_url)
      window.open(data.auth_url, '_blank')
    } catch {
      setError('Network error.')
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
      // Force a refetch via fetchQuery so we can check the new
      // status synchronously here, then invalidate so every other
      // consumer (SettingsView, SetupWizard's ProviderStep) picks
      // up the new state on its next render.
      const next = await queryClient.fetchQuery({
        queryKey: authQueries.provider.claudeStatus.key,
        queryFn: authQueries.provider.claudeStatus.fetch,
      })
      authQueries.provider.claudeStatus.invalidate(queryClient)
      if (!next?.authenticated) {
        setError('Authentication failed. Try again.')
      } else {
        setAuthUrl('')
        setAuthCode('')
        setJustConnected(true)
        setTimeout(() => setJustConnected(false), 3000)
        onDone?.()
      }
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
          A sign-in page opened. Paste the code below when done.{' '}
          Didn't open?{' '}
          <a href={authUrl} target="_blank" rel="noopener noreferrer">Click here</a>.
        </p>
        <form className="pa__form" onSubmit={submitCode}>
          <input
            className="pa__input"
            value={authCode}
            onChange={(e) => setAuthCode(e.target.value)}
            placeholder="Paste authorization code…"
            autoFocus
            autoComplete="off"
          />
          <button
            className="pa__btn"
            type="submit"
            disabled={submitting || !authCode.trim()}
          >
            {submitting ? 'Connecting…' : 'Connect'}
          </button>
        </form>
        {error && <p className="pa__error">{error}</p>}
      </div>
    )
  }

  // Connected state.
  if (authenticated) {
    if (compact) {
      return (
        <div className={`pa__row ${className}`}>
          <span className="pa__label">
            {justConnected ? <span className="pa__success">Connected</span> : 'Connected'}
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
          {justConnected ? <span className="pa__success">Connected</span> : 'Connected'}
        </p>
        {error && <p className="pa__error">{error}</p>}
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
      {error && <p className="pa__error">{error}</p>}
    </div>
  )
}
