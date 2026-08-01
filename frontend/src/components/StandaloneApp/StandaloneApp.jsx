import { useCallback, useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import AppCanvas from '../AppCanvas/AppCanvas.jsx'
import { api, BASE } from '../../api/client.js'
import { appQueries } from '../../hooks/queries.js'
import useSystemEventStream from '../../hooks/useSystemEventStream.js'
import { stageComposerHandoff } from '../ChatView/composerDraft.js'
import RecoveryLink from '../ErrorBoundary/RecoveryLink.jsx'
import {
  buildAgentRepairPrompt,
  createRepairIdentity,
  errorRecoveryFingerprint,
  readErrorRecoveryAttempt,
  recoveryActionPolicy,
  recoveryPhaseForAttempt,
  repairChatPath,
  startAgentRepair,
  writeErrorRecoveryAttempt,
  writeRefreshedRecoveryAttempt,
} from '../../lib/errorRecovery.js'
import {
  isVisualContentOnly,
  standaloneAppVersion,
} from '../../lib/standaloneBoot.js'
import { readableAppDiagnostic } from '../../lib/appDiagnostic.js'
import {
  MAX_STANDALONE_HISTORY_ENTRIES,
  readStandaloneHistoryEntries,
  reconcileStandaloneHistory,
  standaloneHistoryState,
} from '../../lib/standaloneHistory.js'
import StandaloneInstallCard from './StandaloneInstallCard.jsx'
import './StandaloneApp.css'

function shellUrl(params = {}) {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value))
  }
  return `/shell/${query.size ? `?${query}` : ''}`
}

