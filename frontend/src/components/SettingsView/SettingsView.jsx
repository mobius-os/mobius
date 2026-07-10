import { useState, useEffect, useCallback, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Switch } from '@openai/apps-sdk-ui/components/Switch'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { TextLink } from '@openai/apps-sdk-ui/components/TextLink'
import { api } from '../../api/client.js'
import { authQueries, settingsQueries, themeQueries, versionQueries } from '../../hooks/queries.js'
import { SHELL_BUILD } from '../../lib/buildInfo.js'
import { restartCanReload } from '../../lib/restartReadiness.js'
import * as themeService from '../../lib/themeService.js'
import ProviderAuth from '../ProviderAuth/ProviderAuth.jsx'
import CodexAuth from '../ProviderAuth/CodexAuth.jsx'
import ProviderRow from '../ProviderAuth/ProviderRow.jsx'
import StatusDot from '../ui/StatusDot.jsx'
import '../ui/StatusDot.css'
import './SettingsView.css'

const UPDATE_CHECKED_RESET_MS = 2200
const RESTART_POLL_INTERVAL_MS = 1500
const RESTART_POLL_MAX = 40
const RESTART_SHELL_READY_PATH = '/shell/'

async function readRestartHealth() {
  const response = await fetch('/api/health', {
    cache: 'no-store',
    credentials: 'same-origin',
  })
  if (!response.ok) return { ok: false, bootId: '' }
  let body = null
  try { body = await response.json() } catch {}
  return {
    ok: true,
    bootId: typeof body?.boot_id === 'string' ? body.boot_id : '',
  }
}

async function readRestartBootId() {
  try {
    const health = await readRestartHealth()
    return health.ok ? health.bootId : ''
  } catch {
    return ''
  }
}

async function shellDocumentReady() {
  if (typeof window === 'undefined') return false
  try {
    const response = await fetch(new URL(RESTART_SHELL_READY_PATH, window.location.origin), {
      cache: 'no-store',
      credentials: 'same-origin',
      headers: { Accept: 'text/html' },
    })
    if (!response.ok) return false
    return (response.headers.get('content-type') || '').toLowerCase().includes('text/html')
  } catch {
    return false
  }
}

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
    if (updatePhase !== 'checked') return undefined
    const timer = window.setTimeout(() => setUpdatePhase('idle'), UPDATE_CHECKED_RESET_MS)
    return () => window.clearTimeout(timer)
  }, [updatePhase])

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

  // Ref to track the active health-poll timeout so we can cancel it on
  // component unmount or on a second restart attempt.
  const restartPollRef = useRef(null)
  const clearRestartPoll = useCallback(() => {
    if (!restartPollRef.current) return
    window.clearTimeout(restartPollRef.current)
    restartPollRef.current = null
  }, [])
  useEffect(() => {
    return () => clearRestartPoll()
  }, [clearRestartPoll])

  async function restartServer() {
    if (restartPhase === 'restarting') return
    setRestartPhase('restarting')
    setRestartError('')
    try {
      const previousBootId = await readRestartBootId()
      const res = await api.admin.restart()
      if (!res.ok) {
        let detail = ''
        try { detail = (await res.json()).detail || '' } catch {}
        throw new Error(detail || `Restart failed (${res.status})`)
      }
      pollRestartThenReload({
        previousBootId,
        onTimeout: () => {
          setRestartError("Server hasn't come back yet — check the container.")
          setRestartPhase('idle')
        },
      })
    } catch (err) {
      setRestartPhase('idle')
      setRestartError(err.message || 'Restart request failed.')
    }
  }

  async function reloadOntoReadyShell() {
    if ('serviceWorker' in navigator) {
      try {
        const reg = await navigator.serviceWorker.getRegistration()
        if (reg) {
          await reg.update()
          if (reg.installing || reg.waiting) {
            await new Promise((resolve) => {
              navigator.serviceWorker.addEventListener('controllerchange', resolve, { once: true })
              window.setTimeout(resolve, 5000)
            })
          }
        }
      } catch {
        // A service-worker refresh miss should not force a browser error page.
      }
    }
    if (!(await shellDocumentReady())) return false
    window.location.reload()
    return true
  }

  function pollRestartThenReload({ previousBootId = '', onTimeout }) {
    clearRestartPoll()
    const startedAt = Date.now()
    let attempts = 0
    let sawUnavailable = false

    const poll = async () => {
      restartPollRef.current = null
      attempts += 1
      try {
        const health = await readRestartHealth()
        if (!health.ok) {
          sawUnavailable = true
        } else if (restartCanReload({
          previousBootId,
          currentBootId: health.bootId,
          sawUnavailable,
          elapsedMs: Date.now() - startedAt,
        })) {
          const reloaded = await reloadOntoReadyShell()
          if (reloaded) return
        }
      } catch {
        sawUnavailable = true
      }
      if (attempts >= RESTART_POLL_MAX) {
        onTimeout()
        return
      }
      restartPollRef.current = window.setTimeout(poll, RESTART_POLL_INTERVAL_MS)
    }

    restartPollRef.current = window.setTimeout(poll, RESTART_POLL_INTERVAL_MS)
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

  const version = versionQuery.data
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
  const updateStatusLabel = newerBuildInstalled
    ? 'Ready to update'
    : versionQuery.isError && !version
      ? 'Status unavailable'
      : updatePhase === 'checking'
        ? 'Checking updates'
        : 'Up to date'
  const updateStatusColor = newerBuildInstalled
    ? '--accent'
    : versionQuery.isError && !version
      ? '--danger'
      : '--green'
  const updateActionLabel = newerBuildInstalled
    ? 'Reload'
    : updatePhase === 'checking'
      ? 'Checking…'
      : updatePhase === 'checked'
        ? 'No updates found'
        : 'Check'
  const updateBuildLabel = `${SHELL_BUILD}${
    version?.sha && version.sha !== 'unknown'
      ? ` · ${version.sha.slice(0, 7)}`
      : ''
  }`

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
          <div className="settings__row">
            <div>
              <span className="settings__label">Server</span>
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
              Restart signal sent. Keeping this page open until the shell is ready.
            </div>
          )}
          {restartError && (
            <Alert
              color="danger"
              variant="soft"
              description={restartError}
            />
          )}

          <div className="settings__row settings__row--top">
            <div className="settings__update">
              <span className="settings__label">
                Updates
                <StatusDot color={updateStatusColor}>
                  {updateStatusLabel}
                </StatusDot>
              </span>
              <p className="settings__build">{updateBuildLabel}</p>
            </div>
            <button
              className={`settings__btn settings__btn--sm settings__btn--nowrap${newerBuildInstalled ? '' : ' settings__btn--outline'}`}
              type="button"
              onClick={newerBuildInstalled ? reloadOntoReadyShell : checkForUpdates}
              disabled={updatePhase === 'checking'}
            >
              {updateActionLabel}
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
