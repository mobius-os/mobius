/* Retire each Open-app shortcut five seconds after the owner first sees it. */

import { useEffect, useRef } from 'react'
import { shouldShowOpenAppCta } from '../openAppCtaState.js'

const AUTO_DISMISS_MS = 5000

function appBuildKey(app) {
  return `${app?.id ?? ''}:${app?.updated_at ?? ''}`
}

export default function useOpenAppCtaAutoDismiss({
  builtApps,
  turnActive,
  presented,
  onDismissApp,
}, {
  setTimer = setTimeout,
  clearTimer = clearTimeout,
  documentTarget = typeof document === 'undefined' ? null : document,
  windowTarget = typeof window === 'undefined' ? null : window,
} = {}) {
  const timersRef = useRef(new Map())
  const onDismissRef = useRef(onDismissApp)
  onDismissRef.current = onDismissApp

  useEffect(() => {
    const timers = timersRef.current
    function pageIsVisible() {
      return !documentTarget?.visibilityState
        || documentTarget.visibilityState === 'visible'
    }

    function reconcile() {
      const eligibleApps = new Map(
        (Array.isArray(builtApps) ? builtApps : [])
          .filter(app => shouldShowOpenAppCta(app, turnActive))
          .map(app => [appBuildKey(app), app]),
      )

      // A click, acknowledgement, or replacement build retires the old timer.
      // Covering a shortcut after it was seen does not: its original five-second
      // clock keeps the meaning the owner already observed.
      for (const [key, entry] of timers) {
        if (eligibleApps.has(key)) continue
        clearTimer(entry.timerId)
        timers.delete(key)
      }

      // A retained chat, a shell-covered surface, a disconnected foot, or a
      // background browser tab has not presented the shortcut to the owner yet.
      if (!presented || !pageIsVisible() || typeof onDismissApp !== 'function') return

      for (const [key, app] of eligibleApps) {
        if (timers.has(key)) continue
        const timerId = setTimer(() => {
          const current = timersRef.current.get(key)
          if (!current || current.timerId !== timerId) return
          timersRef.current.delete(key)
          onDismissRef.current?.(current.app)
        }, AUTO_DISMISS_MS)
        timers.set(key, { timerId, app })
      }
    }

    function reconcileForeground() {
      if (pageIsVisible()) reconcile()
    }

    reconcile()
    documentTarget?.addEventListener?.('visibilitychange', reconcileForeground)
    windowTarget?.addEventListener?.('pageshow', reconcileForeground)
    return () => {
      documentTarget?.removeEventListener?.('visibilitychange', reconcileForeground)
      windowTarget?.removeEventListener?.('pageshow', reconcileForeground)
    }
  }, [
    builtApps,
    turnActive,
    presented,
    onDismissApp,
    setTimer,
    clearTimer,
    documentTarget,
    windowTarget,
  ])

  useEffect(() => () => {
    for (const entry of timersRef.current.values()) {
      clearTimer(entry.timerId)
    }
    timersRef.current.clear()
  }, [clearTimer])
}
