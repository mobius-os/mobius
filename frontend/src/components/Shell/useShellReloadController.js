import { useCallback, useEffect, useRef } from 'react'
import { replaceNavEntry } from '../../lib/navHistory.js'
import { BEFORE_SHELL_RELOAD_EVENT } from '../../lib/shellReloadEvents.js'
import {
  awaitCacheFlushBeforeReload,
  flushPersistedQueryCache,
} from '../../queryClient.js'
import * as paneModel from './paneModel.js'
import { shouldDeferShellReload } from './shellReloadPolicy.js'
import { reloadWhenWorkerTakesOver } from './swHandoff.js'

const RECHECK_MS = 6000

export function deriveShellReloadState({
  workspace,
  activeView,
  drawerOpen,
}) {
  const content = paneModel.activeContentRoute(workspace)
  return {
    activeView: activeView === 'settings' ? 'settings' : content.view,
    activeAppId: content.appId,
    activeChatId: content.chatId,
    drawerOpen,
  }
}

/**
 * Owns the apply-on-idle shell-generation handoff.
 *
 * All delayed work reads `inputsRef`, so timers and service-worker callbacks
 * cannot serialize a render-time workspace or liveness snapshot. Explicit
 * apply requests promote coalesced passive watcher requests.
 */
export default function useShellReloadController(inputs) {
  const inputsRef = useRef(inputs)
  inputsRef.current = inputs
  const pendingRef = useRef(false)
  const passiveRef = useRef(false)
  const timerRef = useRef(null)
  const lastInteractionAtRef = useRef(0)

  function hasStableVisibleHold(passive) {
    const { doc, multiPaneBuilderVisibleRef, activeViewRef, activeChatIdRef } = inputsRef.current
    if (doc.visibilityState === 'hidden') return false
    if (multiPaneBuilderVisibleRef.current) return true
    return passive
      && activeViewRef.current === 'chat'
      && activeChatIdRef.current != null
  }

  function wouldDisruptUser({ passive = false } = {}) {
    const {
      doc,
      activeViewRef,
      activeChatIdRef,
      multiPaneBuilderVisibleRef,
      streamingChatIdsRef,
      voiceDictationActiveRef,
    } = inputsRef.current
    return shouldDeferShellReload({
      activeElement: doc.activeElement,
      activeView: activeViewRef.current,
      activeChatId: activeChatIdRef.current,
      multiPaneBuilderVisible: multiPaneBuilderVisibleRef.current,
      streamingChatIds: streamingChatIdsRef.current,
      passiveRebuild: passive,
      voiceDictationActive: voiceDictationActiveRef.current,
      lastUserInteractionAt: lastInteractionAtRef.current,
      visibilityState: doc.visibilityState,
    })
  }

  function scheduleCheck() {
    const { win } = inputsRef.current
    if (timerRef.current) win.clearTimeout(timerRef.current)
    timerRef.current = win.setTimeout(() => {
      timerRef.current = null
      checkPendingImpl()
    }, RECHECK_MS)
  }

  function deferReload({ passive = false } = {}) {
    passiveRef.current = pendingRef.current
      ? (passiveRef.current && passive)
      : passive
    pendingRef.current = true
    if (!hasStableVisibleHold(passiveRef.current)) scheduleCheck()
  }

  async function performReload({ passive = false } = {}) {
    const {
      win,
      nav,
      storage,
      queryClient,
      persistWorkspaceSnapshot,
      workspaceStateRef,
      activeViewRef,
      drawerOpenRef,
    } = inputsRef.current
    let stalePrecache = false
    try { stalePrecache = storage.getItem('sw-stale-precache-pending') === '1' } catch { /* ignore */ }
    if (stalePrecache && nav.onLine === false) {
      deferReload({ passive })
      return
    }
    pendingRef.current = false
    passiveRef.current = false
    if (timerRef.current) {
      win.clearTimeout(timerRef.current)
      timerRef.current = null
    }

    win.dispatchEvent(new win.Event(BEFORE_SHELL_RELOAD_EVENT))
    await awaitCacheFlushBeforeReload(flushPersistedQueryCache(queryClient))
    persistWorkspaceSnapshot()
    storage.setItem('shell-reload', JSON.stringify(deriveShellReloadState({
      workspace: workspaceStateRef.current.ws,
      activeView: activeViewRef.current,
      drawerOpen: drawerOpenRef.current,
    })))
    replaceNavEntry('base', '/shell/')
    try { storage.setItem('sw-skip-initiated', '1') } catch { /* ignore */ }

    if (stalePrecache) {
      // Let the new worker replace its precache during activation. Deleting the
      // active generation first leaves a failed reload with no Möbius document.
      try { storage.removeItem('sw-stale-precache-pending') } catch { /* ignore */ }
      try { storage.setItem('sw-stale-precache-recovering', '1') } catch { /* ignore */ }
    }

    const reload = () => win.location.reload()
    if (nav.serviceWorker?.getRegistration) {
      nav.serviceWorker.getRegistration()
        .then(registration => reloadWhenWorkerTakesOver({
          registration,
          serviceWorker: nav.serviceWorker,
          reload,
        }))
        .catch(reload)
    } else {
      reload()
    }
  }

  function checkPendingImpl() {
    if (!pendingRef.current) return
    const passive = passiveRef.current
    if (wouldDisruptUser({ passive })) {
      if (!hasStableVisibleHold(passive)) scheduleCheck()
      return
    }
    performReload({ passive })
  }

  const checkPendingShellReload = useCallback(() => {
    checkPendingImpl()
  }, [])

  const requestShellReload = useCallback(({ passive = false } = {}) => {
    if (wouldDisruptUser({ passive })) {
      deferReload({ passive })
    } else {
      performReload({ passive })
    }
  }, [])

  useEffect(() => {
    const { win, doc } = inputsRef.current
    const record = () => { lastInteractionAtRef.current = Date.now() }
    const releaseWhenHidden = () => {
      if (doc.visibilityState === 'hidden') checkPendingImpl()
    }
    const opts = { capture: true, passive: true }
    win.addEventListener('pointerdown', record, opts)
    win.addEventListener('touchstart', record, opts)
    win.addEventListener('keydown', record, opts)
    win.addEventListener('input', record, opts)
    win.addEventListener('focusin', record, opts)
    doc.addEventListener('visibilitychange', releaseWhenHidden)
    return () => {
      win.removeEventListener('pointerdown', record, opts)
      win.removeEventListener('touchstart', record, opts)
      win.removeEventListener('keydown', record, opts)
      win.removeEventListener('input', record, opts)
      win.removeEventListener('focusin', record, opts)
      doc.removeEventListener('visibilitychange', releaseWhenHidden)
      if (timerRef.current) win.clearTimeout(timerRef.current)
    }
  }, [])

  useEffect(() => {
    checkPendingImpl()
  }, [
    inputs.activeView,
    inputs.activeChatId,
    inputs.multiPaneBuilderVisible,
  ])

  return { requestShellReload, checkPendingShellReload }
}
