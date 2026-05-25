import { useState, useEffect, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client.js'
import { authQueries, settingsQueries, themeQueries } from '../../hooks/queries.js'
import { DARK_COLORS, LIGHT_COLORS, parseThemeMeta, buildThemeCss } from '../../theme.js'
import { applyThemeToDom, persistTheme } from '../../lib/themeService.js'
import ProviderAuth from '../ProviderAuth/ProviderAuth.jsx'
import CodexAuth from '../ProviderAuth/CodexAuth.jsx'
import ProviderRow from '../ProviderAuth/ProviderRow.jsx'
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
  const [lightMode, setLightMode] = useState(false)
  const [themeSwitching, setThemeSwitching] = useState(false)
  // Which provider has its inline auth panel expanded. null = none.
  const [expandedAuth, setExpandedAuth] = useState(null)
  // Surface failures from the dark-mode toggle: a failed theme
  // persist would otherwise bounce the knob without telling the user
  // why.
  const [themeError, setThemeError] = useState('')

  useEffect(() => {
    if (themeModeQuery.data === 'light') setLightMode(true)
  }, [themeModeQuery.data])

  const configured = !!settingsQuery.data?.gemini_configured
  const codexAuthenticated = !!settingsQuery.data?.codex_authenticated
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
    const newMode = !lightMode
    setLightMode(newMode)
    setThemeSwitching(true)
    setThemeError('')

    try {
      const themeRes = await api.storage.shared.getThemeCss()
      const currentCss = themeRes.ok ? await themeRes.text() : ''
      const meta = parseThemeMeta(currentCss)

      // Swap structural colors for the new mode while preserving agent
      // customizations (accents, custom vars, etc.).
      const base = newMode ? LIGHT_COLORS : DARK_COLORS
      const structuralKeys = ['--bg', '--surface', '--surface2', '--border', '--border-light', '--text', '--muted']
      const swapped = {}
      for (const k of structuralKeys) { if (base[k]) swapped[k] = base[k] }
      const colors = { ...meta.colors, ...swapped }
      const mode = newMode ? 'light' : 'dark'
      const newCss = buildThemeCss(colors, meta, mode)

      // Extract bg from the NEW css (not from old meta) — toggling
      // mode swaps --bg so meta.bg is stale.
      const bgMatch = newCss.match(/--bg:\s*(#[0-9a-fA-F]{3,8})/)
      const newBg = bgMatch ? bgMatch[1] : meta.colors['--bg']

      // Apply immediately to the DOM — no round-trip delay. Single
      // entry point in themeService keeps SettingsView free of
      // direct getElementById / body.style / meta-tag mutations.
      applyThemeToDom(newCss, newBg)

      // Persist in background.
      await persistTheme(newCss, mode, api)

      // Invalidate the theme query so AppCanvas picks up the change
      // and sends moebius:frame-theme to iframes. The /notify POST
      // only reaches active chat broadcasts — when no agent is
      // running, iframes would never get the update without this.
      themeQueries.mode.invalidate(queryClient)
      themeQueries.invalidate(queryClient)
      onThemeChange?.()
    } catch {
      setLightMode(!newMode)
      setThemeError(
        'Could not save theme. Check your connection and try again.',
      )
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
    } catch {
      setErrorMsg('Network error.')
      setStatus('error')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="settings">
      <div className="settings__content">
        <h1 className="settings__title">Settings</h1>

        <section className="settings__section">
          <h2 className="settings__section-title">AI providers</h2>
          <p className="settings__subtext" style={{ marginBottom: 12 }}>
            Connect the providers you want to use. Pick the agent in
            each chat using the "/" button.
          </p>

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
                <ProviderAuth compact onDone={onClaudeAuthDone} />
              </ProviderRow>
            </div>
          )}
        </section>

        <section className="settings__section">
          <h2 className="settings__section-title">Image generation</h2>
          <form className="settings__form" onSubmit={handleSave}>
            <label className="settings__label">
              Gemini API key
              {configured && <span className="settings__badge">Configured</span>}
            </label>
            <p className="settings__subtext">
              Used for image generation.
              Get a free key at{' '}
              <a href="https://aistudio.google.com/apikey" target="_blank" rel="noopener noreferrer">
                aistudio.google.com
              </a>.
            </p>
            <input
              className="settings__input"
              type="password"
              value={geminiKey}
              onChange={(e) => { setGeminiKey(e.target.value); setStatus(null) }}
              placeholder={configured ? '••••••••' : 'AIza...'}
              autoComplete="off"
            />
            {status === 'success' && (
              <p className="settings__success">Saved successfully.</p>
            )}
            {status === 'error' && (
              <p className="settings__error">{errorMsg}</p>
            )}
            <button
              className="settings__btn"
              type="submit"
              disabled={saving || !geminiKey.trim()}
            >
              {saving ? 'Saving…' : 'Save'}
            </button>
          </form>
        </section>

        <section className="settings__section settings__section--compact">
          <div className="settings__row">
            <span className="settings__label">Dark mode</span>
            <button
              className={`settings__toggle ${!lightMode ? 'settings__toggle--on' : ''}`}
              onClick={toggleTheme}
              disabled={themeSwitching}
              role="switch"
              aria-checked={!lightMode}
              aria-label="Toggle dark mode"
            >
              <span className="settings__toggle-knob" />
            </button>
          </div>
          {themeError && (
            <p className="settings__error">{themeError}</p>
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
