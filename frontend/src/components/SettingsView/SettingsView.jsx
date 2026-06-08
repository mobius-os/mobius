import { useState, useEffect, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Switch } from '@openai/apps-sdk-ui/components/Switch'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { TextLink } from '@openai/apps-sdk-ui/components/TextLink'
import { api } from '../../api/client.js'
import { authQueries, settingsQueries, themeQueries } from '../../hooks/queries.js'
import * as themeService from '../../lib/themeService.js'
import ProviderAuth from '../ProviderAuth/ProviderAuth.jsx'
import CodexAuth from '../ProviderAuth/CodexAuth.jsx'
import ProviderRow from '../ProviderAuth/ProviderRow.jsx'
import StatusDot from '../ui/StatusDot.jsx'
import '../ui/StatusDot.css'
import './SettingsView.css'


export default function SettingsView({ onThemeChange }) {
  const queryClient = useQueryClient()
  const settingsQuery = settingsQueries.owner.useQuery()
  const claudeStatusQuery = authQueries.provider.claudeStatus.useQuery()
  const themeModeQuery = themeQueries.mode.useQuery()
  const [geminiKey, setGeminiKey] = useState('')
  const [saving, setSaving] = useState(false)
  const [status, setStatus] = useState(null)
  const [errorMsg, setErrorMsg] = useState('')
  // Mirrors ProviderAuth.jsx's `justConnected` pattern: bake the
  // success signal into the Save button label for 2s instead of a
  // separate <Alert> row that auto-dismisses. Cleaner one-place
  // feedback; the form already owns the button.
  const [justSaved, setJustSaved] = useState(false)
  const [lightMode, setLightMode] = useState(false)
  const [themeSwitching, setThemeSwitching] = useState(false)
  // Which provider has its inline auth panel expanded. null = none.
  const [expandedAuth, setExpandedAuth] = useState(null)
  // Surface failures from the dark-mode toggle: a failed theme
  // persist would otherwise bounce the knob without telling the user
  // why.
  const [themeError, setThemeError] = useState('')
  const [restartPhase, setRestartPhase] = useState('idle')
  const [restartError, setRestartError] = useState('')

  useEffect(() => {
    // Mirror the full query value so a cache invalidation that
    // resolves to 'dark' actually flips the knob back. The earlier
    // light-only branch left the toggle stuck on whenever data went
    // light → dark via refetch (e.g. another tab toggled, or a
    // failed persist's rollback landed via invalidation).
    if (themeModeQuery.data === undefined) return
    setLightMode(themeModeQuery.data === 'light')
  }, [themeModeQuery.data])

  const configured = !!settingsQuery.data?.gemini_configured
  const codexAuthenticated = !!settingsQuery.data?.codex_authenticated
  // Live-probed CLI versions (null when the CLI isn't installed or
  // didn't respond). Read-only — updates happen via the agent, not here.
  const claudeVersion = settingsQuery.data?.claude_version
  const codexVersion = settingsQuery.data?.codex_version
  const claudeAuthenticated = !!claudeStatusQuery.data?.authenticated
  const providerLoaded = settingsQuery.isFetched && claudeStatusQuery.isFetched

  // Stable identity-preserving callbacks: passing fresh arrow
  // functions in JSX re-mounted ProviderRow's event handlers every
  // render, which combined with the row's CSS transitions made the
  // panel feel jittery. With the updater form, deps are empty.
  const toggleClaudeAuth = useCallback(
    () => setExpandedAuth(prev => prev === 'claude' ? null : 'claude'),
    [],
  )
  const toggleCodexAuth = useCallback(
    () => setExpandedAuth(prev => prev === 'codex' ? null : 'codex'),
    [],
  )
  const onClaudeAuthDone = useCallback(() => {
    authQueries.provider.claudeStatus.invalidate(queryClient)
    setExpandedAuth(null)
  }, [queryClient])
  const onCodexAuthDone = useCallback(() => {
    settingsQueries.owner.invalidate(queryClient)
    setExpandedAuth(null)
  }, [queryClient])

  async function toggleTheme() {
    if (themeSwitching) return
    const newMode = !lightMode
    setLightMode(newMode)
    setThemeSwitching(true)
    setThemeError('')

    // Delegate the full apply/persist/invalidate dance to
    // themeService — SettingsView keeps only the optimistic UI
    // state (setLightMode + setThemeError) and the
    // catch-rollback. themeService.toggleTheme invalidates both
    // theme queries; AppCanvas's useEffect picks that up and
    // postMessages `moebius:frame-theme` to live iframes.
    try {
      const currentMode = newMode ? 'dark' : 'light'  // opposite of newMode
      await themeService.toggleTheme(queryClient, currentMode, api)
      onThemeChange?.()
    } catch {
      setLightMode(!newMode)
      setThemeError(
        'Could not save theme. Check your connection and try again.',
      )
      // Force the mode query to resync with the server. Covers the
      // write-succeeded-but-response-lost case: refetching reads
      // authoritative state, the mirror effect at line 30 picks it
      // up, and lightMode stops disagreeing with the visible theme.
      themeQueries.mode.invalidate(queryClient)
      onThemeChange?.()  // reload original theme on error
    } finally {
      setThemeSwitching(false)
    }
  }

  async function handleSave(e) {
    e.preventDefault()
    if (!geminiKey.trim()) return
    setSaving(true)
    setStatus(null)
    setErrorMsg('')
    try {
      const res = await api.settings.save({ gemini_api_key: geminiKey.trim() })
      if (!res.ok) {
        const data = await res.json()
        setErrorMsg(data.detail || 'Failed to save key.')
        setStatus('error')
        return
      }
      settingsQueries.owner.invalidate(queryClient)
      setGeminiKey('')
      setStatus('success')
      setJustSaved(true)
      setTimeout(() => setJustSaved(false), 2000)
    } catch {
      setErrorMsg('Network error.')
      setStatus('error')
    } finally {
      setSaving(false)
    }
  }

  async function restartServer() {
    if (restartPhase === 'restarting') return
    setRestartPhase('restarting')
    setRestartError('')
    try {
      const res = await api.admin.restart()
      if (!res.ok) {
        let detail = ''
        try { detail = (await res.json()).detail || '' } catch {}
        throw new Error(detail || `Restart failed (${res.status})`)
      }
      setTimeout(() => window.location.reload(), 10000)
    } catch (err) {
      setRestartPhase('idle')
      setRestartError(err.message || 'Restart request failed.')
    }
  }

  return (
    <div className="settings">
      <div className="settings__content">
        <h1 className="settings__title">Settings</h1>

        <section className="settings__section">
          <h2 className="settings__section-title">AI providers</h2>

          {providerLoaded && (
            <div className="settings__providers">
              <ProviderRow
                id="codex"
                name="OpenAI Codex"
                showRadio={false}
                connected={codexAuthenticated}
                expanded={expandedAuth === 'codex'}
                onToggleExpand={toggleCodexAuth}
              >
                <CodexAuth onConnected={onCodexAuthDone} />
              </ProviderRow>

              <ProviderRow
                id="claude"
                name="Claude Code"
                showRadio={false}
                connected={claudeAuthenticated}
                expanded={expandedAuth === 'claude'}
                onToggleExpand={toggleClaudeAuth}
              >
                <ProviderAuth
                  authenticated={claudeAuthenticated}
                  compact
                  onDone={onClaudeAuthDone}
                />
              </ProviderRow>
            </div>
          )}
        </section>

        <section className="settings__section">
          <h2 className="settings__section-title">Runtime</h2>
          <p className="settings__subtext">
            Installed CLI versions, probed each time Settings opens.
            Updates are handled by the agent.
          </p>
          <div className="settings__versions">
            <div className="settings__row">
              <span className="settings__label">Claude Code</span>
              <span className="settings__version">
                {claudeVersion || 'Not installed'}
              </span>
            </div>
            <div className="settings__row">
              <span className="settings__label">OpenAI Codex</span>
              <span className="settings__version">
                {codexVersion || 'Not installed'}
              </span>
            </div>
          </div>
        </section>

        <section className="settings__section">
          <h2 className="settings__section-title">Image generation</h2>
          <form className="settings__form" onSubmit={handleSave}>
            <label className="settings__label">
              Gemini API key
              {configured && <StatusDot color="--green">Configured</StatusDot>}
            </label>
            <p className="settings__subtext">
              Get one at{' '}
              <TextLink href="https://aistudio.google.com/apikey" forceExternal>
                aistudio.google.com
              </TextLink>.
            </p>
            {/* The key is always pasted, never typed, so there's no reveal
                toggle — keep the field a plain masked paste target. */}
            <input
              className="settings__input"
              type="password"
              value={geminiKey}
              onChange={(e) => { setGeminiKey(e.target.value); setStatus(null) }}
              placeholder={configured ? '••••••••' : 'AIza...'}
              autoComplete="off"
            />
            {status === 'error' && (
              <Alert
                color="danger"
                variant="soft"
                description={errorMsg}
              />
            )}
            <button
              className="settings__btn"
              type="submit"
              disabled={saving || !geminiKey.trim()}
            >
              {saving ? 'Saving…' : justSaved ? 'Saved' : 'Save'}
            </button>
          </form>
        </section>

        <section className="settings__section settings__section--compact">
          <div className="settings__row">
            <span className="settings__label">Dark mode</span>
            <Switch
              checked={!lightMode}
              onCheckedChange={toggleTheme}
              disabled={themeSwitching}
              aria-label="Toggle dark mode"
            />
          </div>
          {themeError && (
            <Alert
              color="danger"
              variant="soft"
              description={themeError}
            />
          )}
        </section>

        <section className="settings__section settings__section--compact">
          <div className="settings__row">
            <div>
              <span className="settings__label">Server</span>
              <p className="settings__subtext settings__subtext--tight">
                Restart after backend or configuration changes.
              </p>
            </div>
            <button
              className="settings__btn settings__btn--outline settings__btn--sm"
              type="button"
              onClick={restartServer}
              disabled={restartPhase === 'restarting'}
            >
              {restartPhase === 'restarting' ? 'Restarting…' : 'Restart'}
            </button>
          </div>
          {restartPhase === 'restarting' && (
            <div className="settings__notice" role="status">
              Restart signal sent. The page will reload shortly.
            </div>
          )}
          {restartError && (
            <Alert
              color="danger"
              variant="soft"
              description={restartError}
            />
          )}
        </section>

        <section className="settings__section settings__section--compact">
          <div className="settings__row">
            <span className="settings__label">Recovery</span>
            <a className="settings__btn settings__btn--outline settings__btn--sm" href="/recover" target="_blank" rel="noopener noreferrer">
              Open
            </a>
          </div>
        </section>
      </div>
    </div>
  )
}
