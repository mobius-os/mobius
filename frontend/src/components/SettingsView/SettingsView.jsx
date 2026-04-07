import { useState, useEffect } from 'react'
import { apiFetch } from '../../api/client.js'
import { DARK_COLORS, LIGHT_COLORS, parseThemeMeta, buildThemeCss } from '../../theme.js'
import ProviderAuth from '../ProviderAuth/ProviderAuth.jsx'
import './SettingsView.css'

export default function SettingsView({ onThemeChange }) {
  const [geminiKey, setGeminiKey] = useState('')
  const [configured, setConfigured] = useState(false)
  const [saving, setSaving] = useState(false)
  const [status, setStatus] = useState(null)
  const [errorMsg, setErrorMsg] = useState('')
  const [lightMode, setLightMode] = useState(false)
  const [themeSwitching, setThemeSwitching] = useState(false)

  useEffect(() => {
    apiFetch('/settings')
      .then(r => r.json())
      .then(data => { if (data.gemini_configured) setConfigured(true) })
      .catch(() => {})

    apiFetch('/storage/shared/theme-mode')
      .then(r => r.ok ? r.json() : null)
      .then(data => { if (data === 'light') setLightMode(true) })
      .catch(() => {})
  }, [])

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

      await apiFetch('/storage/shared/theme.css', {
        method: 'PUT',
        body: JSON.stringify({ content: newCss }),
      })

      await apiFetch('/storage/shared/theme-mode', {
        method: 'PUT',
        body: JSON.stringify({ content: JSON.stringify(mode) }),
      })

      await apiFetch('/notify', {
        method: 'POST',
        body: JSON.stringify({ type: 'theme_updated' }),
      })

      onThemeChange?.()
    } catch {
      setLightMode(!newMode)
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
          <ProviderAuth compact />
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
              placeholder={configured ? '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022' : 'AIza...'}
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
              {saving ? 'Saving\u2026' : 'Save'}
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
