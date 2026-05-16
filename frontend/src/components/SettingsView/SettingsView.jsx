import { useState, useEffect, useRef, useCallback } from 'react'
import { apiFetch } from '../../api/client.js'
import { DARK_COLORS, LIGHT_COLORS, parseThemeMeta, buildThemeCss } from '../../theme.js'
import ProviderAuth from '../ProviderAuth/ProviderAuth.jsx'
import './SettingsView.css'

function CodexAuth({ onConnected }) {
  const [status, setStatus] = useState('idle') // idle | connecting | pending | complete | failed
  const [url, setUrl] = useState('')
  const [code, setCode] = useState('')
  const [error, setError] = useState('')
  const pollRef = useRef(null)

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [])

  useEffect(() => () => stopPolling(), [stopPolling])

  async function startLogin() {
    setError('')
    setStatus('connecting')
    try {
      const res = await apiFetch('/auth/provider/codex/login', { method: 'POST' })
      if (!res.ok) {
        const data = await res.json()
        setError(data.detail || 'Could not start Codex login.')
        setStatus('idle')
        return
      }
      const data = await res.json()
      setUrl(data.url)
      setCode(data.code)
      setStatus('pending')

      // Poll for completion.
      stopPolling()
      pollRef.current = setInterval(async () => {
        try {
          const r = await apiFetch('/auth/provider/codex/status')
          const s = await r.json()
          if (s.status === 'complete') {
            stopPolling()
            setStatus('complete')
            setUrl('')
            setCode('')
            onConnected?.()
          } else if (s.status === 'failed') {
            stopPolling()
            setStatus('failed')
            setError('Login failed. Please try again.')
          }
        } catch { /* ignore polling errors */ }
      }, 3000)
    } catch {
      setError('Network error.')
      setStatus('idle')
    }
  }

  if (status === 'pending') {
    return (
      <div className="codex-auth">
        <p className="settings__subtext">
          Complete sign-in in your browser:
        </p>
        <div className="codex-auth__device">
          <div className="codex-auth__step">
            <span className="codex-auth__step-num">1</span>
            <span>Open <a href={url} target="_blank" rel="noopener noreferrer">{url}</a></span>
          </div>
          <div className="codex-auth__step">
            <span className="codex-auth__step-num">2</span>
            <span>Enter code: <strong className="codex-auth__code">{code}</strong></span>
          </div>
        </div>
        <p className="settings__subtext">Waiting for sign-in to complete...</p>
      </div>
    )
  }

  if (status === 'complete') {
    return (
      <div className="codex-auth">
        <span className="pa__success">Connected to Codex</span>
      </div>
    )
  }

  return (
    <div className="codex-auth">
      <button
        className="pa__btn"
        onClick={startLogin}
        disabled={status === 'connecting'}
      >
        {status === 'connecting' ? 'Starting...' : 'Connect to Codex'}
      </button>
      {error && <p className="pa__error">{error}</p>}
    </div>
  )
}

