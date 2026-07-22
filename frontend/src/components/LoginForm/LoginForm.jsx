import { useState, useEffect } from 'react'
import mobiusLogoUrl from '../../assets/moebius.png'
import { api, setToken, BASE } from '../../api/client.js'
import './LoginForm.css'

export default function LoginForm({ onLogin }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [expired, setExpired] = useState(false)

  useEffect(() => {
    try {
      if (sessionStorage.getItem('auth_expired')) {
        sessionStorage.removeItem('auth_expired')
        setExpired(true)
      }
    } catch {}
  }, [])

  async function handleSubmit(e) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const res = await api.auth.login({ username, password })
      if (!res.ok) {
        let detail = ''
        try { detail = (await res.json())?.detail || '' } catch {}
        setError(res.status === 401
          ? 'Incorrect username or password.'
          : (detail || 'Sign-in is unavailable. Please try again.'))
        return
      }
      const data = await res.json()
      if (!data?.access_token) {
        setError('Sign-in response was incomplete. Please try again.')
        return
      }
      setToken(data.access_token)
      onLogin()
    } catch {
      setError('Network error. Is the backend running?')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login">
      <div className="login__card">
        <img src={mobiusLogoUrl} alt="Möbius" className="login__logo" />
        <h1 className="login__title">Möbius</h1>
        <p className="login__tagline">Your AI. Your apps. Your server.</p>
        {expired && (
          <p className="login__expired" role="alert">Your session expired — please log in again.</p>
        )}
        <form className="login__form" onSubmit={handleSubmit}>
          <label className="login__label">
            Username
            <input
              className="login__input"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              name="username"
              maxLength={64}
              required
              autoFocus
              autoComplete="username"
            />
          </label>
          <label className="login__label">
            Password
            <input
              className="login__input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              name="password"
              required
              autoComplete="current-password"
            />
          </label>
          {error && <p className="login__error" role="alert">{error}</p>}
          <button
            className="login__btn"
            type="submit"
            disabled={loading}
          >
            {loading ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
        <p className="login__hint">
          The{' '}
          <a href={`${BASE}/recover`} target="_blank" rel="noopener noreferrer">recovery page</a>
          {' '}lets you restore a backup or reset the app — but it requires the same password.
          If you've forgotten your password, you'll need to access the server directly to reset the database.
        </p>
      </div>
    </div>
  )
}