export default function StandaloneApp({ initialApp }) {
  const queryClient = useQueryClient()
  const canvasRef = useRef(null)
  const crashHeadingRef = useRef(null)
  const visualContentOnly = isVisualContentOnly()
  const [app, setApp] = useState(initialApp)
  const [updateAvailable, setUpdateAvailable] = useState(false)
  const [removed, setRemoved] = useState(false)
  const [immersive, setImmersive] = useState(false)
  const [crash, setCrash] = useState(null)
  const crashFingerprint = crash?.fingerprint || null
  const recoverySurfaceKey = `standalone-app:${initialApp.id}`
  const repairStartingRef = useRef(false)
  const repairControllerRef = useRef(null)
  const [installOpen, setInstallOpen] = useState(() => {
    try { return new URLSearchParams(window.location.search).get('install') === '1' }
    catch { return false }
  })
  const navEntriesRef = useRef(readStandaloneHistoryEntries(history.state))
  const localPopRef = useRef(false)

  const refreshApp = useCallback(async ({ apply = false } = {}) => {
    const rows = await queryClient.fetchQuery({
      queryKey: appQueries.list.key,
      queryFn: appQueries.list.fetch,
      staleTime: 0,
    })
    const current = rows.find(row => String(row.id) === String(initialApp.id))
    if (!current) {
      setRemoved(true)
      return null
    }
    setRemoved(false)
    if (apply) {
      setApp(current)
      setUpdateAvailable(false)
    } else if (standaloneAppVersion(current) !== standaloneAppVersion(app)) {
      setUpdateAvailable(true)
    }
    return current
  }, [app, initialApp.id, queryClient])

  const captureCrash = useCallback((_appId, error, chatId) => {
    const message = readableAppDiagnostic(error)
    const fingerprint = errorRecoveryFingerprint(recoverySurfaceKey, message)
    const attempt = readErrorRecoveryAttempt({
      surfaceKey: recoverySurfaceKey,
      fingerprint,
    })
    setCrash({
      error,
      chatId,
      fingerprint,
      phase: recoveryPhaseForAttempt(attempt),
      attemptPhase: attempt?.phase || null,
      repairChatId: attempt?.chatId || null,
      repairRequestId: attempt?.repairRequestId || null,
      messageCid: attempt?.messageCid || null,
      agentError: attempt?.phase === 'agent-failed'
        ? 'Repair chat request failed.'
        : '',
    })
  }, [recoverySurfaceKey])

  useEffect(() => () => repairControllerRef.current?.abort(), [])

  useEffect(() => {
    if (crashFingerprint) crashHeadingRef.current?.focus()
  }, [crashFingerprint])

  useEffect(() => {
    if (!crashFingerprint) return undefined
    function onPageShow(event) {
      if (!event.persisted) return
      repairStartingRef.current = false
      repairControllerRef.current = null
      const attempt = readErrorRecoveryAttempt({
        surfaceKey: recoverySurfaceKey,
        fingerprint: crashFingerprint,
      })
      setCrash(current => current ? {
        ...current,
        phase: recoveryPhaseForAttempt(attempt),
        attemptPhase: attempt?.phase || null,
        repairChatId: attempt?.chatId || null,
        repairRequestId: attempt?.repairRequestId || null,
        messageCid: attempt?.messageCid || null,
        agentError: attempt?.phase === 'agent-failed'
          ? 'Repair chat request failed.'
          : '',
      } : current)
    }
    window.addEventListener('pageshow', onPageShow)
    return () => window.removeEventListener('pageshow', onPageShow)
  }, [crashFingerprint, recoverySurfaceKey])

  useSystemEventStream(useCallback((event) => {
    if (String(event.appId || '') !== String(initialApp.id)) return
    if (event.type === 'app_deleted') {
      setRemoved(true)
    } else if (['app_updated', 'app_recovered', 'app_preview_ready'].includes(event.type)) {
      setRemoved(false)
      setUpdateAvailable(true)
    }
  }, [initialApp.id]), {
    // Reconnect reconciliation is best-effort; the next system event or
    // explicit update tap retries it without leaking a rejected promise.
    onOpen: () => { void refreshApp().catch(() => {}) },
  })

  useEffect(() => {
    // Upgrade the current entry so reloads and multi-step browser history UI
    // retain the complete logical stack. Legacy depth-only entries remain
    // readable when a user traverses into them.
    try {
      history.replaceState(
        standaloneHistoryState(history.state, navEntriesRef.current),
        '',
        window.location.href,
      )
    } catch {}

    function onPopState(event) {
      const result = reconcileStandaloneHistory(
        navEntriesRef.current,
        event.state,
        { localPopPending: localPopRef.current },
      )
      navEntriesRef.current = result.entries
      if (result.consumedLocalPop) localPopRef.current = false
      for (const command of result.commands) {
        canvasRef.current?.sendNavigation(command.direction, command.requestId)
      }
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  const onNavPush = useCallback((_appId, meta = {}) => {
    if (navEntriesRef.current.length >= MAX_STANDALONE_HISTORY_ENTRIES) return false
    const entry = {
      requestId: typeof meta.requestId === 'string' ? meta.requestId : null,
      reversible: meta.reversible === true,
    }
    navEntriesRef.current.push(entry)
    try {
      history.pushState(
        standaloneHistoryState(history.state, navEntriesRef.current),
        '',
        window.location.href,
      )
      return true
    } catch {
      navEntriesRef.current.pop()
      return false
    }
  }, [])

  const onNavPop = useCallback(() => {
    if (!navEntriesRef.current.length || localPopRef.current) return
    localPopRef.current = true
    history.back()
  }, [])

  const onHostRequest = useCallback((_appId, request) => {
    void (async () => {
      if (request.type === 'moebius:open-app') {
        window.location.href = shellUrl({ app: request.appId, intent: request.intent })
        return
      }
      if (request.type === 'moebius:open-settings') {
        // Settings focus is a workspace concern. Leave this PWA scope and open
        // the trusted workspace rather than growing a second settings router.
        window.location.href = shellUrl()
        return
      }
      if (request.type === 'moebius:open-chat') {
        stageComposerHandoff(request.chatId, request.draft)
        window.location.href = shellUrl({ chat: request.chatId })
        return
      }
      if (request.type === 'moebius:new-chat') {
        const response = await api.chats.create({})
        if (!response.ok) throw new Error(`chat create ${response.status}`)
        const chat = await response.json()
        stageComposerHandoff(chat.id, request.draft, { autoSend: request.autoSend })
        window.location.href = shellUrl({ chat: chat.id })
      }
    })().catch(error => captureCrash(
      initialApp.id,
      `Möbius couldn't complete that request. ${readableAppDiagnostic(error)}`,
      null,
    ))
  }, [captureCrash, initialApp.id])

  const refreshCrash = useCallback(() => {
    if (!crash || repairStartingRef.current) return
    writeRefreshedRecoveryAttempt({
      surfaceKey: recoverySurfaceKey,
      fingerprint: crash.fingerprint,
    })
    window.location.reload()
  }, [crash, recoverySurfaceKey])

  const reportCrash = useCallback(() => {
    if (!crash || repairStartingRef.current) return
    const identity = createRepairIdentity(crash)
    let createdChatId = crash.repairChatId || null
    repairStartingRef.current = true
    repairControllerRef.current = new AbortController()
    const startingAttempt = {
      phase: 'agent-starting',
      chatId: crash.repairChatId,
      repairRequestId: identity.repairRequestId,
      messageCid: identity.messageCid,
    }
    writeErrorRecoveryAttempt({
      surfaceKey: recoverySurfaceKey,
      fingerprint: crash.fingerprint,
      ...startingAttempt,
    })
    setCrash(current => current ? {
      ...current,
      phase: 'agent-starting',
      attemptPhase: 'agent-starting',
      repairRequestId: identity.repairRequestId,
      messageCid: identity.messageCid,
      agentError: '',
    } : current)
    void startAgentRepair({
      client: api,
      base: BASE,
      repairRequestId: identity.repairRequestId,
      messageCid: identity.messageCid,
      signal: repairControllerRef.current.signal,
      onChatCreated: chatId => {
        createdChatId = chatId
        writeErrorRecoveryAttempt({
          surfaceKey: recoverySurfaceKey,
          fingerprint: crash.fingerprint,
          ...startingAttempt,
          chatId,
        })
        setCrash(current => current ? { ...current, repairChatId: chatId } : current)
      },
      prompt: buildAgentRepairPrompt({
        surface: `standalone app ${app.name} (${app.id})`,
        message: readableAppDiagnostic(crash.error),
        componentStack: '',
        pathname: window.location.pathname,
      }),
    }).then(result => {
      writeErrorRecoveryAttempt({
        surfaceKey: recoverySurfaceKey,
        fingerprint: crash.fingerprint,
        phase: 'agent-directed',
        chatId: result.chatId,
        repairRequestId: identity.repairRequestId,
        messageCid: identity.messageCid,
      })
      window.location.href = result.path
    }).catch(error => {
      repairStartingRef.current = false
      repairControllerRef.current = null
      if (error?.name === 'AbortError') return
      writeErrorRecoveryAttempt({
        surfaceKey: recoverySurfaceKey,
        fingerprint: crash.fingerprint,
        phase: 'agent-failed',
        chatId: createdChatId,
        repairRequestId: identity.repairRequestId,
        messageCid: identity.messageCid,
      })
      setCrash(current => current ? {
        ...current,
        phase: 'recovery',
        attemptPhase: 'agent-failed',
        repairChatId: createdChatId,
        agentError: 'Repair chat request failed.',
      } : current)
    })
  }, [app.id, app.name, crash, recoverySurfaceKey])

  const crashActions = crash ? recoveryActionPolicy({
    phase: crash.phase,
    attemptPhase: crash.attemptPhase,
    repairChatId: crash.repairChatId,
  }) : null

  if (removed) {
    return (
      <main className="standalone-app standalone-app--message">
        <section>
          <h1>{app.name} is no longer installed</h1>
          <p>Open Möbius to recover it or choose another app.</p>
          <a href="/shell/">Open Möbius</a>
        </section>
      </main>
    )
  }

  return (
    <main className={`standalone-app${immersive ? ' standalone-app--immersive' : ''}${crash ? ' standalone-app--crashed' : ''}`}>
      <AppCanvas
        ref={canvasRef}
        appId={app.id}
        appName={app.name}
        appSlug={app.slug}
        version={standaloneAppVersion(app)}
        offlineCapable={app.offline_capable === true}
        capabilityContract={app.capability_contract || null}
        active
        visible
        interactive={!crash}
        immersive={immersive}
        onImmersive={(_id, value) => setImmersive(value)}
        onNavPush={onNavPush}
        onNavPop={onNavPop}
        onHostRequest={onHostRequest}
        onAppError={captureCrash}
      />

      {!crash && (
        <a
          className="standalone-app__mobius-link"
          href={shellUrl({ app: app.id })}
          aria-label={`Open ${app.name} in Möbius`}
        >
          <span aria-hidden="true">∞</span>
          <span>Open in Möbius</span>
        </a>
      )}

      {updateAvailable && !crash && (
        <button
          className="standalone-app__update"
          type="button"
          onClick={() => { void refreshApp({ apply: true }).catch(() => {}) }}
        >
          <span aria-hidden="true">↻</span>
          Updated — tap to refresh
        </button>
      )}

      {crash && (
        <div className="standalone-app__crash-layer">
          <section
            className="standalone-app__crash"
            aria-labelledby="standalone-crash-title"
          >
            <h1 id="standalone-crash-title" ref={crashHeadingRef} tabIndex={-1}>
              {app.name} stopped unexpectedly
            </h1>
            <p>
              {crash.phase === 'refresh' && 'This app hit an unexpected error. Refreshing won’t delete your chats.'}
              {(crash.phase === 'agent' || crash.phase === 'agent-starting') && 'Refreshing didn’t fix the app. Möbius can start a new repair chat and share these technical details with your agent to investigate and fix it.'}
              {crash.phase === 'recovery' && crash.attemptPhase === 'agent-directed' && 'The repair chat started, but the app still can’t open. System recovery is the remaining fallback.'}
              {crash.phase === 'recovery' && crash.attemptPhase === 'agent-failed' && 'The repair chat couldn’t start. You can retry it or use system recovery as a last resort.'}
            </p>
            <details className="standalone-app__details">
              <summary>Technical details</summary>
              <pre>{readableAppDiagnostic(crash.error)}</pre>
            </details>
            {(crash.phase === 'agent-starting' || crash.agentError) && (
              <p
                className={`standalone-app__crash-status${crash.agentError ? ' standalone-app__crash-status--sr-only' : ''}`}
                role="status"
                aria-live="polite"
              >
                {crash.phase === 'agent-starting' ? 'Starting the repair chat. This may take a moment.' : crash.agentError}
              </p>
            )}
            <div
              className="standalone-app__crash-actions"
              aria-busy={crash.phase === 'agent-starting' ? true : undefined}
            >
              <a href={shellUrl({ app: app.id })}>Open in Möbius</a>
              {crashActions.showRefreshAgain && (
                <button type="button" onClick={refreshCrash}>Refresh again</button>
              )}
              {crashActions.showOpenRepairChat && (
                <a href={repairChatPath(crash.repairChatId, BASE)}>Open repair chat</a>
              )}
              {(crash.phase === 'refresh' || crashActions.showAskAgent || crashActions.showRetryAgent || crash.phase === 'agent-starting') && (
                <button
                  type="button"
                  className="standalone-app__crash-primary"
                  onClick={crash.phase === 'refresh' ? refreshCrash : reportCrash}
                  disabled={crash.phase === 'agent-starting'}
                >
                  {crash.phase === 'refresh' && 'Refresh app'}
                  {crash.phase === 'agent' && (crash.attemptPhase === 'agent-starting' ? 'Resume repair chat' : 'Start repair chat')}
                  {crash.phase === 'agent-starting' && 'Starting repair chat…'}
                  {crash.phase === 'recovery' && 'Retry repair chat'}
                </button>
              )}
            </div>
            {crashActions.showRecovery && (
              <RecoveryLink
                className="standalone-app__recovery"
                lead="If the repair chat can’t get you back in,"
              />
            )}
          </section>
        </div>
      )}

      {!crash && !visualContentOnly && (
        <StandaloneInstallCard
          app={app}
          forceOpen={installOpen}
          onClose={() => {
            setInstallOpen(false)
            try {
              const url = new URL(window.location.href)
              url.searchParams.delete('install')
              history.replaceState(history.state, '', url)
            } catch {}
          }}
        />
      )}
    </main>
  )
}
