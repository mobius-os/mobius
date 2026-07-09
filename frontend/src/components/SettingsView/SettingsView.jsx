import { useState, useEffect, useCallback, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Switch } from '@openai/apps-sdk-ui/components/Switch'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { TextLink } from '@openai/apps-sdk-ui/components/TextLink'
import { api } from '../../api/client.js'
import { authQueries, settingsQueries, themeQueries, versionQueries } from '../../hooks/queries.js'
import * as themeService from '../../lib/themeService.js'
import ProviderAuth from '../ProviderAuth/ProviderAuth.jsx'
import CodexAuth from '../ProviderAuth/CodexAuth.jsx'
import ProviderRow from '../ProviderAuth/ProviderRow.jsx'
import StatusDot from '../ui/StatusDot.jsx'
import '../ui/StatusDot.css'
import './SettingsView.css'

const UPDATE_CHECKED_RESET_MS = 2200

export default function SettingsView({ onThemeChange, onOpenChat }) {
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
  // A manual restart interrupts any live chat, so it's a deliberate two-step:
  // the first tap arms the confirm, the second actually restarts.
  const [restartConfirm, setRestartConfirm] = useState(false)
  // Platform self-update (backend/frontend/libraries/recovery as one release).
  // 'idle' | 'applying' | 'restarting'.
  const [platform, setPlatform] = useState(null)
  const [platformPhase, setPlatformPhase] = useState('idle')
  const [platformError, setPlatformError] = useState('')
  // 'idle' | 'checking' | 'checked' — the "Check for updates" button asks the
  // service worker to re-check cached frontend assets and re-reads
  // /api/version. 'checked' is a short-lived success label when no update is
  // available.
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
          setRestartConfirm(false)
        }
      }, POLL_INTERVAL_MS)
    } catch (err) {
      setRestartPhase('idle')
      setRestartConfirm(false)
      setRestartError(err.message || 'Restart request failed.')
    }
  }

  // Refresh both Möbius update signals on demand: the service worker cache and
  // platform git availability. `registration.update()` forces a fresh fetch of
  // /sw.js (served `no-cache`) and we re-read /api/version for the current
  // served identity. Platform: POST /platform/check runs the `git fetch` that the cheap
  // /status read deliberately skips, so a deploy that landed since boot becomes
  // visible without waiting for a reboot. Both run in parallel and neither
  // failing blocks the other (allSettled) — a hiccup just leaves the last-known
  // state on the row.
  async function checkForUpdates() {
    if (updatePhase === 'checking') return
    setUpdatePhase('checking')
    try {
      const frontendP = (async () => {
        if ('serviceWorker' in navigator) {
          const reg = await navigator.serviceWorker.getRegistration()
          if (reg) await reg.update()
        }
        await versionQueries.current.invalidate(queryClient)
        await versionQuery.refetch()
      })()
      const platformP = (async () => {
        const res = await api.platform.check()
        if (res.ok) setPlatform(await res.json())
      })()
      await Promise.allSettled([frontendP, platformP])
    } finally {
      setUpdatePhase('checked')
    }
  }

  // Reload onto the FRESH service worker so the new precache — including the
  // content-hashed /mobius-runtime.js — serves the reloaded page. sw.js calls
  // skipWaiting() + clientsClaim(), so a newly-installed worker activates on its
  // own; we just wait for it to take control before reloading, instead of racing
  // a reload the OLD worker serves with the stale runtime (which leaves
  // window.mobius missing durableWrite/createUseDocument and breaks migrated
  // mini-apps until the tab happens to reload onto the new worker).
  async function reloadOntoFreshSW() {
    if ('serviceWorker' in navigator) {
      try {
        const reg = await navigator.serviceWorker.getRegistration()
        if (reg) {
          await reg.update()
          if (reg.installing || reg.waiting) {
            await new Promise((resolve) => {
              navigator.serviceWorker.addEventListener('controllerchange', resolve, { once: true })
              setTimeout(resolve, 5000) // never hang if the worker never swaps
            })
          }
        }
      } catch { /* fall through to a plain reload */ }
    }
    window.location.reload()
  }

  // Platform self-update: read availability once on mount so Settings can show
  // "new update available" without a polling daemon.
  const refreshPlatform = useCallback(async () => {
    try {
      const res = await api.platform.status()
      if (res.ok) setPlatform(await res.json())
    } catch {
      // A status hiccup just leaves the section hidden — never blocks Settings.
    }
  }, [])
  useEffect(() => { refreshPlatform() }, [refreshPlatform])

  // Silent freshen on opening Settings: ask the SW for a newer cache manifest
  // and re-read /api/version so the Möbius row's served identity is current the
  // moment it renders. This is the cheap on-open pass (no git fetch) — it
  // complements the explicit "Check for updates" button, which additionally
  // fetches platform availability. Once per open; never touches updatePhase, so
  // it can't make the Update/Check button look busy.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        if ('serviceWorker' in navigator) {
          const reg = await navigator.serviceWorker.getRegistration()
          if (reg) await reg.update()
        }
        if (cancelled) return
        await versionQueries.current.invalidate(queryClient)
        await versionQuery.refetch()
      } catch {
        // a refresh hiccup just leaves the last-known status on the row
      }
    })()
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Press Update -> merge the baked platform release into the live backend.
  // Clean -> the row flips to "restart needed"; conflict -> an agent chat opens.
  async function applyPlatformUpdate() {
    if (platformPhase !== 'idle') return
    setPlatformError('')
    setPlatformPhase('applying')
    try {
      const res = await api.platform.apply()
      if (!res.ok) {
        let detail = ''
        try { detail = (await res.json())?.detail || '' } catch {}
        setPlatformError(detail ? `Update failed: ${detail}` : 'Update failed — the instance is unchanged.')
        return
      }
      await refreshPlatform()
    } catch {
      setPlatformError('Update failed — the instance is unchanged.')
    } finally {
      setPlatformPhase('idle')
    }
  }

  // One "Update" button for the platform updater. A clean merge marks
  // restart-needed, so the row advances to "Restart to finish"; that restart
  // reloads onto the fresh service worker too.
  async function applyMobiusUpdate() {
    if (platform?.available) {
      await applyPlatformUpdate()
    }
  }

  // Poll /api/health until the worker is back, then reload. Shared by the
  // platform restart-to-finish flow (reuses restartPollRef + its unmount clear).
  function pollHealthThenReload(onTimeout) {
    const POLL_INTERVAL_MS = 1500
    const POLL_MAX = 30
    let attempts = 0
    restartPollRef.current = setInterval(async () => {
      attempts += 1
      try {
        const probe = await fetch('/api/health', { cache: 'no-store' })
        if (probe.ok) {
          clearInterval(restartPollRef.current)
          restartPollRef.current = null
          reloadOntoFreshSW()
          return
        }
      } catch {
        // still down — keep polling
      }
      if (attempts >= POLL_MAX) {
        clearInterval(restartPollRef.current)
        restartPollRef.current = null
        onTimeout()
      }
    }, POLL_INTERVAL_MS)
  }

  // The owner's explicit confirmation that finishes a platform update: clicking
  // this IS the confirm — nothing restarts on its own.
  async function restartToFinish() {
    if (platformPhase === 'restarting') return
    setPlatformError('')
    setPlatformPhase('restarting')
    try {
      // apiFetch resolves for any non-401, so a 5xx is NOT a thrown error —
      // check res.ok or we'd poll + reload onto the same (unchanged) code and
      // report a non-restart as success. Mirrors applyPlatformUpdate's guard.
      const res = await api.platform.restart()
      if (!res.ok) {
        let detail = ''
        try { detail = (await res.json())?.detail || '' } catch {}
        setPlatformError(detail ? `Restart failed: ${detail}` : 'Restart signal failed.')
        setPlatformPhase('idle')
        return
      }
      pollHealthThenReload(() =>
        setPlatformError("Server hasn't come back yet — check the container."))
    } catch {
      setPlatformError('Restart signal failed.')
      setPlatformPhase('idle')
    }
  }

  const version = versionQuery.data
  // The short SHA of the served platform tree. Fall back to the image build
  // sha and finally 'unknown'. First 7 chars, matching the shell version row.
  const mobiusBuildSha = (() => {
    const raw = version?.served_sha && version.served_sha !== 'unknown'
      ? version.served_sha
      : version?.sha && version.sha !== 'unknown'
        ? version.sha
        : null
    return raw ? raw.slice(0, 7) : 'unknown'
  })()
  // The commit date (YYYY-MM-DD) baked at build time, shown next to the sha as
  // a human "version · date". Null on a dev build that didn't stamp it.
  const buildDate = version?.build_date && version.build_date !== 'unknown'
    ? version.build_date
    : null
  // Derived state for the single "Möbius" update row (see the section below).
  const platformConflict = platform?.state === 'conflict'
  // A text-clean update that failed the post-rebase import probe was rolled back
  // to the previous served version — the update is still available, but its last
  // apply needs a repair pass, so the row says so distinctly rather than reading
  // as a plain "New update available".
  const platformRolledBack = platform?.state === 'rolled_back'
  const platformRestart = !!platform?.needs_restart
  const updateAvailable = !!platform?.available
  const mobiusUpdating =
    platformPhase === 'applying' || updatePhase === 'checking'
  const checkUpdatesLabel =
    updatePhase === 'checking'
      ? 'Checking…'
      : updatePhase === 'checked'
        ? 'No updates found'
        : 'Check for updates'

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

        {/* ONE honest "Möbius" update surface. Folds platform availability,
            conflict repair, and restart-to-finish into a single row: one status
            line, one action. Priority: an in-progress conflict, then a pending
            restart, then an available update, then up to date. Per-layer
            mechanics stay invisible (the SW + platform_update.py). Always
            renders (it carries the Möbius build identity), so there is no second
            surface. The action slot holds exactly one contextual button; when
            up to date that button is an explicit "Check for updates" (git fetch
            + SW re-check) with its own "Checking…" text — it never mutates the
            status label beside it. */}
        <section className="settings__section settings__section--compact">
          <h2 className="settings__section-title">Möbius</h2>
          <div className="settings__row settings__row--top">
            <div className="settings__update">
              <StatusDot
                color={platformConflict || platformRolledBack || platformRestart || updateAvailable ? '--accent' : '--green'}
              >
                {platformConflict
                  ? 'Resolving a conflict'
                  : platformRolledBack
                    ? 'Update needs repair'
                    : platformRestart
                      ? 'Restart to finish'
                      : updateAvailable
                        ? 'New update available'
                        : 'Up to date'}
              </StatusDot>
              {/* The honest build identity is the served platform commit when
                  available, falling back to the image build stamp. Hidden on a
                  dev build where no sha is stamped. */}
              {mobiusBuildSha !== 'unknown' && (
                <p className="settings__build">
                  {mobiusBuildSha}{buildDate ? ` · ${buildDate}` : ''}
                </p>
              )}
            </div>
            {platformConflict ? (
              platform?.conflict_chat_id && onOpenChat ? (
                <button
                  className="settings__btn settings__btn--outline settings__btn--sm settings__btn--nowrap"
                  type="button"
                  onClick={() => onOpenChat(platform.conflict_chat_id)}
                >
                  Open chat
                </button>
              ) : null
            ) : platformRestart ? (
              <button
                className="settings__btn settings__btn--sm settings__btn--nowrap"
                type="button"
                onClick={restartToFinish}
                disabled={platformPhase === 'restarting'}
              >
                {/* "Restart to finish", not bare "Restart" — the Server section's
                    standalone "Restart" sits in the same view, so the word
                    "Restart" must mean exactly one thing. This one completes a
                    pending update; the label says so. */}
                {platformPhase === 'restarting' ? 'Restarting…' : 'Restart to finish'}
              </button>
            ) : updateAvailable ? (
              <button
                className="settings__btn settings__btn--sm settings__btn--nowrap"
                type="button"
                onClick={applyMobiusUpdate}
                disabled={mobiusUpdating}
              >
                {mobiusUpdating ? 'Updating…' : 'Update'}
              </button>
            ) : (
              // Nothing to apply — offer an explicit refresh. Subtle outline (a
              // secondary action, not a call-to-action) and its own transient
              // feedback text, so it never mutates the status label beside it.
              // If the check surfaces an update, this slot re-renders to the
              // Update button on the next paint.
              <button
                className="settings__btn settings__btn--outline settings__btn--sm settings__btn--nowrap"
                type="button"
                onClick={checkForUpdates}
                disabled={updatePhase === 'checking'}
              >
                {checkUpdatesLabel}
              </button>
            )}
          </div>
          {platformPhase === 'restarting' && (
            <div className="settings__notice" role="status">
              Restart signal sent. The page will reload shortly.
            </div>
          )}
          {platformError && (
            <Alert color="danger" variant="soft" description={platformError} />
          )}
        </section>

        <section className="settings__section settings__section--compact settings__section--server">
          <h2 className="settings__section-title">Server</h2>
          <div className="settings__row">
            <span className="settings__label">Restart</span>
            {restartConfirm ? (
              <div className="settings__confirm">
                <button
                  className="settings__btn settings__btn--outline settings__btn--sm"
                  type="button"
                  onClick={() => setRestartConfirm(false)}
                  disabled={restartPhase === 'restarting'}
                >
                  Cancel
                </button>
                <button
                  className="settings__btn settings__btn--sm settings__btn--nowrap"
                  type="button"
                  onClick={restartServer}
                  disabled={restartPhase === 'restarting'}
                >
                  {restartPhase === 'restarting' ? 'Restarting…' : 'Restart now'}
                </button>
              </div>
            ) : (
              <button
                className="settings__btn settings__btn--outline settings__btn--sm"
                type="button"
                onClick={() => { setRestartError(''); setRestartConfirm(true) }}
              >
                Restart
              </button>
            )}
          </div>
          {restartConfirm && restartPhase !== 'restarting' && (
            <p className="settings__subtext settings__subtext--tight">
              Restarting interrupts any chat that's mid-response.
            </p>
          )}
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
          <div className="settings__row settings__row--recovery">
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
