import { useState, useEffect } from 'react'
import { apiFetch, setToken, setSetupInProgress, BASE } from '../../api/client.js'
import ProviderAuth from '../ProviderAuth/ProviderAuth.jsx'
import './SetupWizard.css'

// localStorage key for resuming setup mid-wizard. If the user creates
// an account but closes the tab before finishing the provider /
// Gemini steps, the next visit would otherwise land them in Shell
// with no AI configured — silently broken. AppRoot reads this key
// and routes back into the wizard at the right step.
const SETUP_STEP_KEY = 'setup-step'

export default function SetupWizard({ onDone, initialStep = 'account' }) {
  const [step, setStep] = useState(initialStep)

  // Persist + restore step. Only persist past 'account' — the account
  // step has no token yet, so there's nothing to resume to. Clear on
  // unmount via onDone (handled by the consumer in App.jsx).
  useEffect(() => {
    if (step === 'account') return
    try { localStorage.setItem(SETUP_STEP_KEY, step) } catch {}
  }, [step])
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  // Gemini step state.
  const [geminiKey, setGeminiKey] = useState('')
  const [geminiError, setGeminiError] = useState('')
  const [geminiSaving, setGeminiSaving] = useState(false)

  async function handleAccountSubmit(e) {
    e.preventDefault()
    setError('')
    if (password !== confirmPassword) {
      setError('Passwords do not match.')
      return
    }
    setLoading(true)
    try {
      const res = await apiFetch('/auth/setup', {
        method: 'POST',
        body: JSON.stringify({ username, password }),
      })
      if (!res.ok) {
        const data = await res.json()
        setError(data.detail || 'Setup failed.')
        return
      }
      const data = await res.json()
      setToken(data.access_token)
      setSetupInProgress(true)
      setStep('provider')
    } catch {
      setError('Network error. Is the backend running?')
    } finally {
      setLoading(false)
    }
  }

  async function handleGeminiSave() {
    setGeminiError('')
    if (!geminiKey.trim()) {
      onDone()
      return
    }
    setGeminiSaving(true)
    try {
      const res = await apiFetch('/settings', {
        method: 'POST',
        body: JSON.stringify({ gemini_api_key: geminiKey.trim() }),
      })
      if (!res.ok) {
        const data = await res.json()
        setGeminiError(data.detail || 'Failed to save key.')
        return
      }
      onDone()
    } catch {
      setGeminiError('Network error.')
    } finally {
      setGeminiSaving(false)
    }
  }

  if (step === 'provider') {
    return (
      <div className="setup">
        <div className="setup__card">
          <img src={`${BASE}/moebius.png`} alt="Möbius" className="setup__logo" />
          <h1 className="setup__title">Connect your AI</h1>
          <p className="setup__subtitle">
            Sign in with your Claude account. Requires a Pro or Max subscription.
            No API key needed.
          </p>

          <ProviderAuth onDone={() => setStep('gemini')} />

          <p className="setup__skip-warn">
            Skipping means the AI agent won't work — all chat messages will fail until you sign in with Claude.
          </p>
          <button
            className="setup__skip"
            onClick={() => setStep('gemini')}
          >
            Skip for now
          </button>
        </div>
      </div>
    )
  }

  if (step === 'gemini') {
    return (
      <div className="setup">
        <div className="setup__card">
          <img src={`${BASE}/moebius.png`} alt="Möbius" className="setup__logo" />
          <h1 className="setup__title">Image generation (optional)</h1>
          <p className="setup__subtitle">
            Generate images in chat using Google's Gemini AI.
          </p>
          <p className="setup__subtitle">
            Get an API key at{' '}
            <a href="https://aistudio.google.com" target="_blank" rel="noopener noreferrer">
              aistudio.google.com
            </a>
          </p>
          <input
            className="setup__input"
            type="password"
            placeholder="AIza..."
            value={geminiKey}
            onChange={(e) => { setGeminiKey(e.target.value); setGeminiError('') }}
            autoComplete="off"
          />
          <p className="setup__hint">
            Your key is encrypted before storage and is never sent to the browser.
          </p>
          {geminiError && <p className="setup__error">{geminiError}</p>}
          <button className="setup__btn" onClick={handleGeminiSave} disabled={geminiSaving}>
            {geminiSaving ? 'Saving\u2026' : 'Save & continue'}
          </button>
          <button className="setup__skip" onClick={onDone}>
            Skip for now
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="setup">
      <div className="setup__card">
        <img src={`${BASE}/moebius.png`} alt="Möbius" className="setup__logo" />
        <h1 className="setup__title">Welcome to Möbius</h1>
        <p className="setup__subtitle">
          Create an account to get started.
        </p>
        <form className="setup__form" onSubmit={handleAccountSubmit}>
          <label className="setup__label">
            Username
            <input
              className="setup__input"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
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
              required
              autoComplete="new-password"
            />
          </label>
          <label className="setup__label">
            Confirm password
            <input
              className="setup__input"
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              required
              autoComplete="new-password"
            />
          </label>
          {error && <p className="setup__error">{error}</p>}
          <button
            className="setup__btn"
            type="submit"
            disabled={loading}
          >
            {loading ? 'Setting up…' : 'Continue'}
          </button>
        </form>
      </div>
    </div>
  )
}
