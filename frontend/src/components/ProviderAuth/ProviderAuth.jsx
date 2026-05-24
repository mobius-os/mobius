import { useState, useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client.js'
import { authQueries } from '../../hooks/queries.js'
import './ProviderAuth.css'

/**
 * Shared provider auth flow used by SetupWizard and SettingsView.
 *
 * Props:
 *   onDone    — called after successful auth (optional)
 *   compact   — if true, renders inline status row instead of full card
 *   className — additional class on the wrapper (optional)
 */
export default function ProviderAuth({ onDone, compact = false, className = '' }) {
  const queryClient = useQueryClient()
  const statusQuery = authQueries.provider.claudeStatus.useQuery()
  const [authUrl, setAuthUrl] = useState('')
  const [authCode, setAuthCode] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [starting, setStarting] = useState(false)
  const [justConnected, setJustConnected] = useState(false)
  const authenticated = !!statusQuery.data?.authenticated
  const loading = statusQuery.isLoading

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
      authQueries.provider.claudeStatus.invalidate(queryClient)
      const next = await statusQuery.refetch()
      if (!next.data?.authenticated) {
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

  if (loading) {
    return <p className="pa__muted">Checking…</p>
  }

  // Active auth flow — always show the code input when authUrl is set.
  if (authUrl) {
    return (
      <div className={`pa__flow ${className}`}>
        <p className="pa__muted">
          A sign-in page should have opened.{' '}
          <a href={authUrl} target="_blank" rel="noopener noreferrer">Click here</a>{' '}
          if it didn't. Paste the code below.
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
        <p className="pa__muted">Not connected. Sign in to enable the AI agent.</p>
      )}
      <button className="pa__btn" onClick={startAuth} disabled={starting}>
        {starting ? 'Starting…' : 'Sign in with Claude'}
      </button>
      {error && <p className="pa__error">{error}</p>}
    </div>
  )
}
