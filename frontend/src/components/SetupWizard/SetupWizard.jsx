import { useState, useEffect, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'
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
]
const PROVIDERS = [
  { id: 'codex', label: 'OpenAI Codex' },
  { id: 'claude', label: 'Claude Code' },
]

export default function SetupWizard({ onDone, initialStep = 'account', claimRequired = false }) {
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
  // First-boot claim code — the one-time setup token from the deploy logs
  // (or the MOBIUS_SETUP_CLAIM the deployer preset). Only collected when the
  // backend reports the gate is open.
  const [claim, setClaim] = useState('')
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
    if (password !== confirmPassword) {
      setError('Passwords do not match.')
      return
    }
    setLoading(true)
    try {
      const res = await api.auth.setup.create({ username, password, claim })
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

  if (step === 'provider') {
    return (
      <ProviderStep
        onSkip={onDone}
        onContinue={onDone}
        claudeAuthenticated={claudeAuthenticated}
      />
    )
  }

  return (
    <div className="setup">
      <div className="setup__card">
        <img src={`${BASE}/moebius.png`} alt="Möbius" className="setup__logo" />
        <SetupProgress step="account" />
        <h1 className="setup__title">Create your home key</h1>
        <p className="setup__subtitle">
          This account unlocks your private Möbius. Your agent, apps, and data
          live in the container you just made.
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
          {claimRequired && (
            <label className="setup__label">
              Setup code
              <input
                className="setup__input"
                value={claim}
                onChange={(e) => setClaim(e.target.value)}
                required
                autoComplete="off"
                spellCheck={false}
              />
              <span className="setup__hint">
                Find this in your deploy logs (or the MOBIUS_SETUP_CLAIM you set).
              </span>
            </label>
          )}
          {error && <p className="setup__error" role="alert">{error}</p>}
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
 * with no default-selection radio. Each provider has one explicit
 * action that expands its auth panel. Codex is listed first because
 * it works on a free ChatGPT account; an inline hint about the
 * ChatGPT-account device-auth toggle lives inside CodexAuth. Either
 * provider connecting advances the wizard.
 */
function ProviderStep({ onSkip, onContinue, claudeAuthenticated }) {
  const queryClient = useQueryClient()
  const [expanded, setExpanded] = useState('codex')
  const settingsQuery = settingsQueries.owner.useQuery()
  const codexConnected = !!settingsQuery.data?.codex_authenticated
  const claudeConnected = !!claudeAuthenticated
  const connectedAny = codexConnected || claudeConnected
  const [agentSaving, setAgentSaving] = useState(false)
  const [agentSaved, setAgentSaved] = useState(false)
  const [agentError, setAgentError] = useState('')
  const [agentProvider, setAgentProvider] = useState('')

  function toggle(id) {
    setExpanded(prev => prev === id ? null : id)
  }

  function connectedMap(extraProvider = '') {
    return {
      codex: codexConnected || extraProvider === 'codex',
      claude: claudeConnected || extraProvider === 'claude',
    }
  }

  function firstConnectedProvider(extraProvider = '') {
    const connected = connectedMap(extraProvider)
    return PROVIDERS.find(provider => connected[provider.id])?.id || ''
  }

  const saveConnectedProvider = useCallback(async (provider) => {
    if (!provider) return false
    setAgentSaving(true)
    setAgentSaved(false)
    setAgentError('')
    try {
      const res = await api.settings.save({
        provider,
      })
      if (!res.ok) {
        let detail = ''
        try { detail = (await res.json()).detail || '' } catch {}
        throw new Error(detail || 'Could not save agent defaults.')
      }
      settingsQueries.owner.invalidate(queryClient)
      setAgentSaved(true)
      setTimeout(() => setAgentSaved(false), 1600)
      return true
    } catch (err) {
      setAgentError(err.message || 'Could not save agent defaults.')
      return false
    } finally {
      setAgentSaving(false)
    }
  }, [queryClient])

  async function handleConnected(provider) {
    const preferred = firstConnectedProvider(provider) || provider
    setAgentProvider(preferred)
    settingsQueries.owner.invalidate(queryClient)
    authQueries.provider.claudeStatus.invalidate(queryClient)
    authQueries.provider.statuses.invalidate(queryClient)
    if (await saveConnectedProvider(preferred)) setExpanded(null)
  }

  async function retryProviderSave() {
    const preferred = agentProvider || firstConnectedProvider()
    if (await saveConnectedProvider(preferred)) setExpanded(null)
  }

  const readyToContinue = connectedAny && !agentSaving && !agentError

  return (
    <div className="setup">
      <div className="setup__card">
        <img src={`${BASE}/moebius.png`} alt="Möbius" className="setup__logo" />
        <SetupProgress step="provider" />
        <h1 className="setup__title">Wake up your AI</h1>
        <p className="setup__subtitle">
          Connect Codex or Claude. Möbius will choose simple defaults now;
          you can tune models later in Settings.
        </p>

        <div className="settings__providers">
          <ProviderRow
            name="OpenAI Codex"
            badge="Free ChatGPT account"
            connected={codexConnected}
            expanded={expanded === 'codex'}
            onToggleExpand={() => toggle('codex')}
          >
            <CodexAuth onConnected={() => handleConnected('codex')} />
          </ProviderRow>

          <ProviderRow
            name="Claude Code"
            connected={claudeConnected}
            expanded={expanded === 'claude'}
            onToggleExpand={() => toggle('claude')}
          >
            <ProviderAuth
              authenticated={claudeAuthenticated}
              compact
              onDone={() => handleConnected('claude')}
            />
          </ProviderRow>
        </div>

        {agentSaved && <p className="setup__success" role="status">Provider connected. Ready when you are.</p>}
        {agentError && <p className="setup__error" role="alert">{agentError}</p>}
        {agentError && (
          <button
            className="setup__btn setup__btn--full"
            type="button"
            onClick={retryProviderSave}
            disabled={agentSaving || !agentProvider}
          >
            {agentSaving ? 'Saving…' : 'Try saving again'}
          </button>
        )}

        {!connectedAny && (
          <p className="setup__skip-warn">
            You can explore without AI, but chats need a provider before they can run.
          </p>
        )}
        <button
          className="setup__btn setup__btn--full"
          type="button"
          onClick={onContinue}
          disabled={!readyToContinue}
        >
          {agentSaving ? 'Saving…' : 'Enter Möbius'}
        </button>
        <button className="setup__skip" type="button" onClick={onSkip} disabled={agentSaving}>
          Explore without AI
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
