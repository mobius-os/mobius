import { useEffect, useRef, useState } from 'react'
import { api } from '../../api/client.js'

/**
 * Report which apps this shell is visibly showing, so the server withholds an
 * app's push while the owner is looking at it (presence.has_app_watchers) —
 * the app counterpart of a chat's live stream suppressing its pushes.
 *
 * The report is bound to the live system-stream subscription: the server
 * forgets it when that stream disconnects, and a new stream id is reported
 * afresh. `visibleAppIds` is the same painted-app set that drives
 * `moebius:frame-visibility`; a hidden page reports nothing visible.
 */
export default function useVisibleAppPresence(subscriptionId, visibleAppIds) {
  const [pageVisible, setPageVisible] = useState(
    () => document.visibilityState === 'visible',
  )
  useEffect(() => {
    const onVisibility = () => setPageVisible(document.visibilityState === 'visible')
    document.addEventListener('visibilitychange', onVisibility)
    return () => document.removeEventListener('visibilitychange', onVisibility)
  }, [])

  // A value key: the visible set is re-derived as a fresh Set on each change.
  const shown = JSON.stringify(
    pageVisible ? [...visibleAppIds].map(String).sort() : [],
  )
  // Orders this shell's reports; the server ignores one older than the last,
  // so a delayed "visible" cannot outlive a later "hidden".
  const sequenceRef = useRef(0)
  useEffect(() => {
    if (!subscriptionId) return
    sequenceRef.current += 1
    // A stream that is already gone answers 404; its successor re-reports.
    api.events.reportVisibleApps(
      subscriptionId, sequenceRef.current, JSON.parse(shown),
    ).catch(() => {})
  }, [subscriptionId, shown])
}
