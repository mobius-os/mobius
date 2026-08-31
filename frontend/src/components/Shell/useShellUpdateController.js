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

  const applyShellUpdate = useCallback(async () => {
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
    } = inputsRef.current

    let registration = null
    try {
      ;({ registration } = await inspectShellUpdate({
        serviceWorker: nav.serviceWorker,
      }))
    } catch { /* online document navigation remains authoritative */ }

    win.dispatchEvent(new win.Event(BEFORE_SHELL_RELOAD_EVENT))
    await awaitCacheFlushBeforeReload(flushPersistedQueryCache(queryClient))
    persistWorkspaceSnapshot()
    writeShellReload(storage, deriveShellReloadState({
      workspace: workspaceStateRef.current.ws,
      activeView: activeViewRef.current,
      drawerOpen: drawerOpenRef.current,
    }))

    // The new document restores the current workspace from the one-shot state
    // above. Online shell navigation owns freshness; releasing the worker only
    // advances the coherent offline generation.
    replaceNavEntry('base', '/shell/')
    releaseWaitingShellUpdate(registration)

    // Opt only this deliberate shell-generation navigation into the
    // cross-document continuity treatment. Ordinary reloads retain ordinary
    // browser semantics, and the rendering boundary gives Chromium time to
    // activate the dynamically inserted navigation rule before replacement.
    const transitionPrepared = (
      win.__mobiusPrepareShellReloadTransition?.() === true
    )
    const navigate = () => win.location.replace('/shell/')
    if (transitionPrepared && typeof win.requestAnimationFrame === 'function') {
      win.requestAnimationFrame(navigate)
    } else {
      navigate()
    }
    return true
  }, [])

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
  }
}