export default function SettingsView({ onThemeChange }) {
  const [geminiKey, setGeminiKey] = useState('')
  const [configured, setConfigured] = useState(false)
  const [saving, setSaving] = useState(false)
  const [status, setStatus] = useState(null)
  const [errorMsg, setErrorMsg] = useState('')
  const [lightMode, setLightMode] = useState(false)
  const [themeSwitching, setThemeSwitching] = useState(false)

  // Provider state.
  const [provider, setProvider] = useState('claude')
  const [claudeAuthenticated, setClaudeAuthenticated] = useState(false)
  const [codexAuthenticated, setCodexAuthenticated] = useState(false)
  const [providerSaving, setProviderSaving] = useState(false)

  useEffect(() => {
    apiFetch('/settings')
      .then(r => r.json())
      .then(data => {
        if (data.gemini_configured) setConfigured(true)
        if (data.provider) setProvider(data.provider)
        if (data.codex_authenticated) setCodexAuthenticated(true)
      })
      .catch(() => {})

    apiFetch('/auth/provider/status')
      .then(r => r.json())
      .then(data => { if (data.authenticated) setClaudeAuthenticated(true) })
      .catch(() => {})

    apiFetch('/storage/shared/theme-mode')
      .then(r => r.ok ? r.json() : null)
      .then(data => { if (data === 'light') setLightMode(true) })
      .catch(() => {})
  }, [])

  async function selectProvider(newProvider) {
    if (newProvider === provider || providerSaving) return
    const oldProvider = provider
    setProvider(newProvider)
    setProviderSaving(true)
    try {
      const res = await apiFetch('/settings', {
        method: 'POST',
        body: JSON.stringify({ provider: newProvider }),
      })
      if (!res.ok) setProvider(oldProvider)
    } catch {
      setProvider(oldProvider)
    } finally {
      setProviderSaving(false)
    }
  }

  async function toggleTheme() {
    const newMode = !lightMode
    setLightMode(newMode)
    setThemeSwitching(true)

    try {
      const themeRes = await apiFetch('/storage/shared/theme.css')
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

      // Apply immediately to the DOM — no round-trip delay.
      const el = document.getElementById('mobius-theme') || (() => {
        const s = document.createElement('style')
        s.id = 'mobius-theme'
        document.head.appendChild(s)
        return s
      })()
      const bgMatch = newCss.match(/--bg:\s*(#[0-9a-fA-F]{3,8})/)
      if (bgMatch) {
        document.body.style.background = bgMatch[1]
        const themeMeta = document.querySelector('meta[name="theme-color"]')
        if (themeMeta) themeMeta.setAttribute('content', bgMatch[1])
      }
      el.textContent = newCss.replace(/@import\s+url\([^)]+\)\s*;[^\S\n]*\n?/g, '')

      // Persist in background — don't await sequentially.
      await Promise.all([
        apiFetch('/storage/shared/theme.css', {
          method: 'PUT',
          body: JSON.stringify({ content: newCss }),
        }),
        apiFetch('/storage/shared/theme-mode', {
          method: 'PUT',
          body: JSON.stringify({ content: JSON.stringify(mode) }),
        }),
      ])

      apiFetch('/notify', {
        method: 'POST',
        body: JSON.stringify({ type: 'theme_updated' }),
      }).catch(() => {})

      // Invalidate the theme query so AppCanvas picks up the change
      // and sends moebius:frame-theme to iframes. The /notify POST
      // only reaches active chat broadcasts — when no agent is
      // running, iframes would never get the update without this.
      onThemeChange?.()
    } catch {
      setLightMode(!newMode)
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
      const res = await apiFetch('/settings', {
        method: 'POST',
        body: JSON.stringify({ gemini_api_key: geminiKey.trim() }),
      })
      if (!res.ok) {
        const data = await res.json()
        setErrorMsg(data.detail || 'Failed to save key.')
        setStatus('error')
        return
      }
      setConfigured(true)
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
          <h2 className="settings__section-title">AI provider</h2>
          <p className="settings__subtext" style={{ marginBottom: 12 }}>Start new chats with:</p>

          <div className="settings__provider-cards">
            <button
              className={`settings__provider-card${provider === 'claude' ? ' settings__provider-card--selected' : ''}`}
              onClick={() => selectProvider('claude')}
              disabled={providerSaving}
            >
              <span className="settings__provider-radio">
                {provider === 'claude' && <span className="settings__provider-radio-dot" />}
              </span>
              <span className="settings__provider-card-info">
                <span className="settings__provider-card-name">Claude Code</span>
                <span className={`settings__provider-card-status${claudeAuthenticated ? ' settings__provider-card-status--ok' : ''}`}>
                  {claudeAuthenticated ? 'Connected' : 'Not connected'}
                </span>
              </span>
            </button>

            <button
              className={`settings__provider-card${provider === 'codex' ? ' settings__provider-card--selected' : ''}`}
              onClick={() => selectProvider('codex')}
              disabled={providerSaving}
            >
              <span className="settings__provider-radio">
                {provider === 'codex' && <span className="settings__provider-radio-dot" />}
              </span>
              <span className="settings__provider-card-info">
                <span className="settings__provider-card-name">Codex (OpenAI)</span>
                <span className={`settings__provider-card-status${codexAuthenticated ? ' settings__provider-card-status--ok' : ''}`}>
                  {codexAuthenticated ? 'Connected' : 'Not connected'}
                </span>
              </span>
            </button>
          </div>

          <div className="settings__provider-block" style={{ marginTop: 16 }}>
            <div className="settings__provider-header">
              <span className="settings__provider-name">Claude Code</span>
            </div>
            <ProviderAuth compact onDone={() => setClaudeAuthenticated(true)} />
          </div>

          <div className="settings__provider-block" style={{ marginTop: 12 }}>
            <div className="settings__provider-header">
              <span className="settings__provider-name">Codex (OpenAI)</span>
            </div>
            {codexAuthenticated ? (
              <div className="pa__row">
                <span className="pa__label">
                  <span className="pa__success">Connected</span>
                </span>
              </div>
            ) : (
              <CodexAuth onConnected={() => setCodexAuthenticated(true)} />
            )}
          </div>
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
