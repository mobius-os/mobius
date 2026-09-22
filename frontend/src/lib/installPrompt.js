/**
 * Captures the browser's one-shot PWA install prompt for later user action.
 *
 * Chromium can emit `beforeinstallprompt` while account setup is still on
 * screen, before the first-use card has mounted. App.jsx therefore starts
 * this module eagerly and the card subscribes to its small external-store
 * interface when it eventually appears.
 */

import { isStandaloneDisplay } from '../utils/installPlatform.js'
import { supportsWebInstall } from './webInstall.js'

let captureStarted = false
let deferredPrompt = null
let currentDocumentInstall = null
let currentDocumentInstallFailed = false
// Two different questions, deliberately kept apart.
//
// `launchedInstalled` — "does this document look like it is running AS an
// installed app?" Inferred from display mode at boot. Good enough to stop the
// product nagging someone to install what they are already using, and safe
// when wrong in that direction.
//
// `observedInstall` — "did the browser TELL us an install just happened?"
// Only `appinstalled` sets it. Nothing else may, because no browser on iOS
// answers "is this app on the home screen"; the in-app browser view iOS opens
// from a PWA even reports standalone display mode. Inferring installation
// there made the card congratulate people mid-install. A claim that specific
// needs evidence that specific.
let launchedInstalled = false
let observedInstall = false
const listeners = new Set()

function emitChange() {
  for (const listener of listeners) listener()
}

export function startInstallPromptCapture(
  target = typeof window !== 'undefined' ? window : null,
) {
  if (!target || captureStarted) return
  captureStarted = true
  launchedInstalled = isStandaloneDisplay(target)
  currentDocumentInstall = supportsWebInstall(target.navigator)
    ? target.navigator.install.bind(target.navigator)
    : null

  target.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault?.()
    deferredPrompt = event
    emitChange()
  })

  target.addEventListener('appinstalled', () => {
    deferredPrompt = null
    observedInstall = true
    emitChange()
  })
}

export function getInstallPromptSnapshot() {
  // An actual prompt is app-specific evidence and outranks the window's
  // boot-time display-mode guess. This matters when an installed Möbius window
  // navigates to a mini-app document: the window still looks standalone, but
  // Chromium may offer a prompt for the mini-app whose manifest is now active.
  if (observedInstall) return 'installed'
  if (deferredPrompt) return 'ready'
  if (launchedInstalled) return 'installed'
  if (currentDocumentInstall && !currentDocumentInstallFailed) return 'ready'
  return 'manual'
}

/**
 * True only when this page WATCHED an install complete. Use this — never the
 * snapshot above — to tell someone their app is on the home screen. The
 * snapshot answers "should we stop offering to install", which tolerates a
 * guess; this answers "did it work", which does not.
 */
export function getInstallObservedSnapshot() {
  return observedInstall
}

export function subscribeInstallPrompt(listener) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export async function requestInstall() {
  // Prefer the standards-track Web Install API when the browser exposes it.
  // If the experimental implementation fails, remember that decision and
  // leave any captured beforeinstallprompt untouched for a fresh second tap;
  // both APIs consume transient user activation, so same-click fallback is
  // not reliable.
  if (currentDocumentInstall && !currentDocumentInstallFailed) {
    try {
      await currentDocumentInstall()
      deferredPrompt = null
      observedInstall = true
      emitChange()
      return { outcome: 'accepted' }
    } catch (error) {
      if (error?.name === 'AbortError') return { outcome: 'dismissed' }
      currentDocumentInstallFailed = true
      emitChange()
      if (deferredPrompt) return { outcome: 'fallback-ready' }
      return { outcome: 'unavailable' }
    }
  }

  const promptEvent = deferredPrompt
  if (!promptEvent) return { outcome: 'unavailable' }

  // A BeforeInstallPromptEvent can only be used once. Clear it before
  // awaiting browser UI so a fast second tap cannot call prompt() twice.
  deferredPrompt = null
  emitChange()

  try {
    const promptResult = await promptEvent.prompt()
    const choice = typeof promptResult?.outcome === 'string'
      ? promptResult
      : await promptEvent.userChoice
    return {
      outcome: choice?.outcome === 'accepted' ? 'accepted' : 'dismissed',
    }
  } catch {
    return { outcome: 'unavailable' }
  }
}
