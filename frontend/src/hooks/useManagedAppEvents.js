import { useCallback, useEffect, useRef } from 'react'

// Deliver each completion synchronously from the system-event handler. React
// state would coalesce completions arriving in one SSE chunk to the last app.
// What a manager frame hears about: an app finished updating, was uninstalled or erased (app_deleted), or
// was recovered. A frame that is open beside the Apps screen has no other way to learn of the last two.
const MANAGER_EVENTS = new Set(['app_updated', 'app_deleted', 'app_recovered'])
// An app's own frames hear that it has new activity (an app-attributed notification or an unread-badge
// change). It is never forwarded to managers: they would learn every app's activity timing.
const APP_ACTIVITY = 'app_activity'
// The system stream reopened, so activity may have been missed while it was down. Every app is nudged once.
const STREAM_REOPENED = 'stream_reopened'

export function useManagedAppEvents() {
  const listeners = useRef(new Set())
  const subscribe = useCallback((listener) => {
    listeners.current.add(listener)
    return () => listeners.current.delete(listener)
  }, [])
  const deliver = useCallback((event) => {
    for (const listener of listeners.current) listener(event)
  }, [])
  const observe = useCallback((ev) => {
    if (!(MANAGER_EVENTS.has(ev?.type) || ev?.type === APP_ACTIVITY) || ev.appId == null) return
    deliver({
      type: ev.type,
      appId: String(ev.appId),
    })
  }, [deliver])
  const observeStreamReopened = useCallback(() => deliver({ type: STREAM_REOPENED }), [deliver])
  return [subscribe, observe, observeStreamReopened]
}

// Forward only the narrow {type, appId} projection of those events to reviewed app managers.
// Subscriptions do not replay: new frames fetch current state on mount.
export function useManagedAppFrameForwarding(
  framesRef, subscribe, capabilityContract,
) {
  useEffect(() => {
    if (!subscribe || capabilityContract?.data?.manage_apps !== true) return
    return subscribe((event) => {
      if (!MANAGER_EVENTS.has(event.type)) return
      const message = { type: 'moebius:managed-app-event', event }
      for (const frame of framesRef.current.values()) {
        if (!frame?.contentWindow) continue
        frame.contentWindow.postMessage(message, '*')
      }
    })
  }, [capabilityContract, subscribe, framesRef])
}

// Nudge one app's loaded frames as `moebius:app-activity` (window.mobius.onActivity) when that app has new
// activity, so an open app refreshes with its sidebar pill. The message carries no content. A buffered
// frame about to be promoted gets it too; a frame still loading fetches fresh state anyway.
export function useAppActivityFrameForwarding(
  framesRef, loadedDocsRef, subscribe, appId,
) {
  useEffect(() => {
    if (!subscribe || appId == null) return
    const ownAppId = String(appId)
    return subscribe((event) => {
      if (event.type !== STREAM_REOPENED
        && !(event.type === APP_ACTIVITY && event.appId === ownAppId)) return
      for (const [version, frame] of framesRef.current) {
        if (!loadedDocsRef.current.has(version) || !frame?.contentWindow) continue
        frame.contentWindow.postMessage({ type: 'moebius:app-activity' }, '*')
      }
    })
  }, [appId, subscribe, framesRef, loadedDocsRef])
}
