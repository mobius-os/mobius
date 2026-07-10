import { useState, useEffect, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api, setToken, BASE } from '../../api/client.js'
import * as setupSession from '../../lib/setupSession.js'
import { authQueries, modelQueries, settingsQueries } from '../../hooks/queries.js'
import ProviderAuth from '../ProviderAuth/ProviderAuth.jsx'
import CodexAuth from '../ProviderAuth/CodexAuth.jsx'
import ProviderRow from '../ProviderAuth/ProviderRow.jsx'
import { CLAUDE_MODELS, CODEX_MODELS } from '../ProviderModelPicker/ProviderModelPicker.jsx'
import { PROVIDER_INFO } from '../ChatView/ChatSettingsPanel.jsx'
import './SetupWizard.css'

const SETUP_STEPS = [
  { id: 'account', label: 'Account' },
  { id: 'provider', label: 'AI' },
  { id: 'gemini', label: 'Images' },
]
const PROVIDERS = [
  { id: 'codex', label: 'OpenAI Codex' },
  { id: 'claude', label: 'Claude Code' },
]
const FALLBACK_MODELS = {
  claude: CLAUDE_MODELS.map(m => ({ id: m.value, label: m.label })),
  codex: CODEX_MODELS.map(m => ({ id: m.value, label: m.label })),
}

