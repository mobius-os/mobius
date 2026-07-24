import { useEffect, useState } from 'react'
import mobiusLogoUrl from '../../assets/moebius.png'
import { api, setToken } from '../../api/client.js'
import * as setupSession from '../../lib/setupSession.js'
import './SetupWizard.css'

export default function SetupWizard({ onDone }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  // Clear the 401-bypass flag on any unmount — completion, nav-away,
  // tab close. The onDone path also clears it; idempotent. Without
  // this, closing the browser between account-create and onDone
  // strands `_inProgress=true` in sessionStorage for the tab's life.
  useEffect(() => {
    return () => {
      setupSession.setInProgress(false)
    }
  }, [])

  async function handleAccountSubmit(e) {
    e.preventDefault()
    setError('')
    const normalizedUsername = username.trim()
    if (!normalizedUsername) {
      setError('Enter a username.')
      return
    }
    if (normalizedUsername.length > 64) {
      setError('Username must be 64 characters or fewer.')
      return
    }
    if (!password.trim()) {
      setError('Password cannot be blank.')
      return
    }
    if (password.length > 1024) {
      setError('Password must be 1,024 characters or fewer.')
      return
    }
    if (password !== confirmPassword) {
      setError('Passwords do not match.')
      return
    }
    setLoading(true)
    try {
      const res = await api.auth.setup.create({ username: normalizedUsername, password })
      if (!res.ok) {
        const data = await res.json()
        const detail = Array.isArray(data.detail)
          ? data.detail.find((item) => typeof item?.msg === 'string')?.msg
          : data.detail
        setError(
          typeof detail === 'string'
            ? detail.replace(/^Value error,\s*/, '')
            : 'Setup failed.'
        )
        return
      }
      const data = await res.json()
      setToken(data.access_token)
      setupSession.clearResumeStep()
      setupSession.setInProgress(false)
      onDone?.()
    } catch {
      setError('Network error. Is the backend running?')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="setup">
      <div className="setup__card">
        <img src={mobiusLogoUrl} alt="Möbius" className="setup__logo" />
        <h1 className="setup__title">Set up your Möbius</h1>
        <p className="setup__subtitle">
          Choose the username and password you will use to open this Möbius.
        </p>
        <form
          className="setup__form"
          onSubmit={handleAccountSubmit}
          autoComplete="on"
        >
          <label className="setup__label">
            Username
            <input
              className="setup__input"
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              name="username"
              maxLength={64}
              required
              autoFocus
              autoComplete="username"
            />
          </label>
          <label className="setup__label">
            Password
            <input
              className="setup__input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              name="password"
              maxLength={1024}
              required
              autoComplete="new-password"
              aria-describedby="setup-password-hint"
            />
            <span className="setup__hint" id="setup-password-hint">
              Use a strong password. Long passphrases are supported.
            </span>
          </label>
          <label className="setup__label">
            Confirm password
            <input
              className="setup__input"
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              name="confirm-password"
              maxLength={1024}
              required
              autoComplete="new-password"
            />
          </label>
          {error && <p className="setup__error" role="alert">{error}</p>}
          <button
            className="setup__btn"
            type="submit"
            disabled={loading}
          >
            {loading ? 'Setting up…' : 'Enter Möbius'}
          </button>
        </form>
      </div>
    </div>
  )
}
