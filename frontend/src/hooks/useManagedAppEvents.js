import { useCallback, useEffect, useRef } from 'react'

// Deliver each completion synchronously from the system-event handler. React
// state would coalesce completions arriving in one SSE chunk to the last app.
// What a manager frame hears about: an app finished updating, was uninstalled or erased (app_deleted), or
// was recovered. A frame that is open beside the Apps screen has no other way to learn of the last two.
const MANAGED_EVENTS = new Set(['app_updated', 'app_deleted', 'app_recovered'])

export function useManagedAppEvents() {
  const listeners = useRef(new Set())
  const subscribe = useCallback((listener) => {
    listeners.current.add(listener)
    return () => listeners.current.delete(listener)
  }, [])
  const observe = useCallback((ev) => {
    if (!MANAGED_EVENTS.has(ev?.type) || ev.appId == null) return
    const event = {
      type: ev.type,
      appId: String(ev.appId),
    }
    for (const listener of listeners.current) listener(event)
  }, [])
  return [subscribe, observe]
}

// Forward only the narrow {type, appId} projection of those events to reviewed app managers.
// Subscriptions do not replay: new frames fetch current state on mount.
export function useManagedAppFrameForwarding(
  framesRef, subscribe, capabilityContract,
) {
  useEffect(() => {
    if (!subscribe || capabilityContract?.data?.manage_apps !== true) return
    return subscribe((event) => {
      const message = { type: 'moebius:managed-app-event', event }
      for (const frame of framesRef.current.values()) {
        if (!frame?.contentWindow) continue
        frame.contentWindow.postMessage(message, '*')
      }
    })
  }, [capabilityContract, subscribe, framesRef])
}
