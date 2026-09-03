import { useCallback, useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import AppCanvas from '../AppCanvas/AppCanvas.jsx'
import { api, BASE, jsonOrThrow } from '../../api/client.js'
import { appQueries } from '../../hooks/queries.js'
import useSystemEventStream from '../../hooks/useSystemEventStream.js'
import { stageComposerHandoff } from '../ChatView/composerDraft.js'
import RecoveryPanel from '../ErrorBoundary/RecoveryPanel.jsx'
import {
  buildAgentRepairPrompt,
  errorRecoveryFingerprint,
  readErrorRecoveryAttempt,
  runAgentRepair,
  writeRefreshedRecoveryAttempt,
} from '../../lib/errorRecovery.js'
import {
  isVisualContentOnly,
  standaloneAppVersion,
} from '../../lib/standaloneBoot.js'
import { readableAppDiagnostic } from '../../lib/appDiagnostic.js'
import { makeAppChatController } from '../../lib/appChatControl.js'
import { handleAppProjectsRequest } from '../../lib/appProjectControl.js'
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
  const repairControllerRef = useRef(null)
  const [installOpen, setInstallOpen] = useState(() => {
    try { return new URLSearchParams(window.location.search).get('install') === '1' }
    catch { return false }
  })
  const navEntriesRef = useRef(readStandaloneHistoryEntries(history.state))
  const localPopRef = useRef(false)
  const appChatControllerRef = useRef(null)
  if (!appChatControllerRef.current) {
    appChatControllerRef.current = makeAppChatController({
      chats: api.chats,
      readJson: jsonOrThrow,
    })
  }

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

  const captureCrash = useCallback((_appId, error) => {
    const message = readableAppDiagnostic(error)
    const fingerprint = errorRecoveryFingerprint(recoverySurfaceKey, message)
    const attempt = readErrorRecoveryAttempt({
      surfaceKey: recoverySurfaceKey,
      fingerprint,
    })
    setCrash({
      error,
      fingerprint,
      attempt,
      repairActive: false,
    })
  }, [recoverySurfaceKey])

  useEffect(() => () => {
    const controller = repairControllerRef.current
    repairControllerRef.current = null
    controller?.abort()
  }, [])

  useEffect(() => {
    if (crashFingerprint) crashHeadingRef.current?.focus()
  }, [crashFingerprint])

  useEffect(() => {
    if (!crashFingerprint) return undefined
    function onPageShow(event) {
      if (!event.persisted) return
      const controller = repairControllerRef.current
      repairControllerRef.current = null
      controller?.abort()
      const attempt = readErrorRecoveryAttempt({
        surfaceKey: recoverySurfaceKey,
        fingerprint: crashFingerprint,
      })
      setCrash(current => current ? {
        ...current,
        attempt,
        repairActive: false,
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
    const run = async () => {
      if (request.type === 'moebius:chat-control') {
        return appChatControllerRef.current(initialApp.id, request)
      }
      if (request.type === 'moebius:projects') {
        return handleAppProjectsRequest({
          app: initialApp,
          client: api.projects,
          readJson: jsonOrThrow,
          browseProjects: () => {
            window.location.href = shellUrl({ projects: 1 })
          },
          openProject: project => {
            window.location.href = shellUrl({ project: project.id })
          },
        }, request)
      }
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
      return null
    }
    const outcome = run()
    if (request.requestId) return outcome
    void outcome.catch(error => captureCrash(
      initialApp.id,
      `Möbius couldn't complete that request. ${readableAppDiagnostic(error)}`,
      null,
    ))
    return undefined
  }, [captureCrash, initialApp])

  const refreshCrash = useCallback(() => {
    if (!crash || repairControllerRef.current) return
    writeRefreshedRecoveryAttempt({
      surfaceKey: recoverySurfaceKey,
      fingerprint: crash.fingerprint,
    })
    window.location.reload()
  }, [crash, recoverySurfaceKey])

  const reportCrash = useCallback(() => {
    if (!crash || repairControllerRef.current) return
    const controller = new AbortController()
    repairControllerRef.current = controller
    setCrash(current => current ? { ...current, repairActive: true } : current)
    void runAgentRepair({
      client: api,
      base: BASE,
      surfaceKey: recoverySurfaceKey,
      fingerprint: crash.fingerprint,
      previousAttempt: crash.attempt,
      signal: controller.signal,
      onAttempt: (attempt) => {
        setCrash(current => current ? {
          ...current,
          attempt,
        } : current)
      },
      prompt: buildAgentRepairPrompt({
        surface: `standalone app ${app.name} (${app.id})`,
        message: readableAppDiagnostic(crash.error),
        componentStack: '',
        pathname: window.location.pathname,
      }),
    }).then(result => {
      window.location.href = result.path
    }).catch(error => {
      if (error?.name === 'AbortError') return
    }).finally(() => {
      if (repairControllerRef.current === controller) {
        repairControllerRef.current = null
        setCrash(current => current ? { ...current, repairActive: false } : current)
      }
    })
  }, [app.id, app.name, crash, recoverySurfaceKey])

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
    <main className="standalone-app">
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
          <RecoveryPanel
            variant="standalone"
            className="standalone-app__crash"
            headingRef={crashHeadingRef}
            title={`${app.name} stopped unexpectedly`}
            subject="app"
            diagnostic={readableAppDiagnostic(crash.error)}
            attempt={crash.attempt}
            repairActive={crash.repairActive}
            refreshLabel="Refresh app"
            onRefresh={refreshCrash}
            onAgentRepair={reportCrash}
            secondaryAction={{
              href: shellUrl({ app: app.id }),
              label: 'Open in Möbius',
            }}
          />
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
