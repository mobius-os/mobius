import { useState, useEffect, useCallback, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Switch } from '@openai/apps-sdk-ui/components/Switch'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { TextLink } from '@openai/apps-sdk-ui/components/TextLink'
import { api } from '../../api/client.js'
import { authQueries, settingsQueries, themeQueries, versionQueries } from '../../hooks/queries.js'
import { SHELL_BUILD } from '../../lib/buildInfo.js'
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
  const versionQuery = versionQueries.current.useQuery()
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
  // 'idle' | 'checking' | 'checked' — the "Check for updates" button asks the
  // service worker to re-check for a new shell build and re-reads /api/version.
  const [updatePhase, setUpdatePhase] = useState('idle')
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
  // Three-state gate for the AI-providers section, in priority order:
  //
  //   READY   — at least the cached data is present (data !== undefined).
  //             Both queries are persisted to IndexedDB and hydrate
  //             before the network round-trip (see queryClient.js), so on
  //             a re-open we paint from disk instantly and let the
  //             background revalidation update the rows only if something
  //             changed. This is why we gate on `data`, not `isFetched`:
  //             `isFetched` is still false on this mount even when the
  //             cache already holds a value, and gating on it reintroduced
  //             the open-time flash this fix removes.
  //   ERROR   — no data at all AND the fetch failed. A first-ever open
  //             with no persisted cache that errors must say so, not
  //             render the section blank with no indication.
  //   LOADING — no data yet and no error: the initial in-flight fetch.
  const providerReady = settingsQuery.data !== undefined && claudeStatusQuery.data !== undefined
  const providerError =
    !providerReady && (settingsQuery.isError || claudeStatusQuery.isError)
  const providerErrorMsg =
    settingsQuery.error?.message || claudeStatusQuery.error?.message ||
    'Could not load provider settings.'
  const retryProviders = useCallback(() => {
    settingsQuery.refetch()
    claudeStatusQuery.refetch()
  }, [settingsQuery, claudeStatusQuery])

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

    // Derive the direction from what the user ACTUALLY SEES, not from
    // the optimistic `lightMode` state. `lightMode` mirrors
    // themeModeQuery.data, which resolves async through the SW and
    // LAGS the painted theme; trusting it computed the toggle in the
    // wrong direction (e.g. after a dark→light toggle, a follow-up
    // light→dark would re-derive 'light' from the stale state and
    // hand applyThemeToDom the already-current CSS → no-op repaint,
    // leaving the UI stuck). getEffectiveTheme().mode reads
    // <html data-theme> — the authoritative value applyThemeToDom
    // last painted — so the direction is always relative to the
    // visible theme. Fall back to `lightMode` only at very early boot
    // before any theme has been applied (mode === null).
    const eff = themeService.getEffectiveTheme()
    const currentMode = eff?.mode === 'light' || eff?.mode === 'dark'
      ? eff.mode
      : (lightMode ? 'light' : 'dark')
    const newMode = currentMode === 'light' ? 'dark' : 'light'

    // Keep the optimistic switch UI in sync with the direction we
    // just derived from the visible theme (so the knob reflects the
    // target, not a flip of the stale state).
    setLightMode(newMode === 'light')
    setThemeSwitching(true)
    setThemeError('')

    // Delegate the full apply/persist/invalidate dance to
    // themeService — SettingsView keeps only the optimistic UI
    // state (setLightMode + setThemeError) and the
    // catch-rollback. themeService.toggleTheme invalidates both
    // theme queries; AppCanvas's useEffect picks that up and
    // postMessages `moebius:frame-theme` to live iframes.
    try {
      await themeService.toggleTheme(queryClient, currentMode, api)
      onThemeChange?.()
    } catch {
      setLightMode(currentMode === 'light')
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

  // Ref to track the active health-poll interval so we can cancel it on
  // component unmount or on a second restart attempt (shouldn't happen —
  // the button is disabled while restarting, but belt-and-braces).
  const restartPollRef = useRef(null)
  useEffect(() => {
    return () => {
      if (restartPollRef.current) clearInterval(restartPollRef.current)
    }
  }, [])

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
      // Poll /api/health every ~1.5s instead of a fixed 10s blind reload.
      // Reload as soon as the server is back; surface a notice if it hasn't
      // returned within ~45s (30 polls × 1500ms) so the user knows to check
      // the container rather than waiting indefinitely.
      const POLL_INTERVAL_MS = 1500
      const POLL_MAX = 30
      let polls = 0
      restartPollRef.current = setInterval(async () => {
        polls++
        try {
          const probe = await fetch('/api/health', { cache: 'no-store' })
          if (probe.ok) {
            clearInterval(restartPollRef.current)
            restartPollRef.current = null
            window.location.reload()
            return
          }
        } catch (_) { /* server still down — keep polling */ }
        if (polls >= POLL_MAX) {
          clearInterval(restartPollRef.current)
          restartPollRef.current = null
          setRestartError("Server hasn't come back yet — check the container.")
          setRestartPhase('idle')
        }
      }, POLL_INTERVAL_MS)
    } catch (err) {
      setRestartPhase('idle')
      setRestartError(err.message || 'Restart request failed.')
    }
  }

  // Ask the service worker to re-check for a newer shell build and re-read the
  // live build identity. `registration.update()` forces a fresh fetch of
  // /sw.js (which is served `no-cache`); if a new bundle is live the SW
  // installs it in the background and our update flow (skipWaiting +
  // clientsClaim) activates it. We also re-fetch /api/version so the
  // shell_sha !== sha notice reflects the current server state.
  async function checkForUpdates() {
    if (updatePhase === 'checking') return
    setUpdatePhase('checking')
    try {
      if ('serviceWorker' in navigator) {
        const reg = await navigator.serviceWorker.getRegistration()
        if (reg) await reg.update()
      }
      await versionQueries.current.invalidate(queryClient)
      await versionQuery.refetch()
    } catch {
      // A failed SW update check or refetch is non-fatal — fall through to
      // 'checked' so the row still shows the latest known state.
    } finally {
      setUpdatePhase('checked')
    }
  }

  // The "Update" action reuses checkForUpdates so the SW pulls the newest shell
  // build before we reload to activate it — one source of truth for "ask the SW
  // + re-read /api/version", rather than a bare reload that could race a
  // not-yet-installed bundle.
  async function applyUpdate() {
    await checkForUpdates()
    window.location.reload()
  }

  const version = versionQuery.data
  // The short SHA of the SHELL BUILD the served UI came from — shell_sha
  // (the served bundle's image-build SHA) is the truthful one; fall back to
  // sha (the running image) and finally 'unknown'. First 7 chars, matching
  // how the Shell version row truncates sha.
  const shellBuildSha = (() => {
    const raw = version?.shell_sha && version.shell_sha !== 'unknown'
      ? version.shell_sha
      : version?.sha && version.sha !== 'unknown'
        ? version.sha
        : null
    return raw ? raw.slice(0, 7) : 'unknown'
  })()
  // shell_sha is the build the SERVED UI came from; sha is the running image.
  // A mismatch means a newer image is installed but its UI isn't being served
  // to this client yet — reloading picks it up.
  const newerBuildInstalled =
    !!version &&
    version.shell_sha &&
    version.sha &&
    version.shell_sha !== 'unknown' &&
    version.sha !== 'unknown' &&
    version.shell_sha !== version.sha

  return (
    <div className="settings">
      <div className="settings__content">
        <h1 className="settings__title">Settings</h1>

        <section className="settings__section">
          <h2 className="settings__section-title">AI providers</h2>

          {providerReady ? (
            <div className="settings__providers">
              <ProviderRow
                id="codex"
                name="OpenAI Codex"
                showRadio={false}
                connected={codexAuthenticated}
                version={codexVersion}
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
                version={claudeVersion}
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
          ) : providerError ? (
            // First-ever open with no persisted cache and the fetch
            // failed. Surface the error + a retry rather than rendering
            // the section blank — a silent empty section reads as "no
            // providers", which is wrong.
            <Alert
              color="danger"
              variant="soft"
              description={providerErrorMsg}
              actions={
                <button
                  className="settings__btn settings__btn--outline settings__btn--sm"
                  type="button"
                  onClick={retryProviders}
                >
                  Retry
                </button>
              }
            />
          ) : (
            // Loading: no cached data yet and no error — the initial
            // in-flight fetch. Show a neutral notice instead of nothing.
            <div className="settings__notice" role="status">
              Loading providers…
            </div>
          )}
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
          <h2 className="settings__section-title">App</h2>
          {/* Version + update state in ONE calm row. The old toggling
              "Check for updates" button (idle → Checking… → Up to date) mutated
              its own label and shifted the layout on every tap — the disliked
              UX. The version query already knows whether a newer build is
              waiting, so we surface a single clear "Update" action ONLY when one
              is, and otherwise a quiet status. Nothing here changes width/height
              on interaction. */}
          <div className="settings__row settings__row--top">
            <div>
              <span className="settings__label">Möbius</span>
              <p className="settings__subtext settings__subtext--tight">
                {SHELL_BUILD}
                {shellBuildSha !== 'unknown' ? ` · ${shellBuildSha}` : ''}
              </p>
            </div>
            {newerBuildInstalled ? (
              <button
                className="settings__btn settings__btn--sm settings__btn--nowrap"
                type="button"
                onClick={applyUpdate}
                disabled={updatePhase === 'checking'}
              >
                {updatePhase === 'checking' ? 'Updating…' : 'Update'}
              </button>
            ) : (
              <div className="settings__app-status">
                <StatusDot color="--green">Up to date</StatusDot>
                <button
                  className="settings__btn settings__btn--outline settings__btn--sm"
                  type="button"
                  onClick={checkForUpdates}
                  disabled={updatePhase === 'checking'}
                >
                  {updatePhase === 'checking' ? 'Checking…' : 'Check'}
                </button>
              </div>
            )}
          </div>
        </section>

        <section className="settings__section settings__section--compact">
          <h2 className="settings__section-title">Server</h2>
          <div className="settings__row">
            <span className="settings__label">Restart</span>
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