function defaultEffort(provider) {
  const efforts = PROVIDER_INFO[provider]?.efforts || []
  return efforts.find(e => e.value === 'medium')?.value || efforts[0]?.value || ''
}

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
        onContinue={() => goToStep('gemini')}
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
function ProviderStep({ onSkip, onContinue, claudeAuthenticated }) {
  const queryClient = useQueryClient()
  const [expanded, setExpanded] = useState('codex')
  const settingsQuery = settingsQueries.owner.useQuery()
  const modelRegistryQuery = modelQueries.registry.useQuery()
  const codexConnected = !!settingsQuery.data?.codex_authenticated
  const claudeConnected = !!claudeAuthenticated
  const connectedAny = codexConnected || claudeConnected
  const [chatChoice, setChatChoice] = useState({ provider: 'codex', model: '' })
  const [primaryChoice, setPrimaryChoice] = useState({ provider: 'codex', model: '' })
  const [secondaryChoice, setSecondaryChoice] = useState({ provider: '', model: '' })
  const [agentSaving, setAgentSaving] = useState(false)
  const [agentSaved, setAgentSaved] = useState(false)
  const [agentError, setAgentError] = useState('')

  useEffect(() => {
    if (!settingsQuery.data) return
    const provider = PROVIDERS.some(p => p.id === settingsQuery.data.provider)
      ? settingsQuery.data.provider
      : 'codex'
    setChatChoice({
      provider,
      model: settingsQuery.data.agent_settings?.model || '',
    })
    setPrimaryChoice({
      provider: settingsQuery.data.background_agents?.primary?.provider || provider,
      model: settingsQuery.data.background_agents?.primary?.model || '',
    })
    setSecondaryChoice({
      provider: settingsQuery.data.background_agents?.fallback?.provider || '',
      model: settingsQuery.data.background_agents?.fallback?.model || '',
    })
  }, [settingsQuery.data])

  function toggle(id) {
    setExpanded(prev => prev === id ? null : id)
  }

  function modelsFor(provider) {
    if (!provider) return []
    const rows = modelRegistryQuery.data?.[provider]
    if (Array.isArray(rows) && rows.length) {
      return rows.map(m => ({ id: m.id, label: m.label || m.id }))
    }
    return FALLBACK_MODELS[provider] || []
  }

  function connectedMap(extraProvider = '') {
    return {
      codex: codexConnected || extraProvider === 'codex',
      claude: claudeConnected || extraProvider === 'claude',
    }
  }

  function choiceConnected(choice, extraProvider = '') {
    const connected = connectedMap(extraProvider)
    return !!choice?.provider && connected[choice.provider] === true
  }

  function firstConnectedProvider(extraProvider = '') {
    const connected = connectedMap(extraProvider)
    return PROVIDERS.find(provider => connected[provider.id])?.id || ''
  }

  const persistAgentChoices = useCallback(async (nextChat, nextPrimary, nextSecondary) => {
    if (!nextChat?.provider || !nextPrimary?.provider) return
    setAgentSaving(true)
    setAgentSaved(false)
    setAgentError('')
    try {
      const providerRows = PROVIDERS.map(({ id }) => {
        const isPrimary = nextPrimary.provider === id
        const isFallback = nextSecondary?.provider === id
        const enabled = isPrimary || isFallback
        const model = isPrimary ? nextPrimary.model : isFallback ? nextSecondary.model : ''
        return {
          provider: id,
          model: enabled ? (model || null) : null,
          effort: enabled ? defaultEffort(id) : null,
          enabled,
        }
      })
      const res = await api.settings.save({
        provider: nextChat.provider,
        agent_settings: {
          model: nextChat.model || null,
          effort: defaultEffort(nextChat.provider),
          effort_by_provider: {
            [nextChat.provider]: defaultEffort(nextChat.provider),
          },
        },
        background_agents: {
          providers: providerRows,
          primary: {
            provider: nextPrimary.provider,
            model: nextPrimary.model || null,
            effort: defaultEffort(nextPrimary.provider),
          },
          fallback: nextSecondary?.provider
            ? {
                provider: nextSecondary.provider,
                model: nextSecondary.model || null,
                effort: defaultEffort(nextSecondary.provider),
              }
            : null,
        },
      })
      if (!res.ok) {
        let detail = ''
        try { detail = (await res.json()).detail || '' } catch {}
        throw new Error(detail || 'Could not save agent defaults.')
      }
      settingsQueries.owner.invalidate(queryClient)
      setAgentSaved(true)
      setTimeout(() => setAgentSaved(false), 1600)
    } catch (err) {
      setAgentError(err.message || 'Could not save agent defaults.')
    } finally {
      setAgentSaving(false)
    }
  }, [queryClient])

  function updateChoice(slot, patch) {
    const currentChat = chatChoice
    const currentPrimary = primaryChoice
    const currentSecondary = secondaryChoice
    const normalize = (choice, nextPatch) => {
      const next = { ...choice, ...nextPatch }
      if ('provider' in nextPatch) next.model = ''
      return next
    }
    const nextChat = slot === 'chat' ? normalize(currentChat, patch) : currentChat
    const nextPrimary = slot === 'primary' ? normalize(currentPrimary, patch) : currentPrimary
    const nextSecondary = slot === 'secondary' ? normalize(currentSecondary, patch) : currentSecondary
    setChatChoice(nextChat)
    setPrimaryChoice(nextPrimary)
    setSecondaryChoice(nextSecondary)
    persistAgentChoices(nextChat, nextPrimary, nextSecondary)
  }

  function handleConnected(provider) {
    const preferred = firstConnectedProvider(provider) || provider
    if (preferred) {
      const nextChat = choiceConnected(chatChoice, provider)
        ? chatChoice
        : { provider: preferred, model: '' }
      const nextPrimary = choiceConnected(primaryChoice, provider)
        ? primaryChoice
        : { provider: preferred, model: '' }
      setChatChoice(nextChat)
      setPrimaryChoice(nextPrimary)
      persistAgentChoices(nextChat, nextPrimary, secondaryChoice)
    }
    settingsQueries.owner.invalidate(queryClient)
    authQueries.provider.claudeStatus.invalidate(queryClient)
    setExpanded(null)
  }

  const defaultsReady =
    connectedAny &&
    choiceConnected(chatChoice) &&
    choiceConnected(primaryChoice) &&
    !agentSaving &&
    !agentError
  const connectedChoices = connectedMap()

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
            <CodexAuth onConnected={() => handleConnected('codex')} />
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
              onDone={() => handleConnected('claude')}
            />
          </ProviderRow>
        </div>

        <div className="setup-agent">
          <div className="setup-agent__head">
            <h2>Agent defaults</h2>
            <span>{agentSaving ? 'Saving...' : agentSaved ? 'Saved' : 'Auto-saves'}</span>
          </div>
          <SetupAgentChoice
            label="Chats"
            choice={chatChoice}
            models={modelsFor(chatChoice.provider)}
            connected={connectedChoices}
            onProviderChange={(provider) => updateChoice('chat', { provider })}
            onModelChange={(model) => updateChoice('chat', { model })}
          />
          <SetupAgentChoice
            label="Background primary"
            choice={primaryChoice}
            models={modelsFor(primaryChoice.provider)}
            connected={connectedChoices}
            onProviderChange={(provider) => updateChoice('primary', { provider })}
            onModelChange={(model) => updateChoice('primary', { model })}
          />
          <SetupAgentChoice
            label="Background secondary"
            allowNone
            choice={secondaryChoice}
            models={modelsFor(secondaryChoice.provider)}
            connected={connectedChoices}
            onProviderChange={(provider) => updateChoice('secondary', { provider })}
            onModelChange={(model) => updateChoice('secondary', { model })}
          />
          {agentError && <p className="setup__error">{agentError}</p>}
        </div>

        {!connectedAny && (
          <p className="setup__skip-warn">
            Without a provider, all chat messages will fail.
          </p>
        )}
        <button
          className="setup__btn setup__btn--full"
          type="button"
          onClick={onContinue}
          disabled={!defaultsReady}
        >
          Continue
        </button>
        <button className="setup__skip" type="button" onClick={onSkip}>
          Skip for now
        </button>
      </div>
    </div>
  )
}

function SetupAgentChoice({
  label,
  choice,
  models,
  allowNone = false,
  connected,
  onProviderChange,
  onModelChange,
}) {
  const hasConnectedProvider = connected && Object.values(connected).some(Boolean)
  const providerChoices = PROVIDERS.filter(provider => (
    !hasConnectedProvider || connected[provider.id] || provider.id === choice.provider
  ))
  return (
    <div className="setup-agent-row">
      <div className="setup-agent-row__label">{label}</div>
      <div className="setup-agent-row__controls">
        <select
          className="setup__select"
          value={choice.provider || ''}
          onChange={(e) => onProviderChange(e.target.value)}
          aria-label={`${label} provider`}
        >
          {allowNone && <option value="">No fallback</option>}
          {providerChoices.map(provider => (
            <option key={provider.id} value={provider.id}>{provider.label}</option>
          ))}
        </select>
        <select
          className="setup__select"
          value={choice.model || ''}
          onChange={(e) => onModelChange(e.target.value)}
          aria-label={`${label} model`}
          disabled={!choice.provider}
        >
          <option value="">Provider default</option>
          {models.map(model => (
            <option key={model.id} value={model.id}>{model.label || model.id}</option>
          ))}
        </select>
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
