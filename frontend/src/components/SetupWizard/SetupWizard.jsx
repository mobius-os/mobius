import { useState } from 'react'
import { api, setToken, setSetupInProgress, BASE } from '../../api/client.js'
import { authQueries, settingsQueries } from '../../hooks/queries.js'
import ProviderAuth from '../ProviderAuth/ProviderAuth.jsx'
import CodexAuth from '../ProviderAuth/CodexAuth.jsx'
import ProviderRow from '../ProviderAuth/ProviderRow.jsx'
import './SetupWizard.css'

// localStorage key for resuming setup mid-wizard. If the user creates
// an account but closes the tab before finishing the provider /
// Gemini steps, the next visit would otherwise land them in Shell
// with no AI configured — silently broken. AppRoot reads this key
// and routes back into the wizard at the right step.
const SETUP_STEP_KEY = 'setup-step'

export default function SetupWizard({ onDone, initialStep = 'account' }) {
  const [step, setStep] = useState(initialStep)

  // Writes to localStorage synchronously alongside setStep so a refresh
  // in the microsecond gap between setStep and a useEffect flush can't
  // lose the resume position. Only persist past 'account' — that step
  // has no token yet, so there's nothing to resume to.
  function goToStep(next) {
    if (next !== 'account') {
      try { localStorage.setItem(SETUP_STEP_KEY, next) } catch {}
    }
    setStep(next)
  }
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
      const res = await api.auth.setup.create({ username, password })
      if (!res.ok) {
        const data = await res.json()
        setError(data.detail || 'Setup failed.')
        return
      }
      const data = await res.json()
      setToken(data.access_token)
      setSetupInProgress(true)
      goToStep('provider')
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
      const res = await api.settings.save({ gemini_api_key: geminiKey.trim() })
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
    return <ProviderStep onSkip={() => goToStep('gemini')} onConnected={() => goToStep('gemini')} />
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
            {geminiSaving ? 'Saving…' : 'Save & continue'}
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

/**
 * Connect-AI step: mirrors the Settings page's provider list but
 * with no default-selection radio. Each provider is a row that
 * expands to its auth panel on tap. Codex is listed first because
 * it works on a free ChatGPT account; an inline hint about the
 * ChatGPT-account device-auth toggle lives inside CodexAuth. Either
 * provider connecting advances the wizard.
 */
function ProviderStep({ onSkip, onConnected }) {
  const [expanded, setExpanded] = useState('codex')
  const claudeStatusQuery = authQueries.provider.claudeStatus.useQuery()
  const settingsQuery = settingsQueries.owner.useQuery()
  const codexConnected = !!settingsQuery.data?.codex_authenticated
  const claudeConnected = !!claudeStatusQuery.data?.authenticated

  function toggle(id) {
    setExpanded(prev => prev === id ? null : id)
  }

  return (
    <div className="setup">
      <div className="setup__card">
        <img src={`${BASE}/moebius.png`} alt="Möbius" className="setup__logo" />
        <h1 className="setup__title">Connect your AI</h1>
        <p className="setup__subtitle">
          Pick a provider to connect. You can switch or connect the
          other one later in Settings.
        </p>

        <div className="settings__providers">
          <ProviderRow
            id="codex"
            name="OpenAI Codex"
            badge="Free account, limited usage"
            connected={codexConnected}
            showRadio={false}
            expanded={expanded === 'codex'}
            onToggleExpand={() => toggle('codex')}
          >
            <CodexAuth onConnected={onConnected} />
          </ProviderRow>

          <ProviderRow
            id="claude"
            name="Claude Code"
            connected={claudeConnected}
            showRadio={false}
            expanded={expanded === 'claude'}
            onToggleExpand={() => toggle('claude')}
          >
            <ProviderAuth compact onDone={onConnected} />
          </ProviderRow>
        </div>

        <p className="setup__skip-warn">
          Skipping means the AI agent won't work — all chat messages
          will fail until you connect a provider.
        </p>
        <button className="setup__skip" onClick={onSkip}>
          Skip for now
        </button>
      </div>
    </div>
  )
}
