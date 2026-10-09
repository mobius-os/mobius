/** Owns one coalesced shell update and the owner's explicit refresh action. */

import { useCallback, useEffect, useRef, useState } from 'react'
import { replaceNavEntry } from '../../lib/navHistory.js'
import { BEFORE_SHELL_RELOAD_EVENT } from '../../lib/shellReloadEvents.js'
import { writeShellReload } from '../../lib/shellReloadState.js'
import {
  inspectShellUpdate,
  releaseWaitingShellUpdate,
  watchForShellUpdateOnResume,
} from '../../lib/shellUpdate.js'
import {
  awaitCacheFlushBeforeReload,
  flushPersistedQueryCache,
} from '../../queryClient.js'
import * as paneModel from './paneModel.js'
import { sharedBrowserRoutePath } from '../../lib/sharedBrowserWorkspace.js'

export function deriveShellReloadState({ workspace, activeView, drawerOpen }) {
  const content = paneModel.activeContentRoute(workspace)
  return {
    activeView: activeView === 'settings' ? 'settings' : content.view,
    activeAppId: content.appId,
    activeChatId: content.chatId,
    drawerOpen,
  }
}

/**
 * Rebuild discovery never owns navigation.
 *
 * Watcher, agent, and resume signals collapse into one `updateAvailable` bit.
 * The current document remains completely interactive until the owner invokes
 * `applyShellUpdate`; ordinary chat/app navigation never participates in the
 * update lifecycle.
 */
export default function useShellUpdateController(inputs) {
  const inputsRef = useRef(inputs)
  inputsRef.current = inputs
  const applyingRef = useRef(false)
  const [updateAvailable, setUpdateAvailable] = useState(false)

  const markShellUpdateAvailable = useCallback(() => {
    setUpdateAvailable(true)
  }, [])

  // `inspectUpdate` asks the service worker for a newer shell before leaving.
  // That check can wait up to SW_DISCOVERY_SETTLE_TIMEOUT_MS, so a reload that
  // only re-reads the current document (the theme status-bar refresh) skips it.
  const reloadShellDocument = useCallback(async ({ inspectUpdate }) => {
    if (applyingRef.current) return false
    applyingRef.current = true

    const {
      win,
      nav,
      storage,
      queryClient,
      persistWorkspaceSnapshot,
      workspaceStateRef,
      activeViewRef,
      drawerOpenRef,
      sharedBrowserAccess,
    } = inputsRef.current

    let registration = null
    if (inspectUpdate) {
      try {
        ;({ registration } = await inspectShellUpdate({
          serviceWorker: nav.serviceWorker,
        }))
      } catch { /* online document navigation remains authoritative */ }
    }

    win.dispatchEvent(new win.Event(BEFORE_SHELL_RELOAD_EVENT))
    if (!sharedBrowserAccess) {
      await awaitCacheFlushBeforeReload(flushPersistedQueryCache(queryClient))
    }
    persistWorkspaceSnapshot()
    if (!sharedBrowserAccess) {
      writeShellReload(storage, deriveShellReloadState({
        workspace: workspaceStateRef.current.ws,
        activeView: activeViewRef.current,
        drawerOpen: drawerOpenRef.current,
      }))
    }

    // The new document restores the current workspace from the one-shot state
    // above. Online shell navigation owns freshness; releasing the worker only
    // advances the coherent offline generation.
    const routePath = sharedBrowserAccess ? sharedBrowserRoutePath() : '/shell/'
    replaceNavEntry('base', routePath)
    releaseWaitingShellUpdate(registration)
    const transitionPrepared = (
      win.__mobiusPrepareShellReloadTransition?.() === true
    )
    const navigate = () => win.location.replace(routePath)
    if (transitionPrepared && typeof win.requestAnimationFrame === 'function') {
      // Give Chromium one rendering boundary to activate the cross-document
      // transition before the owner-approved replacement starts.
      win.requestAnimationFrame(navigate)
    } else {
      navigate()
    }
    return true
  }, [])

  const applyShellUpdate = useCallback(
    () => reloadShellDocument({ inspectUpdate: true }),
    [reloadShellDocument],
  )
  const reloadShell = useCallback(
    () => reloadShellDocument({ inspectUpdate: false }),
    [reloadShellDocument],
  )

  useEffect(() => watchForShellUpdateOnResume({
    doc: inputsRef.current.doc,
    win: inputsRef.current.win,
    serviceWorker: inputsRef.current.nav.serviceWorker,
    onAvailable: markShellUpdateAvailable,
  }), [markShellUpdateAvailable])

  return {
    updateAvailable,
    markShellUpdateAvailable,
    applyShellUpdate,
    reloadShell,
  }
}
