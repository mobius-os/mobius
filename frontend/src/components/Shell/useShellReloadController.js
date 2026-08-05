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
  const heldSinceRef = useRef(0)

  // The recent-interaction window is a POLITENESS debounce, not a safety
  // invariant: it exists so an apply does not land in the same breath as the
  // owner's click. It must therefore be able to delay a pending reload by at
  // most one recheck cycle. Left unbounded it is a starvation channel — any
  // source that re-arms `lastInteractionAt` faster than RECENT_SHELL_INTERACTION_MS
  // (a page whose own focus/pointer churn never settles) makes every recheck
  // read "the owner just did something" and the new shell generation NEVER
  // lands, silently and forever. The invariants that actually protect work in
  // progress — a live turn, a visible canvas or multi-pane Builder, a composer
  // holding text, live dictation, a hidden page — are unaffected and keep
  // holding the reload for as long as they are true.
  function interactionGraceSpent() {
    return heldSinceRef.current > 0
      && Date.now() - heldSinceRef.current >= RECHECK_MS
  }

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
      lastUserInteractionAt: interactionGraceSpent()
        ? 0
        : lastInteractionAtRef.current,
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
    const alreadyPending = pendingRef.current
    passiveRef.current = alreadyPending
      ? (passiveRef.current && passive)
      : passive
    // Stamp when this generation first went on hold, so the politeness window
    // above is measured against the reload's age rather than being restarted
    // by every later interaction.
    if (!alreadyPending) heldSinceRef.current = Date.now()
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
    heldSinceRef.current = 0
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
    // `focusin` is deliberately NOT recorded. It fires identically for a real
    // focus change and for the shell's own programmatic `.focus()` calls — the
    // composer focus restore, pane selection, drawer and dialog focus traps,
    // the Settings connection-row restore — so recording it let the page re-arm
    // its own "the owner just did something" window on its own account. Nothing
    // is lost: a user cannot move focus without a pointerdown, a touchstart or
    // a keydown, all of which are still recorded, and typing is separately
    // protected by hasProtectedEditingContent.
    const opts = { capture: true, passive: true }
    win.addEventListener('pointerdown', record, opts)
    win.addEventListener('touchstart', record, opts)
    win.addEventListener('keydown', record, opts)
    win.addEventListener('input', record, opts)
    doc.addEventListener('visibilitychange', releaseWhenHidden)
    return () => {
      win.removeEventListener('pointerdown', record, opts)
      win.removeEventListener('touchstart', record, opts)
      win.removeEventListener('keydown', record, opts)
      win.removeEventListener('input', record, opts)
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
