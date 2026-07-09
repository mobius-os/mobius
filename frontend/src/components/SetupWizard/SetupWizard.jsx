import { useState, useEffect } from 'react'
import { api, setToken, BASE } from '../../api/client.js'
import * as setupSession from '../../lib/setupSession.js'
import { authQueries, settingsQueries } from '../../hooks/queries.js'
import ProviderAuth from '../ProviderAuth/ProviderAuth.jsx'
import CodexAuth from '../ProviderAuth/CodexAuth.jsx'
import ProviderRow from '../ProviderAuth/ProviderRow.jsx'
import './SetupWizard.css'

const SETUP_STEPS = [
  { id: 'account', label: 'Account' },
  { id: 'provider', label: 'AI' },
  { id: 'gemini', label: 'Images' },
]

export default function SetupWizard({ onDone, initialStep = 'account' }) {
  const [step, setStep] = useState(initialStep)
  // Hoisted from ProviderStep so ProviderAuth (which is a
  // grandchild) doesn't need to re-run the same query. Gated on
  // step !== 'account': there's no token yet during account
  // creation, so an eager fetch here 401s, and apiFetch's global
  // 401 handler (client.js) treats that as an expired session and
  // reloads the page — an infinite reload loop on first load.
  const claudeStatusQuery = authQueries.provider.claudeStatus.useQuery({
    enabled: step !== 'account',
  })
  const claudeAuthenticated = !!claudeStatusQuery.data?.authenticated

  // Persists step synchronously alongside setStep so a refresh in
  // the microsecond gap between setStep and a useEffect flush can't
  // lose the resume position. saveStep is a no-op for 'account'
  // because there's no token yet — see setupSession.js.
  function goToStep(next) {
    setupSession.saveStep(next)
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
      setupSession.setInProgress(true)
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
    return (
      <ProviderStep
        onSkip={() => goToStep('gemini')}
        onConnected={() => goToStep('gemini')}
        claudeAuthenticated={claudeAuthenticated}
      />
    )
  }

  if (step === 'gemini') {
    return (
      <div className="setup">
        <div className="setup__card">
          <img src={`${BASE}/moebius.png`} alt="Möbius" className="setup__logo" />
          <SetupProgress step="gemini" />
          <h1 className="setup__title">Image generation (optional)</h1>
          <p className="setup__subtitle">
            Add a Gemini API key only if you want image generation in chat. Get
            one at{' '}
            <a href="https://aistudio.google.com" target="_blank" rel="noopener noreferrer">
              aistudio.google.com
            </a>
            .
          </p>
          <form
            className="setup__form"
            onSubmit={(e) => { e.preventDefault(); handleGeminiSave() }}
          >
            <label className="setup__label">
              Gemini API key
              <input
                className="setup__input"
                type="password"
                placeholder="AIza..."
                value={geminiKey}
                onChange={(e) => { setGeminiKey(e.target.value); setGeminiError('') }}
                autoComplete="off"
              />
            </label>
            <p className="setup__hint">
              Your key is encrypted at rest and never leaves the server.
            </p>
            {geminiError && <p className="setup__error">{geminiError}</p>}
            <button
              type="submit"
              className="setup__btn"
              disabled={geminiSaving}
            >
              {geminiSaving ? 'Saving…' : 'Save & continue'}
            </button>
            <button
              type="button"
              className="setup__skip"
              onClick={onDone}
            >
              Skip for now
            </button>
          </form>
        </div>
      </div>
    )
  }

  return (
    <div className="setup">
      <div className="setup__card">
        <img src={`${BASE}/moebius.png`} alt="Möbius" className="setup__logo" />
        <SetupProgress step="account" />
        <h1 className="setup__title">Welcome to Möbius</h1>
        <p className="setup__subtitle">
          Create the owner account for this install. You will connect an AI
          provider next.
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
function ProviderStep({ onSkip, onConnected, claudeAuthenticated }) {
  const [expanded, setExpanded] = useState('codex')
  const settingsQuery = settingsQueries.owner.useQuery()
  const codexConnected = !!settingsQuery.data?.codex_authenticated
  const claudeConnected = !!claudeAuthenticated

  function toggle(id) {
    setExpanded(prev => prev === id ? null : id)
  }

  return (
    <div className="setup">
      <div className="setup__card">
        <img src={`${BASE}/moebius.png`} alt="Möbius" className="setup__logo" />
        <SetupProgress step="provider" />
        <h1 className="setup__title">Connect your AI</h1>
        <p className="setup__subtitle">
          Connect Codex or Claude so chats can run. You can add the other later
          in Settings.
        </p>

        <div className="settings__providers">
          <ProviderRow
            id="codex"
            name="OpenAI Codex"
            badge="Free ChatGPT account"
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
            <ProviderAuth
              authenticated={claudeAuthenticated}
              compact
              onDone={onConnected}
            />
          </ProviderRow>
        </div>

        <p className="setup__skip-warn">
          Without a provider, all chat messages will fail.
        </p>
        <button className="setup__skip" type="button" onClick={onSkip}>
          Skip for now
        </button>
      </div>
    </div>
  )
}

function SetupProgress({ step }) {
  const currentIndex = SETUP_STEPS.findIndex(item => item.id === step)
  const safeIndex = currentIndex >= 0 ? currentIndex : 0
  const currentStep = SETUP_STEPS[safeIndex]

  return (
    <div
      className="setup__progress"
      aria-label={`Step ${safeIndex + 1} of ${SETUP_STEPS.length}: ${currentStep.label}`}
    >
      <span className="setup__progress-text">
        Step {safeIndex + 1} of {SETUP_STEPS.length}
      </span>
      <span className="setup__progress-track" aria-hidden="true">
        {SETUP_STEPS.map((item, index) => (
          <span
            key={item.id}
            className={`setup__progress-dot${index <= safeIndex ? ' setup__progress-dot--active' : ''}`}
          />
        ))}
      </span>
    </div>
  )
}
