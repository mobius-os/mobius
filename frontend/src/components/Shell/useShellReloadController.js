import { useCallback, useEffect, useRef } from 'react'
import { replaceNavEntry } from '../../lib/navHistory.js'
import { BEFORE_SHELL_RELOAD_EVENT } from '../../lib/shellReloadEvents.js'
import {
  awaitCacheFlushBeforeReload,
  flushPersistedQueryCache,
} from '../../queryClient.js'
import * as paneModel from './paneModel.js'
import { shouldDeferShellReload } from './shellReloadPolicy.js'
import {
  inspectShellUpdate,
  reloadWhenWorkerTakesOver,
} from '../../lib/shellUpdate.js'

const RECHECK_MS = 6000

export function deriveShellReloadState({
  workspace,
  activeView,
  drawerOpen,
  destination = null,
}) {
  const content = paneModel.activeContentRoute(workspace)
  const destinationView = destination?.view
  const destinationIsSettings = destinationView === 'settings'
  return {
    activeView: destinationIsSettings
      ? 'settings'
      : (destinationView || (activeView === 'settings' ? 'settings' : content.view)),
    activeAppId: destination && !destinationIsSettings
      ? (destination.appId ?? null)
      : content.appId,
    activeChatId: destination && !destinationIsSettings
      ? (destination.chatId ?? null)
      : content.chatId,
    drawerOpen: destination ? false : drawerOpen,
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
  const performingRef = useRef(false)
  const performingPassiveRef = useRef(false)
  const destinationRef = useRef(null)
  const navigationCommittedRef = useRef(false)

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

  function wouldDisruptUser({
    passive = false,
    destination = null,
    ignoreRecentInteraction = false,
  } = {}) {
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
      activeView: destination?.view || activeViewRef.current,
      activeChatId: destination?.chatId ?? activeChatIdRef.current,
      multiPaneBuilderVisible: multiPaneBuilderVisibleRef.current,
      streamingChatIds: streamingChatIdsRef.current,
      passiveRebuild: passive,
      voiceDictationActive: voiceDictationActiveRef.current,
      lastUserInteractionAt: ignoreRecentInteraction || interactionGraceSpent()
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

  async function performReload({ passive = false, destination = null } = {}) {
    if (destination && !navigationCommittedRef.current) {
      destinationRef.current = destination
    }
    if (performingRef.current) return
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
    performingRef.current = true
    performingPassiveRef.current = passive
    pendingRef.current = false
    passiveRef.current = false
    heldSinceRef.current = 0
    if (timerRef.current) {
      win.clearTimeout(timerRef.current)
      timerRef.current = null
    }

    let registration = null
    try {
      ;({ registration } = await inspectShellUpdate({ serviceWorker: nav.serviceWorker }))
    } catch { /* a plain reload remains available without a registration */ }

    win.dispatchEvent(new win.Event(BEFORE_SHELL_RELOAD_EVENT))
    await awaitCacheFlushBeforeReload(flushPersistedQueryCache(queryClient))
    if (wouldDisruptUser({
      passive,
      destination: destinationRef.current,
      ignoreRecentInteraction: destinationRef.current != null,
    })) {
      performingRef.current = false
      deferReload({ passive })
      return
    }
    persistWorkspaceSnapshot()

    // Commit the route at the LAST possible instant. A navigation during the
    // worker handoff can still replace destinationRef and be revealed only by
    // the incoming document instead of flashing in the outgoing one.
    const reload = () => {
      if (wouldDisruptUser({
        passive,
        destination: destinationRef.current,
        ignoreRecentInteraction: destinationRef.current != null,
      })) {
        performingRef.current = false
        deferReload({ passive })
        return
      }
      navigationCommittedRef.current = true
      storage.setItem('shell-reload', JSON.stringify(deriveShellReloadState({
        workspace: workspaceStateRef.current.ws,
        activeView: activeViewRef.current,
        drawerOpen: drawerOpenRef.current,
        destination: destinationRef.current,
      })))
      replaceNavEntry('base', '/shell/')
      win.location.reload()
    }
    if (registration) {
      reloadWhenWorkerTakesOver({
        registration,
        serviceWorker: nav.serviceWorker,
        reload,
      })
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
    if (performingRef.current) return
    if (wouldDisruptUser({ passive })) {
      deferReload({ passive })
    } else {
      performReload({ passive })
    }
  }, [])

  const claimPendingShellReloadNavigation = useCallback((destination) => {
    if (!destination?.view) return false
    if (!pendingRef.current && !performingRef.current) return false
    if (navigationCommittedRef.current) return false
    // performReload clears queued state before its async handoff. Keep the
    // claimant on that request's immutable policy until it commits or defers.
    const passive = performingRef.current
      ? performingPassiveRef.current
      : passiveRef.current
    if (wouldDisruptUser({
      passive,
      destination,
      // The navigation tap is the apply boundary the owner just chose. Its own
      // pointerdown must not re-arm the generic politeness debounce.
      ignoreRecentInteraction: true,
    })) {
      // A newer navigation that cannot be folded into this reload wins. Clear
      // any older claimed route so the final safety check sees the surface that
      // navigation is about to paint.
      destinationRef.current = null
      return false
    }
    destinationRef.current = destination
    if (!performingRef.current) void performReload({ passive, destination })
    return true
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

  return {
    requestShellReload,
    checkPendingShellReload,
    claimPendingShellReloadNavigation,
  }
}
